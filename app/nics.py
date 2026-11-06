"""网卡链路巡检：net.check.sh / net.poll.sh 的日志 → 主机 × 网卡 视图。

数据只有一处来源：VL 里的 `app_name:net-poll`（.91 每轮 ssh 下发 net.check.sh，
按分节收集后统一上报）。三类行：

    [盘点] 2026-11-06 18:32:23  nic2[ixgbe·10G Ethernet: 10G Base-SR·0m·2020·bond0]  ls-bak08  bond0 : 10000Mb/s ✓ 正常
    [异常] 2026-11-06 18:32:26  nic4[ixgbe·…·bond0]  v3-nod14  bond0 : ✗ PCIe 链路宽度降级: 0000:81:00.0 期望 x8 实际 x4
    [汇总] 2026-11-06 18:32:24  ls-bak08  5 块网卡，无新增错误
    [开始]/[结束]  每轮各一条，给「轮询记录」用

写这个模块时查明的坑（改之前先看）：

  · [盘点]/[异常]/[汇总] 这几个 tag 和 osd.poll（磁盘）**共用**。不按 app_name
    过滤，`/dev/sdg` 的盘异常会被当成网卡异常混进来 —— 现在 92 条网卡异常里有
    132 条 SAS 读错误就是这么来的（查询时漏了过滤）。
  · 主机名有两种写法：裸名，以及 `ls-bak08(192.168.93.98)`（poll 追加的 [来源]），
    必须归一化，否则同一台机器会算成两台。
  · 方括号里是按 `·` 拼的复合标签（驱动·型号/光模块·线长·年份·归属），
    段数不固定（有的没有光模块、有的没有线长），只能从右往左按形态认，不能按下标取。
  · ignore 名单是每轮从 .79 下发到节点 /var/lib/net-check/ignore 的，
    历史上的异常行不会消失。所以「当前状态」只认最近一轮，历史只用来算「首见 / 持续几轮」。
"""
import re
import time

from . import vl

_CACHE = {"ts": 0.0, "data": None}
_TTL = 120                      # 巡检每天 2 轮，页面缓存 2 分钟足够

_TAG_RE = re.compile(r'^\[(盘点|异常|汇总|开始|结束)\]\s*'
                     r'(?:(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+)?(.*)$')
_SRC_RE = re.compile(r'\s*\[来源\s+[^\]]*\]\s*$')
_NIC_RE = re.compile(
    r'^(?P<dev>[^\[\s]+)(?:\[(?P<label>[^\]]*)\])?\s+'
    r'(?P<host>[^\s(]+)(?:\((?P<ip>[^)]*)\))?\s+'
    r'(?P<owner>[^\s:]+)\s*:\s*(?P<rest>.*)$')
_SPEED_RE = re.compile(r'^(?P<speed>\S+)\s+(?P<mark>[✓✗])\s*(?P<text>.*)$')
_IPV4_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')
_YEAR_RE = re.compile(r'^\d{4}$')
_DIST_RE = re.compile(r'^\d+(?:\.\d+)?m$')
_OWNER_RE = re.compile(r'^(bond\d*|物理口|聚合)$', re.I)

_END_RE = re.compile(r'用时\s*(\d+)s.*?检查\s*(\d+)\s*块网卡\s*/\s*(\d+)\s*台主机.*?'
                     r'(?:发现\s*(\d+)\s*处异常|未发现异常)')
_SUM_RE = re.compile(r'(\d+)\s*块网卡[，,]\s*(.+)$')


def _epoch(s):
    """'2026-11-06 18:32:23' / ISO8601 → epoch 秒；取不到返回 None。"""
    if not s:
        return None
    s = str(s).strip().replace("T", " ").replace("Z", "")
    s = s.split("+")[0].split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(s[:len(time.strftime(fmt))], fmt))
        except Exception:
            continue
    return None


def _natural(name):
    """nic10 排在 nic9 后面，别按字典序。"""
    m = re.match(r'^(.*?)(\d+)$', name or "")
    return (m.group(1), int(m.group(2))) if m else (name or "", -1)


