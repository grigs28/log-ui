"""自动告警：磁盘新异常 / 采集器掉线 / 每日汇总，经钉钉发出。

由 `collectors.watchdog()` 每分钟调一次 `tick()`（它已经在跑常驻循环了）。

三个触发器都是**边沿触发 + 去重**，绝不重复刷屏：

| 触发器 | 什么时候发 | 去重方式 |
|---|---|---|
| 磁盘新异常 | 某块盘从「不异常」变成「异常」 | 记下报过的 host+设备；**恢复了就移出**，下次再坏会重新报 |
| 采集器掉线 | vector/cobian/victorialogs 从「好」变成「坏」 | 记下上次状态，只在**状态翻转**时发 |
| 每日汇总 | 每天到点发一条概览 | 记下发过的日期，一天只发一次 |

**恢复也会报** —— 「好了」和「坏了」一样重要，不然你不知道要不要再去机房。

状态存 `config/notify_state.json`。总开关是 `notify.enabled`（设置页可关，
关掉后这里一次都不发）。
"""
import json
import time
from pathlib import Path

from . import notify, vl

STATE = Path(__file__).resolve().parent.parent / "config" / "notify_state.json"
COLLECTORS = ("vector", "cobian", "victorialogs")   # 与 collectors.probe() 的 key 一致


def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _cfg() -> dict:
    from .config import get_notify
    return get_notify()


def _on(event: str) -> bool:
    """总开关 + 该事件的独立开关都开着才发。"""
    c = _cfg()
    if not c.get("enabled"):
        return False
    ev = c.get("events") or {}
    return bool(ev.get(event, True))


def enabled_for(event: str) -> bool:
    """给设置页显示用。"""
    return _on(event)


# ---------------- 触发器 1：磁盘新异常 ----------------

def _disk_items() -> dict:
    """当前所有异常盘 → {'host|/dev/sdX': (主机, 设备, 摘要)}。"""
    from . import disks
    out = {}
    try:
        data = disks.merged()
    except Exception:
        return out
    for s in data.get("servers", []):
        for x in s.get("disks", []):
            if x.get("state") != "err" or not x.get("dev"):
                continue
            out[f'{s["name"]}|{x["dev"]}'] = (s["name"], x["dev"],
                                             (x.get("status") or "异常")[:120])
    return out


def _check_disks(st: dict) -> None:
    if not _on("disk_anomaly"):
        return
    cur = _disk_items()
    known = st.setdefault("disks", {})
    fresh = [k for k in cur if k not in known]
    back = [k for k in list(known) if k not in cur]

    for k in back:                       # 恢复了 → 移出名单，下次再坏会重新报
        known.pop(k, None)

    if not fresh and not back:
        return

    blocks = []
    if fresh:
        b = [f"### ✗ 新发现 {len(fresh)} 块异常盘"]
        for k in fresh:
            host, dev, issue = cur[k]
            b.append(f"- **{host}** `{dev}` — {issue}")
            known[k] = {"ts": time.time(), "issue": issue}
        blocks.append("\n".join(b))
    if back:
        b = [f"### ✓ {len(back)} 块盘已恢复"]
        for k in back:
            host, dev, _ = known.get(k, (k.split("|")[0], k.split("|")[-1], ""))
            b.append(f"- **{host}** `{dev}`")
        blocks.append("\n".join(b))

    n = len(fresh)
    title = (f"[磁盘] {n} 块新异常" if n else f"[磁盘] {len(back)} 块已恢复")
    if n and back:
        title = f"[磁盘] {n} 块新异常 / {len(back)} 块恢复"
    notify.send(title, "\n\n".join(blocks))


# ---------------- 触发器 2：采集器掉线 ----------------

def _check_collectors(badges: list, st: dict) -> None:
    if not _on("collector_down"):
        return
    known = st.setdefault("collectors", {})
    blocks, down, up = [], [], []
    for b in badges:
        k = b.get("key")
        if k not in COLLECTORS:
            continue
        bad = b.get("state") in ("down", "zombie")
        was = known.get(k)
        if was is None:                  # 第一次见：只记基线，不报（免得重启后刷一堆）
            known[k] = bad
            continue
        if bad and not was:
            down.append(f"- **{b.get('label')}** 掉线 — {b.get('detail', '')}")
        elif not bad and was:
            up.append(f"- **{b.get('label')}** 已恢复 — {b.get('detail', '')}")
        known[k] = bad
    if down:
        blocks.append("\n".join([f"### ✗ {len(down)} 个采集器掉线"] + down))
    if up:
        blocks.append("\n".join([f"### ✓ {len(up)} 个采集器恢复"] + up))
    if blocks:
        title = ("[采集器] 掉线" if down and not up else
                 "[采集器] 恢复" if up and not down else
                 "[采集器] 掉线 / 恢复")
        notify.send(title, "\n\n".join(blocks))


# ---------------- 触发器 3：每日汇总 ----------------

def _check_daily(st: dict) -> None:
    if not _on("daily_digest"):
        return
    c = _cfg()
    want = str(c.get("daily_at") or "10:00")
    today = time.strftime("%Y-%m-%d")
    if st.get("digest") == today:
        return
    if time.strftime("%H:%M") < want:     # 还没到点
        return

    from . import disks
    lines = ["### 日志", ""]
    try:
        total = vl.stats_total("*", 1)
        errs = vl.stats_total("level:error", 1)
        warns = vl.stats_total("level:warning", 1)
        lines.append(f"- 近 24 小时：**{total}** 条（error {errs} · warning {warns}）")
    except Exception:
        lines.append("- 日志量：统计失败")

    lines += ["", "### 磁盘", ""]
    try:
        data = disks.merged()
        stt = data.get("stats", {})
        bad = stt.get("bad", 0)
        lines.append(f"- **{stt.get('servers', 0)}** 台机器 · **{stt.get('disks', 0)}** 个盘位 · "
                     f"异常 **{bad}** 块" + ("　✅ 全部健康" if not bad else ""))
        for s in data.get("servers", []):
            if s.get("bad"):
                devs = [x["dev"] for x in s["disks"] if x.get("state") == "err"]
                lines.append(f"  - {s['name']}：{'、'.join(devs)}")
    except Exception:
        lines.append("- 磁盘：统计失败")

    lines += ["", "### 采集器", ""]
    try:
        from . import collectors
        for b in collectors.status():
            mark = "✗" if b.get("state") in ("down", "zombie") else "✓"
            lines.append(f"- {mark} **{b.get('label')}** — {b.get('detail', '')}")
    except Exception:
        lines.append("- 采集器：探测失败")

    bad = 0
    try:
        bad = disks.merged().get("stats", {}).get("bad", 0)
    except Exception:
        pass
    title = f"[每日汇总] {'⚠ ' + str(bad) + ' 块异常盘' if bad else '✅ 一切正常'}"
    r = notify.send(title, "\n".join(lines))
    if r.get("ok"):
        st["digest"] = today               # 只有真发出去了才记「今天发过」
        _save(st)


# ---------------- 入口 ----------------

def tick(badges: list = None) -> None:
    """由一个周期任务调用（watchdog 每分钟）。任何异常都不该影响调用方。"""
    if not _cfg().get("enabled"):
        return
    st = _load()
    try:
        if badges is None:
            from . import collectors
            badges = collectors.status()
        _check_collectors(badges, st)
        _check_disks(st)
        _check_daily(st)
    finally:
        _save(st)
