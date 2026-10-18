"""备份状态：.98 rclone-backup / .28 Cobian / .79 Cobian + 每日巡检。

数据全部来自 VictoriaLogs，不另建库：

| 源 | 查询 | 判定 |
|---|---|---|
| .98 ls-bak08 | `app_name:rclone-backup` | `BACKUP_COMPLETE exit_code=0` 成功；`BACKUP_ERROR` 失败 |
| .28 | `app_name:cobian-backup` + `log_source:"192.168.0.28"` | 任务汇总行 `任务“…”备份已完成。错误数：N` |
| .79 | `app_name:cobian-backup` + `log_source:"192.168.0.79"` | 没有汇总行，按错误明细行判断 |
| 巡检 | `app_name:bak-status` | 每天一次的「全绿 x/y」或「发现 N 项失败」 |

**时间**：VL 的 `_time` 是 UTC。rclone-backup 在 2026-10-18T10:16Z 之前被多减了
8 小时（vector 双重时区校正，已修），展示时按 FIX_TS 分段补回。统一按 UTC+8。
"""
import calendar
import re
import time

from . import vl

# rclone_prep 去掉 -28800 的上线时刻（2026-10-18T10:16:00Z）
FIX_TS = calendar.timegm(time.strptime("2026-10-18T10:16:00", "%Y-%m-%dT%H:%M:%S"))

CACHE_TTL = 60
_cache = {"ts": 0.0, "data": None}

# Cobian 错误明细的关键词：只取真正的备份失败，排除「检查新版本」「文件已删除」
_COB_ERR = ('_msg:~"无法创建目录" OR _msg:~"无法复制文件" OR '
            '_msg:~"拒绝访问" OR _msg:~"I/O 设备错误" OR '
            '_msg:~"网络错误" OR _msg:~"来源目录"')
_COB_SUM_RE = re.compile(r'任务“([^”]+)”.*?错误数[：:]\s*(\d+)')
_INSPECT_OK_RE = re.compile(r'备份巡检全绿:\s*(\d+)/(\d+)')
_INSPECT_NG_RE = re.compile(r'备份巡检发现\s*(\d+)\s*项失败')