def parse_label(label):
    """`ixgbe·10G Ethernet: 10G Base-SR·0m·2020·bond0` → 分字段。

    段数不固定，从右往左认「归属 / 年份 / 线长」，剩下的都算型号。
    """
    parts = [p.strip() for p in (label or "").split("·") if p.strip()]
    out = {"driver": "", "model": "", "distance": "", "year": "", "owner": ""}
    if not parts:
        return out
    out["driver"] = parts[0]
    rest = parts[1:]
    # 归属可能重复出现在末尾，也可能没有；只剩一段时也要认（`ixgbe·bond0`）
    while rest:
        p = rest[-1]
        if not out["owner"] and _OWNER_RE.match(p):
            out["owner"] = p
        elif not out["year"] and _YEAR_RE.match(p):
            out["year"] = p
        elif not out["distance"] and _DIST_RE.match(p):
            out["distance"] = p
        else:
            break
        rest.pop()
    out["model"] = " · ".join(rest)
    return out


def _kind(text):
    t = text or ""
    if "PCIe" in t or "AER" in t:
        return "PCIe"
    if "CRC" in t:
        return "CRC"
    if "校验和" in t:
        return "校验和"
    if "帧错误" in t or "_errors" in t:
        return "帧错误"
    if "carrier" in t or "链路抖动" in t:
        return "链路抖动"
    if "SFP" in t:
        return "SFP"
    if "bond" in t.lower() or "聚合" in t:
        return "bond"
    if "无有效巡检输出" in t or "无 IP" in t:
        return "主机"
    return "其它"


def _level(text):
    t = text or ""
    if "不可纠正" in t or "无有效巡检输出" in t or "无 IP" in t:
        return "err"
    return "warn"


def _fmt_ts(ep):
    return time.strftime("%m-%d %H:%M", time.localtime(ep)) if ep else ""


def _events():
    """VL 原始行 → 结构化事件列表（按时间升序）。"""
    logs = vl.query_logs(expr="app_name:net-poll", limit=4000)
    evs = []
    for rec in logs:
        msg = (rec.get("_msg") or "").strip()
        m = _TAG_RE.match(msg)
        if not m:
            continue
        tag, ts, rest = m.group(1), (m.group(2) or ""), m.group(3).strip()
        rest = _SRC_RE.sub("", rest).strip()
        ep = _epoch(ts) or _epoch(rec.get("_time"))
        ev = {"tag": tag, "ts": ts, "ep": ep or 0, "rest": rest, "raw": msg}

        if tag in ("盘点", "异常"):
            mm = _NIC_RE.match(rest)
            if mm and not mm.group("dev").startswith("主机"):
                ev.update(dev=mm.group("dev"), label=mm.group("label") or "",
                          host=mm.group("host"), ip=(mm.group("ip") or "").strip(),
                          owner=mm.group("owner"), body=mm.group("rest"))
            else:
                # 主机级：`主机 h 在 /etc/hosts 中无 IP，跳过` / `h(ip) 无有效巡检输出（…）`
                m2 = re.match(r'^(?:主机\s+)?([^\s(]+)(?:\(([^)]*)\))?\s*(.*)$', rest)
                ev.update(dev=None, host=(m2.group(1) if m2 else ""),
                          ip=((m2.group(2) or "").strip() if m2 else ""),
                          owner="", body=(m2.group(3) if m2 else rest))
        elif tag == "汇总":
            m3 = _SUM_RE.search(rest)
            if m3:
                ev["nics"] = int(m3.group(1))
                ev["body"] = m3.group(2)
                ev["host"] = rest[:m3.start()].strip().split()[0] if rest[:m3.start()].strip() else ""
            else:
                ev["body"] = rest
        elif tag == "结束":
            m4 = _END_RE.search(rest)
            if m4:
                ev["elapsed"] = int(m4.group(1))
                ev["nics"] = int(m4.group(2))
                ev["hosts"] = int(m4.group(3))
                ev["issues"] = int(m4.group(4) or 0)
            ev["body"] = rest
        elif tag == "开始":
            ev["body"] = rest
        evs.append(ev)

    evs.sort(key=lambda e: e["ep"])
    return evs


