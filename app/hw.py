"""整机巡检（IPMI / EDAC / 系统层）→ 主机 × 一行的视图。

数据来源：`app_name:hw-poll`（`hw.check.sh` 本机巡检 + `hw.poll.sh` 在 .91 轮询），
与磁盘（`osd-poll`）、网卡（`net-poll`）同一条 syslog 通道，**不新增采集**。

约定格式（见 `/opt/loging/docs` 与 `gr` 侧设计文档第 10 节）：

    [盘点] <ts>  <host>  : ipmi=ok inlet=19C cpu=43C cpu_max=74C fan_min=4200RPM fan_n=8 \
                           psu=ok ecc_ce=0 ecc_ue=0 disk=OK disk_life=97 root=5% failed=0 ntp=0.3ms
    [异常] <ts>  <host>  : ✗ 风扇转速异常 FAN3 1200RPM | kind=fan item=FAN3
    [遥测] <ts>  <host>  : inlet=19C cpu=43C fan_min=4200 ecc_ce=0 ecc_ue=0
    [汇总] <ts>  <host>  : 9 项检查，1 处异常

**这个模块先按"空架子"实现**：脚本还没上线，VL 里一条 `hw-poll` 都没有，
页面照样能打开（显示"等待数据"），等 `[盘点]` 一到就自动出数。
解析规则刻意写得很宽松——认 key 不认列，脚本加字段不用改这里。
"""
import re
import time

from . import vl

_CACHE = {"ts": 0.0, "data": None}
_TTL = 120

_TAG_RE = re.compile(r'^\[(盘点|异常|遥测|汇总|开始|结束)\]\s*'
                     r'(?:(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+)?(.*)$')
_SRC_RE = re.compile(r'\s*\[来源\s+[^\]]*\]\s*$')
_IPV4_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')

# 异常 kind → 显示名（与设计文档 7.3 一致；未列出的原样显示）
KIND_NAMES = {
    "temp": "温度", "fan": "风扇", "psu": "电源", "ecc": "内存",
    "disk": "系统盘", "service": "服务", "ntp": "时间", "host": "主机",
    "ipmi": "IPMI",
}

# 判据阈值（与设计文档 A4 一致）：页面只做"显示层"着色，真正的告警由脚本发 [异常]
ROOT_WARN, ROOT_ERR = 85, 90
NTP_WARN, NTP_ERR = 100, 1000          # ms


def _epoch(s):
    if not s:
        return 0
    s = str(s).strip().replace("T", " ").replace("Z", "")
    s = s.split("+")[0].split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(s[:len(time.strftime(fmt))], fmt))
        except Exception:
            continue
    return 0


def _fmt_ts(ep):
    return time.strftime("%m-%d %H:%M", time.localtime(ep)) if ep else ""


def _kv(text):
    """'a=1 b=2C' → {'a':'1','b':'2C'}；不认位置，加字段不用改代码。"""
    out = {}
    for tok in (text or "").split():
        k, _, v = tok.partition("=")
        if k and v:
            out[k.strip()] = v.strip()
    return out


def _split_ext(text):
    """`… ✗ 某某 | kind=fan item=FAN3` → (正文, {kind,item})。

    `|` 分隔是约定；也容忍没有 `|` 的尾部 kv（上次 net 踩过，见 docs）。
    """
    m = re.search(r'\s\|\s(.*)$', text or "")
    if m:
        return text[:m.start()], _kv(m.group(1))
    return text or "", {}


def _split_host(rest):
    """'m3-nod16  : key=value …' → ('m3-nod16', 'key=value …')。"""
    if " : " in rest:
        host, body = rest.split(" : ", 1)
    elif ":" in rest:
        host, body = rest.split(":", 1)
    else:
        host, body = rest, ""
    host = host.strip()
    ip = ""
    m = re.match(r'^([^()\s]+)\(([^)]*)\)$', host)
    if m:
        host, ip = m.group(1), m.group(2).strip()
    return host, body.strip(), ip


