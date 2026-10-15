"""log-ui: self-hosted log viewer on VictoriaLogs, with yz-login SSO."""
import asyncio
import json
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import (SECRET_KEY, get_sso_config, save_sso_config,
                     get_retention, save_retention, RETENTION_CHOICES,
                     save_notify)
from . import retention as retention_mod
from .auth import router as auth_router, handle_ticket
from . import vl
from . import servers as srv
from . import vecsync
from . import collectors
from . import disks
from . import actions
from . import notify
from .prefs import load_prefs, save_prefs

BASE = Path(__file__).resolve().parent.parent

app = FastAPI(title="log-ui")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")


@app.middleware("http")
async def _no_store_html(request: Request, call_next):
    """HTML 一律不缓存。

    页面是服务端渲染的，JS 内联在 HTML 里。而 TemplateResponse 不带任何缓存头，
    浏览器就会**启发式缓存** HTML —— 普通 F5 可能直接用本地副本，
    于是「改了前端但页面没变」，排查起来非常费劲（2026-10-15 右键菜单不弹
    就是这么被坑的）。静态资源另说：StaticFiles 自带 etag/last-modified，
    正常协商缓存即可。
    """
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp
app.include_router(auth_router)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _app_version() -> str:
    try:
        for line in (BASE / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
            if line.startswith("## ["):
                return line.split("[")[1].split("]")[0]
    except Exception:
        pass
    return "?"


# 注入的是函数而不是取值：模板渲染时现读 CHANGELOG.md，
# 否则版本号在 import 时被冻结，与实时的 /version、/changelog 不一致
templates.env.globals["version"] = _app_version

LEVELS = ["ALL", "error", "warning", "info", "debug"]
REFRESH_OPTIONS = [0, 5, 10, 30, 60]
RANGE_OPTIONS = [1, 3, 7, 30]


def _require_user(request: Request):
    return request.session.get("user")


def _theme(user) -> str:
    return load_prefs(user.get("username", "")).get("theme", "dark") if user else "dark"


def _build_expr(q: str, host: str, level: str) -> str:
    parts = []
    if host and host != "ALL":
        parts.append(f"hostname:{host}")
    if level and level != "ALL":
        parts.append(f"level:{level}")
    if q and q.strip() and q.strip() != "*":
        parts.append(q.strip())
    return " ".join(parts) if parts else "*"


# ---- CSRF protection for state-changing endpoints ----
def csrf_protect(request: Request):
    """Reject cross-origin POST requests (defense-in-depth; same_site=lax also applies)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(status_code=403, detail="CSRF check failed")


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


_watchdog_task = None


@app.on_event("startup")
async def _start_watchdog():
    global _watchdog_task
    _watchdog_task = asyncio.create_task(collectors.watchdog())


@app.on_event("shutdown")
async def _stop_watchdog():
    """必须显式取消 watchdog —— 它是 while True 常驻任务，不取消会让 uvicorn
    关停时一直「Waiting for background tasks to complete」，systemd 只能等
    TimeoutStopSec 超时后强杀（2026-10-15 实测：restart 卡在 deactivating 90s+）。"""
    if _watchdog_task and not _watchdog_task.done():
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except asyncio.CancelledError:
            pass


# 注入全局模板变量（版本号）
@app.get("/version")
def app_version():
    return {"version": _app_version()}


@app.get("/changelog")
def changelog(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    try:
        md = (BASE / "CHANGELOG.md").read_text(encoding="utf-8")
    except Exception:
        md = "# Changelog\n\n（暂无）"
    return templates.TemplateResponse(request, "changelog.html",
                                      {"user": user, "theme": _theme(user), "md": md})


# ---------------- 总览大屏 ----------------
@app.get("/")
def overview(request: Request, days: int = 7, ticket: str | None = None):
    if ticket:
        return handle_ticket(request, ticket)
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    days = days if days in RANGE_OPTIONS else 7
    from . import servers as srv_ov
    ignored_set = set(srv_ov.load_ignore())
    by_host_raw = vl.stats_by("hostname", days)
    by_host_f  = {k: v for k, v in by_host_raw.items() if k and k != "(none)" and k not in ignored_set}
    by_err_raw = vl.stats_by("hostname", days, "level:error")
    by_err_f   = {k: by_err_raw.get(k, 0) for k in by_host_f}   # 0-error hosts included
    by_warn_raw= vl.stats_by("hostname", days, "level:warning")
    by_warn_f  = {k: by_warn_raw.get(k, 0) for k in by_host_f}
    data = {
        "days": days,
        "total": sum(by_host_f.values()),
        "errors": sum(by_err_f.values()),
        "warnings": sum(by_warn_f.values()),
        "nhosts": len(by_host_f),
        "by_level": vl.stats_by("level", days),
        "by_host": by_host_f,
        "errors_by_host": by_err_f,
        "trend": [[t, c] for t, c in vl.stats_range("*", days, "1h")],
    }
    return templates.TemplateResponse(
        request, "overview.html",
        {"user": user, "theme": _theme(user), "data": data, "range_options": RANGE_OPTIONS,
         "badges": collectors.status()},
    )


# ---------------- 日志搜索 ----------------
@app.get("/search")
def search(
    request: Request, ticket: str | None = None, q: str = "*",
    host: str | None = None, level: str = "ALL",
    limit: int | None = None, refresh: int | None = None,
):
    if ticket:
        return handle_ticket(request, ticket)
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    prefs = load_prefs(user.get("username", ""))
    host = host or "ALL"
    limit = int(limit) if limit is not None else prefs["limit"]
    refresh = int(refresh) if refresh is not None else prefs["refresh_sec"]
    expr = _build_expr(q, host, level)
    logs = vl.query_logs(expr=expr, limit=limit)
    # sync host list with /servers: exclude ignored, registered first
    from . import servers as srv_search
    ignored_set = set(srv_search.load_ignore())
    reg_names = {s.get("name") for s in srv_search.load_registry()}
    all_hosts = [h for h in vl.field_values("hostname", limit=200) if h and h not in ignored_set]
    all_hosts.sort(key=lambda h: (h not in reg_names, h))
    hosts = ["ALL"] + all_hosts
    return templates.TemplateResponse(
        request, "index.html",
        {"user": user, "theme": prefs["theme"], "logs": logs, "hosts": hosts, "levels": LEVELS,
         "refresh_options": REFRESH_OPTIONS, "q": q, "host": host, "level": level,
         "limit": limit, "refresh": refresh, "expr": expr, "count": len(logs)},
    )


@app.get("/logs")
def logs_fragment(request: Request, q: str = "*", host: str = "ALL",
                  level: str = "ALL", limit: int = 100):
    if not _require_user(request):
        return Response("Unauthorized", status_code=401)
    expr = _build_expr(q, host, level)
    logs = vl.query_logs(expr=expr, limit=limit)
    return templates.TemplateResponse(request, "_logs.html", {"logs": logs})


# ---------------- 服务器管理 ----------------
@app.get("/servers")
def servers_page(request: Request, edit: str | None = None, vecerr: str | None = None):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    registry = srv.load_registry()
    ignored = set(srv.load_ignore())
    reg_names = {s.get("name") for s in registry}
    all_hosts = [h for h in vl.field_values("hostname", limit=200) if h]
    summary = vl.host_summary()  # batch: 2 queries for ALL hosts

    rows = []
    for s in registry:
        n = s.get("name", "")
        if n in ignored:
            continue  # 已注册且被忽略：只显示在忽略列表（删除后回到纳管表）
        hs = summary.get(n, {})
        rows.append({"name": n, "ip": s.get("ip", ""), "type": s.get("type", ""),
                     "note": s.get("note", ""), "registered": True, "ignored": False,
                     "min_level": s.get("min_level", ""),
                     "count": hs.get("count", 0), "errors": hs.get("errors", 0),
                     "last_seen": hs.get("last_seen")})
    for h in all_hosts:
        if h in reg_names or h in ignored:
            continue
        hs = summary.get(h, {})
        rows.append({"name": h, "ip": "", "type": "", "note": "", "registered": False,
                     "ignored": False, "min_level": "",
                     "count": hs.get("count", 0), "errors": hs.get("errors", 0),
                     "last_seen": hs.get("last_seen")})
    rows.sort(key=lambda r: r["count"], reverse=True)

    edit_entry = None
    edit_is_registered = False
    if edit:
        edit_entry = next((s for s in registry if s.get("name") == edit), None)
        if edit_entry:
            edit_is_registered = True
        elif edit not in ignored:
            edit_entry = {"name": edit, "ip": "", "type": "", "note": "", "min_level": ""}

    return templates.TemplateResponse(
        request, "servers.html",
        {"user": user, "theme": _theme(user), "rows": rows, "is_admin": bool(user.get("is_admin")),
         "edit_entry": edit_entry, "edit_is_registered": edit_is_registered,
         "ignored_full": srv.load_ignore_full(), "vecerr": vecerr},
    )


@app.post("/servers/add")
def servers_add(request: Request, name: str = Form(""), ip: str = Form(""),
                stype: str = Form(""), note: str = Form(""), min_level: str = Form(""),
                csrf_ok: None = Depends(csrf_protect)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/servers", status_code=303)
    srv.add_server(name, ip, stype, note, min_level)
    ok, msg = vecsync.sync()
    dest = "/servers" if ok else f"/servers?vecerr={quote(msg)}"
    return RedirectResponse(dest, status_code=303)


@app.post("/servers/delete")
def servers_delete(request: Request, name: str = Form(""),
                   csrf_ok: None = Depends(csrf_protect)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/servers", status_code=303)
    srv.remove_server(name)
    return RedirectResponse("/servers", status_code=303)


@app.post("/servers/update")
def servers_update(request: Request, orig_name: str = Form(""), name: str = Form(""),
                   ip: str = Form(""), stype: str = Form(""), note: str = Form(""),
                   min_level: str = Form(""),
                   csrf_ok: None = Depends(csrf_protect)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/servers", status_code=303)
    srv.update_server(orig_name, name, ip, stype, note, min_level)
    ok, msg = vecsync.sync()
    dest = "/servers" if ok else f"/servers?vecerr={quote(msg)}"
    return RedirectResponse(dest, status_code=303)


@app.post("/servers/ignore")
def servers_ignore(request: Request, name: str = Form(""),
                   csrf_ok: None = Depends(csrf_protect)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/servers", status_code=303)
    reg = {s.get("name"): s for s in srv.load_registry()}
    e = reg.get((name or "").strip(), {})
    srv.add_ignore(name, ip=e.get("ip", ""), stype=e.get("type", ""),
                   min_level=e.get("min_level", ""))
    ok, msg = vecsync.sync()
    dest = "/servers" if ok else f"/servers?vecerr={quote(msg)}"
    return RedirectResponse(dest, status_code=303)


@app.post("/servers/unignore")
def servers_unignore(request: Request, name: str = Form(""),
                     csrf_ok: None = Depends(csrf_protect)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/servers", status_code=303)
    srv.remove_ignore(name)
    ok, msg = vecsync.sync()
    dest = "/servers" if ok else f"/servers?vecerr={quote(msg)}"
    return RedirectResponse(dest, status_code=303)


# ---------------- 磁盘盘位图 ----------------

def _disks_back(back: str) -> str:
    """操作完回到原来那张卡片，而不是跳回页面顶部。

    页面上有 20 张服务器卡片，点一次「应用」就滚回顶部、得重新找，
    所以跳转带 ?focus=<服务器名>，由页面脚本滚回去并高亮一下。
    """
    b = (back or "").strip()
    return f"/disks?focus={quote(b)}" if b else "/disks"


@app.get("/disks")
def disks_page(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    data = disks.merged()
    return templates.TemplateResponse(
        request, "disks.html",
        {"user": user, "theme": _theme(user), "is_admin": bool(user.get("is_admin")),
         "edit_server": None, **data},
    )


@app.post("/disks/server/add")
def disks_server_add(request: Request, name: str = Form(""), ip: str = Form(""),
                     per_row: int = Form(0),
                     csrf_ok: None = Depends(csrf_protect)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/disks", status_code=303)
    disks.upsert_server(name, ip, per_row or None)
    return RedirectResponse(_disks_back(name), status_code=303)   # 新增完滚到新卡片


@app.post("/disks/server/delete")
def disks_server_delete(request: Request, name: str = Form(""),
                        csrf_ok: None = Depends(csrf_protect)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/disks", status_code=303)
    disks.remove_server(name)
    return RedirectResponse("/disks", status_code=303)


@app.post("/disks/archive")
def disks_archive(request: Request, server: str = Form(""), back: str = Form(""),
                  csrf_ok: None = Depends(csrf_protect)):
    """把日志发现的盘落进档案（槽号顺延，事后可改）。"""
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/disks", status_code=303)
    disks.archive_unfiled(server)
    return RedirectResponse(_disks_back(back), status_code=303)


@app.post("/disks/view")
def disks_view(request: Request, server: str = Form(""), mode: str = Form(""),
               orient: str = Form(""), per_row: int = Form(0), rows: int = Form(0),
               ip: str = Form(""), back: str = Form(""),
               csrf_ok: None = Depends(csrf_protect)):
    """面板横竖 + 盘向 + 每排数量 + 排数（每排 × 排数 = 槽位数）。"""
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/disks", status_code=303)
    disks.set_view(server, mode, per_row, orient, ip, rows)
    return RedirectResponse(_disks_back(back), status_code=303)


@app.post("/disks/order")
def disks_order(request: Request, server: str = Form(""), dev: list[str] = Form([]),
                osd: list[str] = Form([]), note: list[str] = Form([]),
                size: list[str] = Form([]), ip: str = Form(""), back: str = Form(""),
                csrf_ok: None = Depends(csrf_protect)):
    """拖动改面板显示位置 → 槽号按新顺序重排。"""
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/disks", status_code=303)
    disks.save_order(server, list(zip(dev, osd, note, size)), ip)
    return RedirectResponse(_disks_back(back), status_code=303)


# 注：原 POST /disks/slots（独立设槽位数）已移除。
# 总槽位数改为派生值 = 每排 × 排数，由 /disks/view 一并应用 ——
# 曾出现「填了 24 却点成应用、按 4×10=40 写回」的误操作，两个入口收敛成一个。
# 后端函数 set_slot_count() 保留，供 set_view 调用。


@app.post("/disks/clean")
def disks_clean(request: Request, server: str = Form(""), back: str = Form(""),
                csrf_ok: None = Depends(csrf_protect)):
    """删掉档案里「日志查不到记录」的盘（多半是拔掉了）。"""
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/disks", status_code=303)
    disks.remove_missing(server)
    return RedirectResponse(_disks_back(back or server), status_code=303)


@app.post("/disks/save")
def disks_save(request: Request, server: str = Form(""), per_row: int = Form(0),
               slot: list[str] = Form([]), dev: list[str] = Form([]),
               osd: list[str] = Form([]), note: list[str] = Form([]),
               size: list[str] = Form([]), back: str = Form(""),
               csrf_ok: None = Depends(csrf_protect)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/disks", status_code=303)
    disks.save_table(server, per_row, list(zip(slot, dev, osd, note, size)))
    return RedirectResponse(_disks_back(back), status_code=303)


# ---------------- 单盘动作：检测 / LED 定位 ----------------
# 这两个动作会在 PVE 节点上执行命令，只对管理员开放。
# 目标 IP 由 actions.resolve_ip() 从 NAS hosts 清单解析，不接受网页传地址。
@app.post("/disks/act")
def disks_act(request: Request, server: str = Form(""), dev: str = Form(""),
              act: str = Form(""), seconds: int = Form(0), back: str = Form(""),
              csrf_ok: None = Depends(csrf_protect)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/disks", status_code=303)
    try:
        job = actions.start(act, server, dev, seconds or actions.LED_SECONDS)
    except actions.Rejected as e:
        # 输入不合法：XHR 回错误，表单回跳（不把细节回显到页面）
        if "json" in (request.headers.get("accept") or ""):
            return {"id": "", "error": str(e)}
        return RedirectResponse(_disks_back(back or server), status_code=303)
    if "json" in (request.headers.get("accept") or ""):
        return {"id": job["id"], "action": job["action"], "host": job["host"], "dev": job["dev"]}
    return RedirectResponse(_disks_back(back or server), status_code=303)


@app.get("/disks/act/{job_id}")
def disks_act_poll(request: Request, job_id: str):
    user = _require_user(request)
    if not user:
        return Response("Unauthorized", status_code=401)
    if not user.get("is_admin"):
        return Response("Forbidden", status_code=403)
    j = actions.get(job_id)
    if not j:
        return {"state": "missing"}
    return {"state": j["state"], "rc": j["rc"], "action": j["action"],
            "host": j["host"], "dev": j["dev"], "cmd": j["cmd"],
            "elapsed": round((j["ended"] or time.time()) - j["started"], 1),
            "seconds": j.get("seconds") or 0,
            "verdict": j.get("verdict"), "output": j.get("output", "")}


# ---------------- 实时 tail (WebSocket) ----------------
@app.get("/tail")
def tail_page(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    return templates.TemplateResponse(request, "tail.html", {"user": user, "theme": _theme(user)})


@app.websocket("/ws/tail")
async def ws_tail(ws: WebSocket):
    await ws.accept()
    if not ws.scope.get("session", {}).get("user"):
        await ws.close(code=4401)
        return
    last = int(time.time()) - 120
    try:
        while True:
            now = int(time.time())
            logs = await asyncio.to_thread(vl.query_logs, "*", 60, last, now)
            for log in reversed(logs):
                await ws.send_text(json.dumps({
                    "time": log.get("_time"), "level": log.get("level") or "info",
                    "host": log.get("hostname") or "-", "app": log.get("app_name") or "-",
                    "msg": (log.get("_msg", "") or "")[:300],
                }, ensure_ascii=False))
            if logs:
                last = now
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# ---------------- 设置 ----------------
@app.get("/settings")
def settings_get(request: Request, reterr: str | None = None,
                 nerr: str | None = None, nok: str | None = None):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    prefs = load_prefs(user.get("username", ""))
    return templates.TemplateResponse(
        request, "settings.html",
        {"user": user, "prefs": prefs, "refresh_options": REFRESH_OPTIONS,
         "theme": prefs["theme"], "sso": get_sso_config(),
         "retention_days": get_retention(), "retention_choices": RETENTION_CHOICES,
         "retention_live": retention_mod.current_retention_days(),
         "reterr": reterr,
         "notify": notify.status(), "nerr": nerr, "nok": nok},
    )


@app.post("/settings/retention")
def settings_retention(request: Request, retention_days: int = Form(180),
                       csrf_ok: None = Depends(csrf_protect)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/settings", status_code=303)
    if retention_days not in RETENTION_CHOICES:
        return RedirectResponse("/settings", status_code=303)
    save_retention(retention_days)
    ok, msg = retention_mod.apply(retention_days)
    dest = "/settings" if ok else f"/settings?reterr={quote(msg)}"
    return RedirectResponse(dest, status_code=303)


@app.post("/settings")
def settings_post(request: Request, refresh_sec: int = Form(10),
                  limit: int = Form(100), theme: str = Form("dark"),
                  csrf_ok: None = Depends(csrf_protect)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    theme = theme if theme in ("dark", "light") else "dark"
    save_prefs(user.get("username", ""), {
        "refresh_sec": max(0, min(int(refresh_sec), 300)),
        "limit": max(1, min(int(limit), 1000)),
        "theme": theme,
    })
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/notify")
def settings_notify(request: Request, enabled: str = Form(""),
                    url: str = Form(""), api_key: str = Form(""),
                    phones: str = Form(""),
                    csrf_ok: None = Depends(csrf_protect)):
    """保存钉钉通知配置。"""
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/settings", status_code=303)
    save_notify(enabled not in ("", "0", "false", "off"), url, api_key,
                notify.clean_phones(phones))
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/notify/test")
def settings_notify_test(request: Request, phones: str = Form(""),
                         csrf_ok: None = Depends(csrf_protect)):
    """发一条测试消息。表单里带的手机号优先，没带就用已保存的接收人。"""
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/settings", status_code=303)
    lst = notify.clean_phones(phones)
    r = notify.send("log-ui 测试消息",
                    "## log-ui 测试消息\n\n"
                    "如果你看到这条，说明钉钉通知已打通。\n\n"
                    "发送人：%s" % (user.get("display_name") or user.get("username") or "-"),
                    phones=lst or None)
    key = "nok" if r["ok"] else "nerr"
    return RedirectResponse(f"/settings?{key}={quote(r['msg'][:200])}", status_code=303)


@app.post("/settings/sso")
def settings_sso_post(request: Request, yz_login_url: str = Form(""),
                      yz_app_id: str = Form(""),
                      csrf_ok: None = Depends(csrf_protect)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/settings", status_code=303)
    save_sso_config(yz_login_url, yz_app_id)
    return RedirectResponse("/settings", status_code=303)