def _rounds(evs):
    """[开始] … [结束] → 轮次列表。"""
    runs = []
    cur = None
    for ev in evs:
        if ev["tag"] == "开始":
            if cur is not None:
                runs.append(cur)          # 上一轮没等到 [结束]（中断）也要留一条
            cur = {"ts": ev["ts"], "ep": ev["ep"], "end_ts": "", "ep_end": ev["ep"],
                   "nics": None, "hosts": None, "issues": 0, "elapsed": None,
                   "text": ev.get("body") or ""}
        elif ev["tag"] == "结束":
            if cur is None:
                cur = {"ts": "", "ep": ev["ep"]}
            cur.update(end_ts=ev["ts"], ep_end=ev["ep"], nics=ev.get("nics"),
                       hosts=ev.get("hosts"), issues=ev.get("issues", 0),
                       elapsed=ev.get("elapsed"), text=ev.get("body") or "")
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    for r in runs:
        r["time"] = _fmt_ts(r.get("ep") or 0)
        # 说明列：去掉和「主机/网卡/异常/用时」重复的前半段，只留结论
        r["note"] = re.sub(r'^轮询完成，用时\s*\d+s，检查\s*\d+\s*块网卡\s*/\s*\d+\s*台主机[，,]?\s*',
                           '', r.get("text") or "")
    return runs


