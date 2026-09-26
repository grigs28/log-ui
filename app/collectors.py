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
ZOMBIE_WINDOWS = 3       # 连续多少个零流量窗口才判 vector 僵尸（低峰期单窗口零流量属正常）

_cache = {"ts": 0.0, "badges": None}     # status() 供页面用的 60s 缓存
_last_heal: dict[str, float] = {}
# 连续零流量窗口计数：n=已累计窗口数，ts=上次计数的时刻（保证每个窗口最多计一次，
# 否则页面/巡检频繁调用 probe() 会把同一次零流量重复计数，误判僵尸）
_zero_streak = {"n": 0, "ts": 0.0}


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


def _mounted(path: str = "/mnt/cobian-logs") -> bool:
    """读 /proc/mounts 判断，而不是 os.path.ismount()/stat——
    僵死的 CIFS 挂载上 stat 会无限期阻塞，拖死 watchdog 与页面渲染
    （2026-09-26 实际发生：挂载表有条目但目录访问挂起）。
    挂载是否「可用」由采集器退出码反映（Result=failed）。"""
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            return any((ln.split()[1] if len(ln.split()) > 1 else "") == path
                       for ln in f)
    except Exception:
        return False


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
    note = ""
    if not running:
        state, css = "down", "off"
        _zero_streak["n"] = 0
    elif flow is None:
        state, css = "unknown", "off"     # VL 不可达，无法判流量
        _zero_streak["n"] = 0
    elif flow == 0:
        # 单窗口零流量在低峰期属正常（v3-ctr01 拒包风暴停止后基线降到 ~150 条/小时）。
        # 每个窗口最多计一次，累计满 ZOMBIE_WINDOWS 个才判僵尸并触发重启。
        now = time.time()
        if now - _zero_streak["ts"] >= FLOW_WINDOW_MIN * 60:
            _zero_streak["n"] += 1
            _zero_streak["ts"] = now
        if _zero_streak["n"] >= ZOMBIE_WINDOWS:
            state, css = "warn", "on"
            note = f"（已连续 {_zero_streak['n']} 个窗口零入库）"
        else:
            state, css = "quiet", "on"
            note = f"（零入库观察中 {_zero_streak['n']}/{ZOMBIE_WINDOWS}）"
    else:
        _zero_streak["n"] = 0
        state, css = "ok", "on"
    badges.append({"key": "vector", "label": "vector", "state": state, "css": css,
                   "detail": f"容器{'Up' if running else '未运行'}，{FLOW_WINDOW_MIN}分钟入库 "
                             f"{'?' if flow is None else flow} 条{note}"})

    # ---- cobian ----
    ok_t, _ = _sh(["systemctl", "is-active", "cobian-log-collector.timer"])
    _, result = _sh(["systemctl", "show", "cobian-log-collector.service",
                     "--property=Result", "--value"])
    mounted = _mounted()
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
                  f"docker restart（连续 {ZOMBIE_WINDOWS} 个窗口约 "
                  f"{ZOMBIE_WINDOWS * FLOW_WINDOW_MIN} 分钟零入库）")
    # state == "quiet"（单窗口零流量，低峰常态）不触发任何动作
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
