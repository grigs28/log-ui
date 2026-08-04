"""log-ui: self-hosted log viewer on VictoriaLogs, with yz-login SSO."""
import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import SECRET_KEY, get_sso_config, save_sso_config
from .auth import router as auth_router, handle_ticket
from . import vl
from . import servers as srv
from .prefs import load_prefs, save_prefs

BASE = Path(__file__).resolve().parent.parent

app = FastAPI(title="log-ui")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")
app.include_router(auth_router)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

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


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


# ---------------- 总览大屏 ----------------
@app.get("/")
async def overview(request: Request, days: int = 7):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    days = days if days in RANGE_OPTIONS else 7
    hosts_seen = [h for h in vl.field_values("hostname", limit=200) if h]
    data = {
        "days": days,
        "total": vl.stats_total("*", days),
        "errors": vl.stats_total("level:error", days),
        "warnings": vl.stats_total("level:warning", days),
        "nhosts": len(hosts_seen),
        "by_level": vl.stats_by("level", days),
        "by_host": vl.stats_by("hostname", days),
        "errors_by_host": vl.stats_by("hostname", days, "level:error"),
        "trend": [[t, c] for t, c in vl.stats_range("*", days, "1h")],
    }
    return templates.TemplateResponse(
        request, "overview.html",
        {"user": user, "theme": _theme(user), "data": data, "range_options": RANGE_OPTIONS},
    )


# ---------------- 日志搜索 (原 / ) ----------------
@app.get("/search")
async def search(
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
    hosts = ["ALL"] + vl.field_values("hostname", limit=100)
    return templates.TemplateResponse(
        request, "index.html",
        {"user": user, "theme": prefs["theme"], "logs": logs, "hosts": hosts, "levels": LEVELS,
         "refresh_options": REFRESH_OPTIONS, "q": q, "host": host, "level": level,
         "limit": limit, "refresh": refresh, "expr": expr, "count": len(logs)},
    )


@app.get("/logs")
async def logs_fragment(request: Request, q: str = "*", host: str = "ALL",
                        level: str = "ALL", limit: int = 100):
    if not _require_user(request):
        return Response("Unauthorized", status_code=401)
    expr = _build_expr(q, host, level)
    logs = vl.query_logs(expr=expr, limit=limit)
    return templates.TemplateResponse(request, "_logs.html", {"logs": logs})


# ---------------- 服务器管理 ----------------
@app.get("/servers")
async def servers_page(request: Request, edit: str | None = None):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    registry = srv.load_registry()
    ignored = set(srv.load_ignore())
    reg_names = {s.get("name") for s in registry}
    by_host = vl.stats_by("hostname", 7)
    errors_by_host = vl.stats_by("hostname", 7, "level:error")
    all_hosts = [h for h in vl.field_values("hostname", limit=200) if h]

    rows = []
    for s in registry:
        n = s.get("name", "")
        rows.append({"name": n, "ip": s.get("ip", ""), "type": s.get("type", ""),
                     "note": s.get("note", ""), "registered": True,
                     "count": by_host.get(n, 0), "errors": errors_by_host.get(n, 0),
                     "last_seen": vl.last_seen(n)})
    for h in all_hosts:
        if h in reg_names or h in ignored:
            continue
        rows.append({"name": h, "ip": "", "type": "", "note": "", "registered": False,
                     "count": by_host.get(h, 0), "errors": errors_by_host.get(h, 0),
                     "last_seen": vl.last_seen(h)})
    rows.sort(key=lambda r: r["count"], reverse=True)

    edit_entry = None
    edit_is_registered = False
    if edit:
        edit_entry = next((s for s in registry if s.get("name") == edit), None)
        if edit_entry:
            edit_is_registered = True
        elif edit not in ignored:
            edit_entry = {"name": edit, "ip": "", "type": "", "note": ""}

    return templates.TemplateResponse(
        request, "servers.html",
        {"user": user, "theme": _theme(user), "rows": rows, "is_admin": bool(user.get("is_admin")),
         "edit_entry": edit_entry, "edit_is_registered": edit_is_registered,
         "ignored": sorted(ignored)},
    )


@app.post("/servers/add")
async def servers_add(request: Request, name: str = Form(""), ip: str = Form(""),
                      stype: str = Form(""), note: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/servers", status_code=303)
    srv.add_server(name, ip, stype, note)
    return RedirectResponse("/servers", status_code=303)


@app.post("/servers/delete")
async def servers_delete(request: Request, name: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/servers", status_code=303)
    srv.remove_server(name)
    return RedirectResponse("/servers", status_code=303)


@app.post("/servers/update")
async def servers_update(request: Request, orig_name: str = Form(""), name: str = Form(""),
                         ip: str = Form(""), stype: str = Form(""), note: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/servers", status_code=303)
    srv.update_server(orig_name, name, ip, stype, note)
    return RedirectResponse("/servers", status_code=303)


@app.post("/servers/ignore")
async def servers_ignore(request: Request, name: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/servers", status_code=303)
    srv.add_ignore(name)
    return RedirectResponse("/servers", status_code=303)


@app.post("/servers/unignore")
async def servers_unignore(request: Request, name: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/servers", status_code=303)
    srv.remove_ignore(name)
    return RedirectResponse("/servers", status_code=303)


# ---------------- 实时 tail (WebSocket) ----------------
@app.get("/tail")
async def tail_page(request: Request):
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
async def settings_get(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    prefs = load_prefs(user.get("username", ""))
    return templates.TemplateResponse(
        request, "settings.html",
        {"user": user, "prefs": prefs, "refresh_options": REFRESH_OPTIONS,
         "theme": prefs["theme"], "sso": get_sso_config()},
    )


@app.post("/settings")
async def settings_post(request: Request, refresh_sec: int = Form(10),
                        limit: int = Form(100), theme: str = Form("dark")):
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


@app.post("/settings/sso")
async def settings_sso_post(request: Request, yz_login_url: str = Form(""), yz_app_id: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse("/settings", status_code=303)
    save_sso_config(yz_login_url, yz_app_id)
    return RedirectResponse("/settings", status_code=303)
