"""巡检健康度：三个 poll 最近一次「[结束]」是什么时候。

背景：`osd.poll` / `net.poll` / `hw.poll` 都只在 `.91` 单点跑，
cron 失效或 .91 出问题时巡检会**静默中断**——看板一直是绿的，没人会发现。

这里做两件事，各管一半：
  · 本模块（页面侧）：**直接从 VL 算**最近一次 [结束] 距今多久，页面顶部亮灯。
    不依赖心跳脚本，页面一打开就能看到。
  · `scripts/poll_heartbeat.sh`（cron 侧）：每小时检查一次，超时写 `app_name:poll-heartbeat`
    的 err 事件，供告警链路使用。

判据（与脚本一致）：
    有数据但 > 26h 没结束   → err（中断）
    有数据但 > 14h 没结束   → warn（可能漏跑一轮；osd 间隔 10h、net 间隔 12h）
    30 天内从没出现过        → none（未上线，不告警）
"""
import re
import time

from . import vl

JOBS = [
    ("osd-poll", "磁盘巡检", "03:33 / 13:33"),
    ("net-poll", "网卡巡检", "04:33 / 16:33"),
    ("hw-poll", "整机巡检", "未上线"),
]

WARN_H, ERR_H = 14, 26
_CACHE = {"ts": 0.0, "data": None}
_TTL = 60


def _epoch(iso):
    if not iso:
        return 0
    s = str(iso).strip().replace("T", " ").replace("Z", "")[:19]
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0


def _last_end(app):
    rows = vl.query_logs(expr='app_name:%s AND _msg:~"[结束]"' % app, limit=1)
    if not rows:
        return "", 0
    row = rows[0]
    iso = row.get("_time") or ""
    m = re.search(r'\[结束\]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', row.get("_msg") or "")
    # 优先用日志正文里的时间（那是节点本地时间，比入库时间更贴近真实结束时刻）
    if m:
        return m.group(1), _epoch(m.group(1))
    return iso, _epoch(iso)


def status():
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["ts"] < _TTL:
        return _CACHE["data"]

    out = []
    for app, title, schedule in JOBS:
        try:
            last, ep = _last_end(app)
        except Exception:
            last, ep = "", 0
        if not ep:
            age_h, state = None, "none"
        else:
            age_h = int((now - ep) // 3600)
            state = "err" if age_h >= ERR_H else ("warn" if age_h >= WARN_H else "ok")
        out.append({"app": app, "title": title, "schedule": schedule,
                    "last": last[5:16] if last else "",
                    "last_full": last, "age_h": age_h, "state": state,
                    "label": {"ok": "正常", "warn": "漏跑", "err": "中断",
                              "none": "未上线"}[state]})

    worst = "err" if any(j["state"] == "err" for j in out) else (
        "warn" if any(j["state"] == "warn" for j in out) else
        ("ok" if any(j["state"] == "ok" for j in out) else "none"))
    data = {"jobs": out, "worst": worst}
    _CACHE["data"], _CACHE["ts"] = data, now
    return data