def _events():
    logs = vl.query_logs(expr="app_name:hw-poll", limit=3000)
    evs = []
    for rec in logs:
        msg = (rec.get("_msg") or "").strip()
        m = _TAG_RE.match(msg)
        if not m:
            continue
        tag, ts, rest = m.group(1), (m.group(2) or ""), m.group(3).strip()
        rest = _SRC_RE.sub("", rest).strip()
        host, body, ip = _split_host(rest)
        ep = _epoch(ts) or _epoch(rec.get("_time"))
        ev = {"tag": tag, "ts": ts, "ep": ep, "host": host, "ip": ip,
              "body": body, "raw": msg, "ext": {}}
        if tag == "盘点":
            ev["kv"] = _kv(body)
        elif tag == "异常":
            text, ext = _split_ext(body)
            ev["text"] = text
            ev["ext"] = ext
        evs.append(ev)
    evs.sort(key=lambda e: e["ep"])
    return evs


def _runs(evs):
    runs = []
    cur = None
    for ev in evs:
        if ev["tag"] == "开始":
            cur = {"ts": ev["ts"], "ep": ev["ep"], "end_ts": "", "ep_end": ev["ep"],
                   "text": ev.get("body") or ""}
        elif ev["tag"] == "结束" and cur:
            cur.update(end_ts=ev["ts"], ep_end=ev["ep"], text=ev.get("body") or "")
            runs.append(cur)
            cur = None
    return runs


def _c(v):
    """温度：脚本给 `43C`（hwmon 毫度已换算），显示成 `43 °C`。"""
    v = (v or "").strip()
    if not v:
        return ""
    m = re.match(r'^(-?\d+(?:\.\d+)?)C$', v)
    return (m.group(1) + " °C") if m else v


def _pct(v):
    """`5%` / `5` → 5（int），取不到返回 None。"""
    m = re.match(r'^(-?\d+(?:\.\d+)?)', (v or "").strip())
    return float(m.group(1)) if m else None


def _ms(v):
    """`0.3ms` / `300us` / `1.2s` → 毫秒数，取不到返回 None。"""
    v = (v or "").strip()
    m = re.match(r'^(-?\d+(?:\.\d+)?)(ms|us|s)?$', v)
    if not m:
        return None
    n = float(m.group(1))
    return n * {"ms": 1, "us": 0.001, "s": 1000}.get(m.group(2) or "ms", 1)


