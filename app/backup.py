"""备份状态：.98 rclone / .28 rclone / .79 ABB / .16 磁带 + 每日巡检。

数据全部来自 VictoriaLogs，不另建库：

| 源 | 查询 | 判定 |
|---|---|---|
| .98 ls-bak08 | `app_name:rclone-backup` | `BACKUP_COMPLETE exit_code=0` 成功；`BACKUP_ERROR` 失败 |
| .28 | `app_name:rclone28` | 脚本跑完直推 `RCLONE_TOTAL`/`RCLONE_JOB` 结构化事件 |
| .79 | `app_name:ActiveBackup` | `was completed` 成功、`partially completed`/`canceled` 警告、`Backup path … failed` 失败路径 |
| .16 磁带 | `app_name:taf` | `🚀 备份任务开始` / `✅ 备份任务完成` + 磁带写入事件 |
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

# Cobian 已停用（.28 改用 rclone、.79 是 Synology ABB），相关解析正则一并删除
_INSPECT_OK_RE = re.compile(r'备份巡检全绿:\s*(\d+)/(\d+)')
_INSPECT_NG_RE = re.compile(r'备份巡检发现\s*(\d+)\s*项失败')
# 日志里的 ANSI 色码（与 disks.py 同一套）
_ANSI_RE = re.compile(r'(?:\x1b|#033)?\[\d+(?:;\d+)*m')


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
    rows = _q('app_name:rclone-backup AND (_msg:~"BACKUP_" OR _msg:~"SYNC_ERROR" OR '
              '_msg:~"SNAPSHOT_ERROR")', 500)
    rows = _dedupe(rows)
    rows.sort(key=lambda r: r.get("_time") or "")

    done = [r for r in rows if (r.get("_msg") or "").startswith("BACKUP_COMPLETE")]
    errs = [r for r in rows
            if (r.get("_msg") or "").startswith(
                ("BACKUP_ERROR", "SYNC_ERROR", "SNAPSHOT_ERROR"))]

    job = {"key": "98", "title": "ls-bak08 · rclone → tank/79-nfs",
           "state": "none", "last": "", "summary": "", "note": "", "errors": []}
    last = (done + errs)
    # 同一秒内：错误排在完成之后 —— 快照失败时脚本会连发 SNAPSHOT_ERROR
    # 和 BACKUP_COMPLETE，按错误算（宁可报红，不藏问题）
    last.sort(key=lambda r: (r.get("_time") or "",
                             0 if (r.get("_msg") or "").startswith("BACKUP_COMPLETE") else 1))
    if last:
        r = last[-1]
        msg = (r.get("_msg") or "").strip()
        job["last"] = _local(r.get("_time"), "rclone-backup")
        job["summary"] = msg
        job["state"] = "ok" if msg.startswith("BACKUP_COMPLETE") else "err"
    job["note"] = f"近 30 天：{len(done)} 成功 / {len(errs)} 条错误事件"
    for r in errs[-6:]:
        job["errors"].append({"ts": _local(r.get("_time"), "rclone-backup"),
                              "text": (r.get("_msg") or "").strip()})
    return job


# ---------------- .28 rclone（脚本跑完直推结构化事件） ----------------

_RCLONE28_OK = ("ok", "success", "0", "true")
_TAF_MSG_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} - [\w.]+ - \w+ - [\w.:]+ - ')


def _num(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _fmt_dur(sec: int) -> str:
    sec = max(0, int(sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def _strip_prefix(msg: str) -> str:
    """去掉 TAF 日志行的 '时间 - logger - LEVEL - 函数 - ' 前缀。"""
    return _TAF_MSG_RE.sub("", (msg or "").strip())


def rclone28_job() -> dict:
    """`.28` 的 rclone 任务。脚本每跑完一个源推一行 `RCLONE_JOB`，
    全部跑完再推一行 `RCLONE_TOTAL`（见 C:\\opt\\rclone\\backup-79.ps1）。"""
    rows = _q('app_name:rclone28 AND (_msg:~"RCLONE_JOB" OR _msg:~"RCLONE_TOTAL")', 400)
    rows = _dedupe(rows)
    rows.sort(key=lambda r: r.get("_time") or "")

    job = {"key": "28", "title": ".28 · rclone · 79 共享 → D:\\文件备份",
           "state": "none", "last": "", "summary": "", "note": "", "errors": []}

    totals = [r for r in rows if (r.get("_msg") or "").startswith("RCLONE_TOTAL")]
    jobs = [r for r in rows if (r.get("_msg") or "").startswith("RCLONE_JOB")]

    if totals:
        r = totals[-1]
        failed = _num(r.get("failed"))
        job["last"] = _local(r.get("_time"))
        job["state"] = "ok" if failed == 0 else "err"
        job["summary"] = (
            f"{_num(r.get('jobs'))} 个源 · 复制 {_num(r.get('copied'))} 个 / {r.get('bytes_h') or '—'}"
            f" · 删除 {_num(r.get('deleted'))} 个"
            f" · 耗时 {_fmt_dur(_num(r.get('elapsed_s')))}"
            f" · 错误 {_num(r.get('errors'))}"
        )
    elif jobs:
        r = jobs[-1]
        job["last"] = _local(r.get("_time"))
        job["state"] = "ok" if (r.get("state") or "ok") in _RCLONE28_OK else "err"
        job["summary"] = _strip_prefix(r.get("_msg") or "")[:160]

    ok_runs = sum(1 for r in totals if _num(r.get("failed")) == 0)
    bad_runs = sum(1 for r in totals if _num(r.get("failed")) > 0)
    job["note"] = (f"近 30 天：{ok_runs} 次完成 / {bad_runs} 次有失败"
                   if rows else "还没有事件（脚本下次运行后开始上报）")

    errs = [r for r in rows if (r.get("level") or "").lower() in ("err", "error")]
    for r in errs[-5:]:
        job["errors"].append({
            "ts": _local(r.get("_time")),
            "text": _strip_prefix(r.get("err_sample") or r.get("_msg") or "")[:200],
        })
    if errs:
        job["last_error"] = _local(errs[-1].get("_time"))
    return job


# ---------------- .16 磁带备份（TAF） ----------------

_TAF_NOISE = re.compile(r'opengauss', re.I)
_TAF_SIZE_RE = re.compile(r'大小[:：]\s*([\d.]+)\s*([KMGT]B)', re.I)
_TAF_UNIT = {"KB": 1e-6, "MB": 1e-3, "GB": 1.0, "TB": 1e3}


def taf_job() -> dict:
    """`.16` TAF 磁带备份。

    生命周期用钉钉通知器的「备份任务开始/完成」（量少、可靠）；
    磁带写入单独查、只统计「最近一次任务」窗口内的事件，并按
    （分钟 + 文件名）去重 —— TAF 每条日志会入库两份。
    """
    job = {"key": "taf", "title": ".16 taf · 磁带备份（LTFS）", "state": "none",
           "last": "", "summary": "", "note": "", "errors": []}

    life = _q('app_name:taf AND (_msg:~"备份任务开始" OR _msg:~"备份任务完成")', 200)
    life = _dedupe(life)
    life.sort(key=lambda r: r.get("_time") or "")
    starts = [r for r in life if "备份任务开始" in (r.get("_msg") or "")]
    dones = [r for r in life if "备份任务完成" in (r.get("_msg") or "")]

    last_done = dones[-1] if dones else None
    last_start = starts[-1] if starts else None
    running = bool(
        last_start and (not last_done
                        or (last_start.get("_time") or "") > (last_done.get("_time") or ""))
    )

    win_from = ""
    if running:
        win_from = last_start.get("_time") or ""
        job["state"] = "run"
        job["last"] = _local(win_from)
        job["summary"] = (f"备份任务进行中 · 已运行 "
                          f"{_fmt_dur(time.time() - _epoch(win_from))}")
    elif last_done:
        job["state"] = "ok"
        job["last"] = _local(last_done.get("_time"))
        prev = [r for r in starts
                if (r.get("_time") or "") <= (last_done.get("_time") or "")]
        if prev:
            win_from = prev[-1].get("_time") or ""
            job["summary"] = ("备份完成 · 耗时 "
                              + _fmt_dur(_epoch(last_done.get("_time")) - _epoch(win_from)))
        else:
            job["summary"] = "备份完成"

    # 磁带写入：最近一次任务窗口内，按（分钟 + 文件名）去重
    raw = _q('app_name:taf AND _msg:~"开始移动文件到磁带"', 1000)
    seen, tapes = set(), []
    for r in raw:
        ts = r.get("_time") or ""
        if win_from and ts < win_from:
            continue
        msg = r.get("_msg") or ""
        fm = re.search(r'移动文件到磁带:\s*(\S+)', msg)
        key = (ts[:16], fm.group(1) if fm else msg[:60])
        if key in seen:
            continue
        seen.add(key)
        tapes.append(r)
    tapes.reverse()

    written_gb = 0.0
    for r in tapes:
        m = _TAF_SIZE_RE.search(r.get("_msg") or "")
        if m:
            written_gb += float(m.group(1)) * _TAF_UNIT.get(m.group(2).upper(), 0)

    n_done_30d = vl.stats_total('app_name:taf AND _msg:~"备份任务完成"', days=30)
    n_tape_30d = vl.stats_total('app_name:taf AND _msg:~"开始移动文件到磁带"', days=30)
    note = (f"最近一次：写入 {len(tapes)} 个包 / {written_gb:.1f} GB"
            f" · 近 30 天完成 {n_done_30d} 次 / 磁带事件 {n_tape_30d} 条")

    # 错误：统计用 stats（不受 limit 截断），明细取样本
    err_total = vl.stats_total(
        'app_name:taf AND level:error AND NOT _msg:~"openGauss"', days=30)
    errs = _q('app_name:taf AND level:error', 400)
    errs = [r for r in errs
            if not _TAF_NOISE.search((r.get("_msg") or "") + (r.get("logger") or ""))]
    errs = _dedupe(errs)
    errs.sort(key=lambda r: r.get("_time") or "")
    if errs or err_total:
        note += f" · 近 30 天错误 {err_total} 条"
        if errs:
            job["last_error"] = _local(errs[-1].get("_time"))
            if job["state"] == "ok" and time.time() - _epoch(errs[-1].get("_time")) < 86400:
                job["state"] = "warn"
            for r in errs[-5:]:
                job["errors"].append({"ts": _local(r.get("_time")),
                                      "text": _strip_prefix(r.get("_msg") or "")[:200]})
    job["note"] = note
    return job


# ---------------- .79 Synology ABB（Active Backup for Business） ----------------

_ABB_TASK_RE = re.compile(
    r'^\[([^\]]+)\]\s+The backup task \1 was (completed|partially completed|canceled)\.')
_ABB_ERR_RE = re.compile(r'failed\.\((.+)\)\s*$')


def abb_job() -> dict:
    """`.79` 的 ABB 任务级结果 + 失败路径聚合。

    日志形如（Synology 直接发 syslog，_time 已是 UTC）：
        [192.168.0.101] The backup task 192.168.0.101 was completed.
        [192.168.0.28-yzbak] The backup task ... was partially completed.
        Backup path /data/...pdf failed.(Filename is too long.)
    """
    tasks = _q('app_name:ActiveBackup AND (_msg:~"was completed" OR '
               '_msg:~"partially completed" OR _msg:~"was canceled")', 800)
    tasks = _dedupe(tasks)
    tasks.sort(key=lambda r: r.get("_time") or "")

    job = {"key": "79", "title": ".79 · Synology ABB（Active Backup）",
           "state": "none", "last": "", "summary": "", "note": "", "errors": []}

    done, part, cancel = {}, {}, {}
    for r in tasks:
        m = _ABB_TASK_RE.search((r.get("_msg") or "").strip())
        if not m:
            continue
        name, verdict = m.group(1), m.group(2)
        bucket = {"completed": done, "partially completed": part, "canceled": cancel}[verdict]
        bucket[name] = r

    verdict_cn = {"completed": "完成", "partially completed": "部分完成",
                  "canceled": "已取消"}
    if tasks:
        last = tasks[-1]
        job["last"] = _local(last.get("_time"))
        lm = _ABB_TASK_RE.search((last.get("_msg") or "").strip())
        if lm:
            job["summary"] = f"最近：{lm.group(1)} {verdict_cn[lm.group(2)]}"
        else:
            job["summary"] = _strip_prefix(last.get("_msg") or "")[:140]

    # 近 24 小时的任务级结果决定颜色
    now = time.time()
    recent = [r for r in tasks if now - _epoch(r.get("_time")) < 86400]
    bad_recent = [r for r in recent
                  if "partially completed" in (r.get("_msg") or "")
                  or "was canceled" in (r.get("_msg") or "")]
    if recent:
        job["state"] = "warn" if bad_recent else "ok"
    if bad_recent:
        job["state"] = "warn"

    # 失败路径（按原因聚合，避免几百条刷屏）
    err_total = vl.stats_total(
        'app_name:ActiveBackup AND level:error AND _msg:~"Backup path"', days=30)
    errs = _q('app_name:ActiveBackup AND level:error AND _msg:~"Backup path"', 400)
    errs = _dedupe(errs)
    reasons = {}
    for r in errs:
        m = _ABB_ERR_RE.search((r.get("_msg") or "").strip())
        reason = (m.group(1).strip() if m else "其它错误")[:60]
        reasons[reason] = reasons.get(reason, 0) + 1
    top = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)[:3]

    job["note"] = (f"近 30 天：完成 {sum(1 for _ in done)} 个任务 · "
                   f"部分完成 {sum(1 for _ in part)} · 取消 {sum(1 for _ in cancel)}"
                   f" · 失败路径 {err_total} 条")
    if top:
        job["note"] += "（主因：" + "、".join(k for k, _ in top) + "）"
    errs_24h = [r for r in errs if now - _epoch(r.get("_time")) < 86400]
    if errs_24h:
        job["last_error"] = _local(errs_24h[-1].get("_time"))

    # 卡片里只放一条汇总（原因可能上千条，不能逐条列）
    if err_total:
        sample = errs[-1] if errs else None
        path = ""
        if sample:
            pm = re.search(r'Backup path (\S+) failed', sample.get("_msg") or "")
            if pm:
                path = pm.group(1)
        top_txt = ("，主因：" + top[0][0]) if top else ""
        job["errors"].append({
            "ts": _local((sample or {}).get("_time")),
            "text": f"近 30 天 {err_total} 条失败路径{top_txt}"
                    + (f" · 例：…{path[-80:]}" if path else ""),
        })
    return job


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


# ---------------- 全部警告以上日志 ----------------

# 每个源：显示名 + 基础查询 + 条数上限
_LOG_SOURCES = {
    "98": ("98 · ls-bak08", 'app_name:rclone-backup AND '
                            '(level:error OR _msg:~"BACKUP_ERROR" OR _msg:~"SYNC_ERROR" OR '
                            '_msg:~"SNAPSHOT_ERROR")', 600),
    "28": ("28 · rclone", 'app_name:rclone28 AND level:error', 600),
    "79": ("79 · ABB", 'app_name:ActiveBackup AND (level:error OR level:warning)', 600),
    "taf": ("16 · 磁带", 'app_name:taf AND (level:error OR level:warning OR _msg:~"磁带")', 800),
    "inspect": ("巡检", 'app_name:bak-status AND (level:error OR _msg:~"失败")', 200),
}

_LOG_NOISE = re.compile(r'检查新版本|已删除，因为原始文件不存在|opengauss', re.I)
_LOG_ERR = re.compile(
    r'BACKUP_ERROR|SYNC_ERROR|SNAPSHOT_ERROR|无法创建目录|无法复制文件|拒绝访问|I/O 设备错误|'
    r'来源目录.*不存在|网络错误|失败项|发现\s*\d+\s*项失败|错误数[：:]\s*[1-9]', re.I)
_LOG_WARN = re.compile(r'another_run_in_progress|速度极慢|卡顿', re.I)


def _classify(msg: str, level: str, source: str) -> str:
    if _LOG_NOISE.search(msg or ""):
        return ""
    if source == "inspect" and "全绿" in (msg or ""):
        return ""                       # 巡检全绿是 level=warning，但不是问题
    if _LOG_ERR.search(msg or "") or (level or "").lower() in (
            "err", "error", "crit", "critical", "alert", "emerg"):
        return "err"
    if _LOG_WARN.search(msg or "") or (level or "").lower() in ("warn", "warning"):
        return "warn"
    return ""


def logs(days: int = 30, source: str = "", level: str = "") -> dict:
    """备份相关源的「警告及以上」日志。source/level 为空表示不过滤。"""
    try:
        days = max(1, min(int(days or 30), 180))
    except (TypeError, ValueError):
        days = 30
    keys = [source] if source in _LOG_SOURCES else list(_LOG_SOURCES)
    rows, seen = [], set()
    for key in keys:
        label, expr, limit = _LOG_SOURCES[key]
        for r in _q(expr, limit, days):
            msg = _ANSI_RE.sub("", r.get("_msg") or "").strip()
            if not msg:
                continue
            lv = _classify(msg, r.get("level") or "", key)
            if not lv or (level and lv != level):
                continue
            ep = _epoch(r.get("_time"))
            if key == "98" and ep and ep < FIX_TS:
                ep += 8 * 3600          # 修复前的双重时区校正
            k = (key, ep, msg)
            if k in seen:
                continue
            seen.add(k)
            rows.append({"epoch": ep, "ts": _local(r.get("_time"), "rclone-backup" if key == "98" else ""),
                         "source": label, "skey": key, "level": lv, "text": msg})
    rows.sort(key=lambda x: x["epoch"], reverse=True)
    return {"rows": rows[:500], "total": len(rows), "days": days,
            "source": source if source in _LOG_SOURCES else "",
            "level": level if level in ("warn", "err") else ""}


# ---------------- 汇总 ----------------

def status() -> dict:
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    # 每张卡片独立读：一个源查询失败不影响其它卡片
    jobs = []
    for fn in (rclone_job, rclone28_job, abb_job, taf_job):
        try:
            jobs.append(fn())
        except Exception:
            jobs.append({"key": "?",
                         "title": getattr(fn, "__name__", "?"),
                         "state": "none", "last": "", "summary": "读取失败",
                         "note": "该源数据读取异常", "errors": []})
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