def merged():
    """渲染用模型。结构见模板 nics.html。"""
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["ts"] < _TTL:
        return _CACHE["data"]

    evs = _events()
    runs = _rounds(evs)
    run_eps = [(r.get("ep") or 0, r.get("ep_end") or r.get("ep") or 0) for r in runs]

    def run_index(ep):
        for i, (a, b) in enumerate(run_eps):
            if a - 30 <= ep <= b + 300:
                return i
        return None

    # 最近一轮的时间窗：优先用 [开始]~[结束]，没有就用最新一行往前 15 分钟
    last = runs[-1] if runs else None
    if last:
        w0 = (last.get("ep") or 0) - 30
        w1 = (last.get("ep_end") or last.get("ep") or 0) + 300
    else:
        newest = max((e["ep"] for e in evs), default=0)
        w0, w1 = newest - 900, newest + 60

    # ---- 当前状态：每块网卡认它最近一条 [盘点]；不在本轮窗口内就标 stale ----
    hosts = {}
    for ev in evs:
        if ev["tag"] != "盘点" or not ev.get("dev"):
            continue
        h = hosts.setdefault(ev["host"], {"name": ev["host"], "ip": "", "nics": {},
                                          "summary": "", "updated": 0, "issues": []})
        if ev.get("ip") and _IPV4_RE.match(ev["ip"]):
            h["ip"] = ev["ip"]
        lab = parse_label(ev.get("label"))
        sp = _SPEED_RE.match(ev.get("body") or "")
        nic = h["nics"].get(ev["dev"])
        # 只保留时间更新的那条
        if nic and nic["ep"] > ev["ep"]:
            continue
        h["nics"][ev["dev"]] = {
            "name": ev["dev"],
            "owner": ev.get("owner") or lab.get("owner") or "",
            "driver": lab["driver"], "model": lab["model"],
            "distance": lab["distance"], "year": lab["year"],
            "speed": (sp.group("speed") if sp else ""),
            "ok": bool(sp and sp.group("mark") == "✓"),
            "ep": ev["ep"], "ts": ev["ts"],
            "stale": not (w0 <= ev["ep"] <= w1),
            "issues": [],
        }
        h["updated"] = max(h["updated"], ev["ep"])

    for ev in evs:
        if ev["tag"] != "汇总" or not ev.get("host"):
            continue
        h = hosts.get(ev["host"])
        if h and ev["ep"] >= h["updated"]:
            h["summary"] = ev.get("body") or ""
            if ev.get("ip") and _IPV4_RE.match(ev["ip"]):
                h["ip"] = ev["ip"]

    # ---- 异常：本轮窗口内的算「当前问题」，全量的算「首见 / 持续几轮」 ----
    hist = {}
    for ev in evs:
        if ev["tag"] != "异常":
            continue
        text = ev.get("body") or ""
        kind = _kind(text)
        host = ev.get("host") or ""
        key = (host, ev.get("dev") or "", kind)
        h = hist.setdefault(key, {"first": ev["ep"], "first_ts": ev["ts"],
                                  "rounds": set(), "text": text})
        ri = run_index(ev["ep"])
        h["rounds"].add(ri if ri is not None else ev["ep"])
        h["text"] = text                                     # 留最新一条文案

        if not (w0 <= ev["ep"] <= w1):
            continue
        item = {"kind": kind, "text": text, "level": _level(text),
                "since": "", "rounds": 0, "nic": ev.get("dev") or "", "owner": ev.get("owner") or "",
                "host": host, "ts": ev["ts"], "ep": ev["ep"]}
        if ev.get("dev"):
            hn = hosts.get(host)
            if hn:
                n = hn["nics"].get(ev["dev"])
                if n:
                    n["issues"].append(item)
                else:
                    hn["issues"].append(item)      # 本轮没有盘点行的网卡
        else:
            hn = hosts.setdefault(host, {"name": host, "ip": ev.get("ip") or "", "nics": {},
                                         "summary": "", "updated": ev["ep"], "issues": []})
            hn["issues"].append(item)

    for key, h in hist.items():
        item_rounds = len(h["rounds"])
        host, dev, kind = key
        hn = hosts.get(host)
        if not hn:
            continue
        pool = (hn["nics"].get(dev) or {}).get("issues") or hn["issues"]
        for it in pool:
            if it["kind"] == kind and it.get("nic", "") == dev:
                it["since"] = h["first_ts"][5:16].replace("-", "-")
                it["rounds"] = item_rounds

    # ---- 组装 ----
    out = []
    for h in hosts.values():
        nics = sorted(h["nics"].values(), key=lambda n: _natural(n["name"]))
        bad = [i for n in nics for i in n.get("issues") or []]
        bad += [i for i in h.get("issues") or []]
        err = any(i["level"] == "err" for i in bad)
        warn = any(i["level"] != "err" for i in bad)
        state = "err" if err else ("warn" if warn else ("ok" if nics else "none"))
        stale = bool(nics) and all(n["stale"] for n in nics)
        for n in nics:
            if n["issues"]:
                n["state"] = "err" if any(i["level"] == "err" for i in n["issues"]) else "warn"
                n["label"] = "异常"
            elif n["stale"]:
                n["state"], n["label"] = "none", "未上报"
            elif n["ok"]:
                n["state"], n["label"] = "ok", "正常"
            else:
                n["state"], n["label"] = "warn", "未知"
        out.append({
            "name": h["name"], "ip": h["ip"], "nics": nics,
            "count": len(nics), "bad": len(bad), "issues": h.get("issues") or [],
            "summary": h.get("summary", ""), "state": state, "stale": stale,
            "updated": _fmt_ts(h["updated"]),
            "label": {"err": "异常", "warn": "告警", "ok": "正常",
                      "none": "无数据"}.get(state, ""),
        })

    # 有问题的排前面，其余按名字
    out.sort(key=lambda x: (0 if x["bad"] else 1, 0 if x["stale"] else 1, x["name"]))

    all_nics = [n for h in out for n in h["nics"]]
    speed = {}
    driver = {}
    for n in all_nics:
        s = n["speed"] or "未知"
        speed[s] = speed.get(s, 0) + 1
        d = n["driver"] or "未知"
        driver[d] = driver.get(d, 0) + 1

    issues = [i for h in out for i in (h["issues"] if not h["nics"] else [])
              ] + [i for h in out for n in h["nics"] for i in n.get("issues") or []]
    issues.sort(key=lambda i: (i["level"] != "err", -(i.get("ep") or 0)))

    stats = {
        "hosts": len(out),
        "nics": len(all_nics),
        "issues": len(issues),
        "bad_hosts": sum(1 for h in out if h["bad"]),
        "stale_hosts": sum(1 for h in out if h["stale"]),
        "last": _fmt_ts(last.get("ep")) if last else "",
        "last_elapsed": last.get("elapsed") if last else None,
        "last_text": (last.get("text") if last else ""),
        "speed": sorted(speed.items(), key=lambda kv: -kv[1]),
        "driver": sorted(driver.items(), key=lambda kv: -kv[1]),
    }

    data = {"stats": stats, "hosts": out, "issues": issues, "runs": list(reversed(runs))[:10]}
    _CACHE["data"], _CACHE["ts"] = data, now
    return data