def merged():
    """渲染用模型。结构见模板 hw.html。"""
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["ts"] < _TTL:
        return _CACHE["data"]

    evs = _events()
    runs = _runs(evs)
    last = runs[-1] if runs else None
    if last:
        w0 = (last.get("ep") or 0) - 60
        if len(runs) >= 2:
            w0 = max(w0, (runs[-2].get("ep_end") or 0) + 1)
        w1 = (last.get("ep_end") or last.get("ep") or 0) + 300
    else:
        newest = max((e["ep"] for e in evs), default=0)
        w0, w1 = newest - 900, newest + 60
        last = {"ep": newest, "ep_end": newest, "ts": _fmt_ts(newest),
                "text": "", "elapsed": None} if newest else None

    hosts = {}
    for ev in evs:
        if ev["tag"] != "盘点" or not ev["host"]:
            continue
        h = hosts.setdefault(ev["host"], {
            "name": ev["host"], "ip": "", "kv": {}, "ep": 0, "ts": "",
            "issues": [], "updated": 0,
        })
        if ev["ip"] and _IPV4_RE.match(ev["ip"]):
            h["ip"] = ev["ip"]
        if ev["ep"] >= h["ep"]:
            h["kv"] = dict(ev.get("kv") or {})
            h["ep"], h["ts"] = ev["ep"], ev["ts"]
        h["updated"] = max(h["updated"], ev["ep"])

    for ev in evs:
        if ev["tag"] != "异常" or not ev["host"]:
            continue
        h = hosts.setdefault(ev["host"], {
            "name": ev["host"], "ip": ev["ip"] or "", "kv": {}, "ep": ev["ep"],
            "ts": ev["ts"], "issues": [], "updated": ev["ep"],
        })
        if ev["ip"] and _IPV4_RE.match(ev["ip"]):
            h["ip"] = ev["ip"]
        if not (w0 <= ev["ep"] <= w1):
            continue
        kind = (ev["ext"].get("kind") or "").strip().lower()
        item = (ev["ext"].get("item") or "").strip()
        text = re.sub(r'^✗\s*', '', ev.get("text") or "").strip()
        h["issues"].append({
            "kind": KIND_NAMES.get(kind, kind or "其它"),
            "item": item,
            "text": text[:200],
            "level": "err" if ("不可纠正" in text or kind in ("ecc", "psu") and "fail" in text.lower()) else "warn",
            "ts": ev["ts"], "ep": ev["ep"], "host": ev["host"],
        })

    out = []
    for h in hosts.values():
        kv = h["kv"]
        issues = h["issues"]
        # 虚拟机/无 IPMI：ipmi=none 且没有任何传感器数据 → 「不适用」，不当异常
        na = (kv.get("ipmi", "").lower() == "none") and not kv.get("inlet") and not kv.get("cpu")
        level = "err" if any(i["level"] == "err" for i in issues) else (
            "warn" if issues else ("na" if na else ("ok" if kv else "none")))

        root = _pct(kv.get("root"))
        ntp = _ms(kv.get("ntp"))
        ce, ue = _pct(kv.get("ecc_ce")), _pct(kv.get("ecc_ue"))
        failed = _pct(kv.get("failed"))

        out.append({
            "name": h["name"], "ip": h["ip"], "kv": kv, "issues": issues,
            "state": level,
            "label": {"err": "异常", "warn": "告警", "ok": "正常",
                      "na": "不适用", "none": "无数据"}[level],
            "updated": _fmt_ts(h["updated"]),
            "stale": bool(kv) and not (w0 <= h["ep"] <= w1),
            "inlet": _c(kv.get("inlet")),
            "cpu": _c(kv.get("cpu")),
            "cpu_max": _c(kv.get("cpu_max")),
            "fan": kv.get("fan_min", ""),
            "fan_n": kv.get("fan_n", ""),
            "psu": (kv.get("psu") or "").lower(),
            "ecc": ("%d / %d" % (ce or 0, ue or 0)) if (ce is not None or ue is not None) else "",
            "ecc_err": bool(ue),
            "root": kv.get("root", ""),
            "root_level": ("err" if root is not None and root >= ROOT_ERR else
                           "warn" if root is not None and root >= ROOT_WARN else ""),
            "ntp": kv.get("ntp", ""),
            "ntp_level": ("err" if ntp is not None and abs(ntp) >= NTP_ERR else
                          "warn" if ntp is not None and abs(ntp) >= NTP_WARN else ""),
            "failed": int(failed) if failed is not None else 0,
            "disk": kv.get("disk", ""),
            "disk_life": kv.get("disk_life", ""),
            "boots": kv.get("boots", ""),
            "intrusion": (kv.get("intrusion") or "").lower(),
        })

    out.sort(key=lambda x: (0 if x["issues"] else 1, 0 if x["state"] == "na" else 1, x["name"]))

    issues = [i for h in out for i in h["issues"]]
    issues.sort(key=lambda i: (i["level"] != "err", -(i.get("ep") or 0)))

    stats = {
        "has_data": bool(evs),
        "hosts": len(out),
        "issues": len(issues),
        "bad_hosts": sum(1 for h in out if h["issues"]),
        "na_hosts": sum(1 for h in out if h["state"] == "na"),
        "stale_hosts": sum(1 for h in out if h["stale"]),
        "last": _fmt_ts(last.get("ep")) if last else "",
        "last_text": (last.get("text") or "") if last else "",
    }
    data = {"stats": stats, "hosts": out, "issues": issues,
            "runs": list(reversed(runs))[:10]}
    _CACHE["data"], _CACHE["ts"] = data, now
    return data