def _epoch(iso: str) -> int:
    try:
        return calendar.timegm(time.strptime((iso or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0


def _local(iso: str, app: str = "") -> str:
    """VL 的 _time（UTC）→ 本地时间字符串（UTC+8）。"""
    ep = _epoch(iso)
    if not ep:
        return (iso or "")[:16].replace("T", " ")
    if app == "rclone-backup" and ep < FIX_TS:
        ep += 8 * 3600          # 修复前的双重校正
    return time.strftime("%m-%d %H:%M", time.gmtime(ep + 8 * 3600))


def _q(expr: str, limit: int, days: int = 30):
    now = int(time.time())
    return vl.query_logs(expr, limit=limit, start=now - days * 86400, end=now)


def _dedupe(rows, key=lambda r: (r.get("_time"), r.get("_msg"))):
    """同一事件重复入库（实测 9 月那批 BACKUP_ERROR 每条两份）。"""
    out, seen = [], set()
    for r in rows:
        k = key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# ---------------- .98 rclone-backup ----------------

def rclone_job() -> dict:
    rows = _q('app_name:rclone-backup AND (_msg:~"BACKUP_" OR _msg:~"SYNC_ERROR")', 500)
    rows = _dedupe(rows)
    rows.sort(key=lambda r: r.get("_time") or "")

    done = [r for r in rows if (r.get("_msg") or "").startswith("BACKUP_COMPLETE")]
    errs = [r for r in rows if (r.get("_msg") or "").startswith("BACKUP_ERROR")]
    sync_err = [r for r in rows if (r.get("_msg") or "").startswith("SYNC_ERROR")]

    job = {"key": "98", "title": "ls-bak08 · rclone → tank/79-nfs",
           "state": "none", "last": "", "summary": "", "note": "", "errors": []}
    last = (done + errs)
    last.sort(key=lambda r: r.get("_time") or "")
    if last:
        r = last[-1]
        msg = (r.get("_msg") or "").strip()
        job["last"] = _local(r.get("_time"), "rclone-backup")
        job["summary"] = msg
        job["state"] = "ok" if msg.startswith("BACKUP_COMPLETE") else "err"
    job["note"] = f"近 30 天：{len(done)} 成功 / {len(errs)} 失败"
    for r in errs[-5:]:
        job["errors"].append({"ts": _local(r.get("_time"), "rclone-backup"),
                              "text": (r.get("_msg") or "").strip()})
    for r in sync_err[-3:]:
        job["errors"].append({"ts": _local(r.get("_time"), "rclone-backup"),
                              "text": (r.get("_msg") or "").strip()})
    return job


# ---------------- .28 / .79 Cobian ----------------

def cobian_jobs() -> list:
    sums = _q('app_name:cobian-backup AND _msg:~"备份已完成" AND _msg:~"错误数"', 400)
    sums = _dedupe(sums, key=lambda r: (r.get("log_source"), r.get("_time"),
                                        re.sub(r'[“”].*?[“”]', '', r.get("_msg") or "")))
    errs = _q(f'app_name:cobian-backup AND ({_COB_ERR})', 400)
    errs = _dedupe(errs)

    by_src = {}
    for r in sums:
        src = r.get("log_source") or "?"
        m = _COB_SUM_RE.search(r.get("_msg") or "")
        if not m:
            continue
        task, n = m.group(1), int(m.group(2))
        cur = by_src.setdefault(src, {"tasks": {}, "errors": []})
        ent = cur["tasks"].get(task)
        ts = r.get("_time") or ""
        if not ent or ts > ent["ts"]:
            cur["tasks"][task] = {"ts": ts, "n": n}
    for r in errs:
        src = r.get("log_source") or "?"
        by_src.setdefault(src, {"tasks": {}, "errors": []})["errors"].append(r)

    jobs = []
    for src, title in (("192.168.0.28", ".28 · Cobian · 79备份到 D:\\文件备份"),
                       ("192.168.0.79", ".79 · Cobian · \\\\192.168.0.79\\yzbak")):
        cur = by_src.get(src) or {"tasks": {}, "errors": []}
        job = {"key": src.rsplit(".", 1)[-1], "title": title, "state": "none",
               "last": "", "summary": "", "note": "", "errors": []}
        if cur["tasks"]:
            task, ent = sorted(cur["tasks"].items(), key=lambda kv: kv[1]["ts"])[-1]
            job["last"] = _local(ent["ts"], "cobian-backup")
            job["summary"] = f"任务“{task}” · 错误数 {ent['n']}"
            job["state"] = "ok" if ent["n"] == 0 else "err"
            job["note"] = f"共 {len(cur['tasks'])} 个任务有汇总"
        else:
            job["note"] = "无任务汇总行，按错误明细判断"
        es = sorted(cur["errors"], key=lambda r: r.get("_time") or "")
        job["note"] += f" · 近 30 天错误明细 {len(es)} 条"
        if es:
            newest = _epoch(es[-1].get("_time"))
            job["last"] = job["last"] or _local(es[-1].get("_time"), "cobian-backup")
            if job["state"] == "none":
                # 没有汇总行：最近 24 小时有错误才报警，否则视为历史噪音
                job["state"] = "warn" if time.time() - newest < 86400 else "ok"
        for r in es[-5:]:
            job["errors"].append({"ts": _local(r.get("_time"), "cobian-backup"),
                                  "text": (r.get("_msg") or "").strip()})
        jobs.append(job)
    return jobs


# ---------------- 每日巡检 ----------------

def inspect() -> dict:
    rows = _q('app_name:bak-status AND _msg:~"备份巡检"', 30)
    rows.sort(key=lambda r: r.get("_time") or "")
    out = {"ok": None, "last": "", "text": "暂无巡检记录", "failures": []}
    if rows:
        r = rows[-1]
        msg = (r.get("_msg") or "").strip()
        out["last"] = _local(r.get("_time"), "bak-status")
        out["text"] = msg
        if _INSPECT_OK_RE.search(msg):
            out["ok"] = True
        elif _INSPECT_NG_RE.search(msg):
            out["ok"] = False
    # 失败项明细（与巡检同一批）
    fails = _q('app_name:bak-status AND _msg:~"失败项"', 40)
    if fails and rows:
        newest = rows[-1].get("_time") or ""
        for r in fails:
            if abs(_epoch(r.get("_time")) - _epoch(newest)) <= 3600:
                out["failures"].append((r.get("_msg") or "").strip())
    return out


# ---------------- 汇总 ----------------

def status() -> dict:
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    try:
        jobs = [rclone_job()] + cobian_jobs()
    except Exception:
        jobs = []
    try:
        insp = inspect()
    except Exception:
        insp = {"ok": None, "last": "", "text": "巡检数据读取失败", "failures": []}

    events = []
    for j in jobs:
        for e in j.get("errors") or []:
            events.append({"source": j["title"].split(" · ")[0], "ts": e["ts"],
                           "level": "err" if j["state"] == "err" else "warn",
                           "text": e["text"]})
    events.sort(key=lambda e: e["ts"], reverse=True)

    data = {"updated": time.strftime("%m-%d %H:%M"), "inspect": insp,
            "jobs": jobs, "events": events[:40]}
    _cache["data"], _cache["ts"] = data, now
    return data
