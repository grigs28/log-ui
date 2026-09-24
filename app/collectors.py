"""采集器状态探测 + 自愈 watchdog。

组件与判定（双层：进程 + 流量）：
  vector        docker 容器 Running；再以「最近 10 分钟 VL 入库量 > 0」判流量。
                进程 Up 但零流量 = 僵尸态（上次事故形态），会触发 docker restart。
  cobian        systemd timer active + 上次运行 Result + SMB 挂载在位。
                timer 停 → start timer；上次运行失败 → 触发一次重跑；
                SMB 挂载丢失只报红不自愈（挂载脚本含凭据，留人工处理）。
  victorialogs  /health 200。

自愈：10 分钟组件级节流；动作写留痕日志到 VL（hostname=log-ui，
app_name=selfheal——注意不能复用本机名，本机名可能已被忽略拒收）。
settings.yaml 设 selfheal: false 可整体关闭。
"""
import asyncio
import json
import os
import subprocess
import time

import httpx

from .config import VL_URL, get_flag

HEAL_COOLDOWN = 600      # 同一组件两次自愈的最小间隔（秒）
CHECK_INTERVAL = 60      # watchdog 轮询周期
FLOW_WINDOW_MIN = 10     # 流量判定窗口

_cache = {"ts": 0.0, "badges": None}     # status() 供页面用的 60s 缓存
_last_heal: dict[str, float] = {}


def _sh(cmd: list, timeout: int = 4) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def _flow_count() -> "int | None":
    """最近 FLOW_WINDOW 分钟 VL 入库条数；VL 不可达返回 None。"""
    try:
        t0 = int((time.time() - FLOW_WINDOW_MIN * 60) * 1000)
        r = httpx.get(f"{VL_URL}/select/logsql/query",
                      params={"query": "* | stats count() as n", "limit": "1",
                              "start": str(t0)},
                      timeout=5)
        if r.status_code != 200:
            return None
        for line in r.text.splitlines():
            line = line.strip()
            if line:
                return int(json.loads(line).get("n", 0))
        return 0
    except Exception:
        return None


def _vl_alive() -> bool:
    try:
        return httpx.get(f"{VL_URL}/health", timeout=5).status_code == 200
    except Exception:
        return False


def _log_heal(msg: str) -> None:
    """自愈留痕写 VL；失败静默（不能因留痕影响主流程）。"""
    try:
        rec = {"_msg": msg, "_time": int(time.time() * 1000), "hostname": "log-ui",
               "app_name": "selfheal", "level": "warning", "log_source": "log-ui"}
        httpx.post(f"{VL_URL}/insert/jsonline",
                   content=json.dumps(rec, ensure_ascii=False) + "\n", timeout=5)
    except Exception:
        pass


def probe() -> list:
    """返回徽章数据 [{key,label,state(css class),detail}, ...]。"""
    badges = []

    # ---- vector ----
    # 注意：docker 29.x 的 --format 里 .State.RestartCount 不存在（模板报错），
    # 只查 Status，判定以 {{.State.Status}} == "running" 为准
    ok_v, out = _sh(["docker", "inspect", "vector", "--format", "{{.State.Status}}"])
    running = ok_v and out.strip() == "running"
    flow = _flow_count()
    # 徽章二态：连接（进程在跑，含降级）=蓝；未连接（容器停/状态未知）=灰
    if not running:
        state, css = "down", "off"
    elif flow is None:
        state, css = "unknown", "off"     # VL 不可达，无法判流量
    elif flow == 0:
        state, css = "warn", "on"         # 僵尸态：进程在但断流（悬停见详情）
    else:
        state, css = "ok", "on"
    badges.append({"key": "vector", "label": "vector", "state": state, "css": css,
                   "detail": f"容器{'Up' if running else '未运行'}，{FLOW_WINDOW_MIN}分钟入库 "
                             f"{'?' if flow is None else flow} 条"})

    # ---- cobian ----
    ok_t, _ = _sh(["systemctl", "is-active", "cobian-log-collector.timer"])
    _, result = _sh(["systemctl", "show", "cobian-log-collector.service",
                     "--property=Result", "--value"])
    mounted = os.path.ismount("/mnt/cobian-logs")
    if not ok_t:
        state, css = "down", "off"
    elif result.strip().lower() == "failed":
        state, css = "warn", "on"
    elif not mounted:
        state, css = "warn", "on"         # SMB 丢失只报警，不自愈（含凭据脚本留人工）
    else:
        state, css = "ok", "on"
    badges.append({"key": "cobian", "label": "cobian", "state": state, "css": css,
                   "detail": f"timer {'active' if ok_t else '停止'}，上次运行 {result or '?'}，"
                             f"SMB 挂载{'在' if mounted else '丢失'}"})

    # ---- victorialogs ----
    alive = _vl_alive()
    badges.append({"key": "victorialogs", "label": "victorialogs",
                   "state": "ok" if alive else "down",
                   "css": "on" if alive else "off",
                   "detail": "/health " + ("200" if alive else "不可达")})
    return badges


def _try_heal(key: str, action: list, desc: str) -> None:
    now = time.time()
    if now - _last_heal.get(key, 0) < HEAL_COOLDOWN:
        return
    _last_heal[key] = now
    ok, out = _sh(action, timeout=90)
    _log_heal(f"自愈 {key}: {desc} -> {'成功' if ok else '失败 ' + out[:200]}")


def heal(badges: list) -> None:
    if not get_flag("selfheal", True):
        return
    b = {x["key"]: x for x in badges}
    if b["vector"]["state"] == "down":
        _try_heal("vector", ["docker", "start", "vector"], "docker start（容器未运行）")
    elif b["vector"]["state"] == "warn":
        _try_heal("vector", ["docker", "restart", "vector"],
                  "docker restart（容器 Up 但 10 分钟零入库）")
    if b["victorialogs"]["state"] == "down":
        _try_heal("victorialogs", ["docker", "start", "victorialogs"], "docker start（/health 不可达）")
    if b["cobian"]["state"] == "down":
        _try_heal("cobian", ["systemctl", "start", "cobian-log-collector.timer"], "start timer")
    elif b["cobian"]["state"] == "warn" and "SMB 挂载在" in b["cobian"]["detail"]:
        _try_heal("cobian", ["systemctl", "start", "cobian-log-collector.service"],
                  "触发一次采集（上次运行失败）")


def status() -> list:
    """页面用：60s 缓存的徽章数据。"""
    if _cache["badges"] is None or time.time() - _cache["ts"] > 60:
        _cache["badges"] = probe()
        _cache["ts"] = time.time()
    return _cache["badges"]


async def watchdog() -> None:
    """常驻循环：探测 + 必要时自愈。异常不退出。"""
    while True:
        try:
            badges = probe()
            _cache["badges"], _cache["ts"] = badges, time.time()
            heal(badges)
        except Exception as e:
            _log_heal(f"watchdog 异常: {e}")
        await asyncio.sleep(CHECK_INTERVAL)
