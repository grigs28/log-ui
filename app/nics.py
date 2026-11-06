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
import os
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


def owner_class(owner):
    """归属 → 配色类。bond 成员一律上色（bond0/bond1… 各一种），物理口用灰色虚线。

    归属是「这块网卡属于哪个聚合组」，是结构信息不是健康状态，
    所以只用蓝/紫/青/粉这几个冷色相，绿黄红留给状态。
    """
    o = (owner or "").strip()
    m = re.match(r'bond(\d+)$', o, re.I)
    if m:
        return "own-b%d" % (int(m.group(1)) % 4)
    if o in ("物理口", "聚合"):
        return "own-phys"
    return ""


# 纯虚拟网卡驱动：主机如果全是这些，就当虚拟机，默认不显示
# （taf / .16 是 VM，2 块 virtio_net —— 2026-11-06 用户要求去掉）
VIRTUAL_DRIVERS = {
    "virtio_net", "vmxnet3", "e1000", "e1000e", "hv_netvsc",
    "xen_netfront", "ena", "veth", "dummy",
}
_CFG_PATH = None          # 允许测试覆盖
_CFG_CACHE = {"ts": 0.0, "data": {"hide": [], "show": []}}


def _load_cfg():
    """config/nics.yaml：hide 额外隐藏的主机，show 强制显示（全是虚拟网卡也显示）。"""
    global _CFG_PATH
    now = time.time()
    if now - _CFG_CACHE["ts"] < 30:
        return _CFG_CACHE["data"]
    path = _CFG_PATH or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "nics.yaml")
    data = {"hide": [], "show": []}
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for k in ("hide", "show"):
            v = raw.get(k) or []
            if isinstance(v, str):
                v = [v]
            data[k] = [str(x).strip() for x in v if str(x).strip()]
    except Exception:
        pass
    _CFG_CACHE["data"], _CFG_CACHE["ts"] = data, now
    return data


def _natural(name):
    """nic10 排在 nic9 后面，别按字典序。"""
    m = re.match(r'^(.*?)(\d+)$', name or "")
    return (m.group(1), int(m.group(2))) if m else (name or "", -1)


def speed_class(speed):
    """速率分档 → CSS 类名。10G 和 1G 一眼要能分开（颜色由 log-ui.css 的 .spd-* 定义）。"""
    m = re.match(r'(\d+)', str(speed or ""))
    if not m:
        return "spd-unk"
    mb = int(m.group(1))
    if mb >= 100000:
        return "spd-100g"
    if mb >= 25000:
        return "spd-25g"
    if mb >= 10000:
        return "spd-10g"
    if mb >= 1000:
        return "spd-1g"
    return "spd-low"


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


# 扩展区 kind 取值 → 页面显示名（A10：已知值直接用，未知值/缺失回落到文案关键字）
KIND_NAMES = {
    "pcie": "PCIe 链路",
    "aer": "PCIe 错误",
    "crc": "CRC",
    "cksum": "校验和",
    "frame": "帧错误",
    "drop": "拥塞丢包",
    "carrier": "链路抖动",
    "sfp": "SFP",
    "bond": "bond",
    "link": "链路",
    "host": "主机",
}


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


def _num(v, digits):
    """扩展区的数值统一在这里定精度（脚本侧保留原值，展示精度归页面，见 A9）。"""
    try:
        return ("%." + str(digits) + "f") % float(v)
    except (TypeError, ValueError):
        return str(v)


def _ext_display(n):
    """把扩展字段拼成表格用的短文本；没有扩展区时全为空串。

    三态规则（A11/A12/A15）：
      链路  —— 断（红）/ 通·未转发（灰，bond backup 的正常态）/ 通
      计数  —— d_x=0 → +0；d_x>0 → +N；整组缺失（首轮）→ 空，由模板显示 —
    """
    e = n.get("ext") or {}

    # ---- 链路：MTU / 双工 + link/oper 三态 ----
    parts = []
    if e.get("mtu"):
        parts.append("MTU " + e["mtu"])
    if e.get("duplex"):
        parts.append(e["duplex"])
    link, oper = e.get("link"), e.get("oper")
    if link == "no":
        parts.append("断")
        n["link_state"] = "err"
    elif link and oper == "down":
        parts.append("通 / 未转发")          # bond backup 成员：不告警，标灰
        n["link_state"] = "mute"
    elif link:
        parts.append("通")
        n["link_state"] = "ok"
    else:
        n["link_state"] = ""
    n["link_txt"] = " · ".join(parts)

    # ---- PCIe：当前值，和能力不同就并排给出 ----
    pcie = e.get("pcie") or ""
    cap = e.get("cap") or ""
    if pcie and cap and pcie != cap:
        pcie = "%s（能力 %s）" % (pcie, cap)
    n["pcie_txt"] = pcie
    n["pci_txt"] = e.get("pci") or ""
    n["fw_txt"] = " ".join(x for x in (e.get("drv_ver"), e.get("fw")) if x)

    # ---- SFP：各自独立，缺哪项省哪项 ----
    sfp = []
    for k, unit, dg in (("sfp_temp", "°C", 1), ("sfp_vcc", "V", 2),
                        ("sfp_txbias", "mA", 1), ("sfp_txpwr", "dBm", 2),
                        ("sfp_rxpwr", "dBm", 2)):
        if e.get(k):
            sfp.append(_num(e[k], dg) + " " + unit)
    alarm = (e.get("sfp_alarm") or "").strip()
    if alarm and alarm != "none":
        sfp.append("报警 " + alarm)
    n["sfp_txt"] = " · ".join(sfp)
    n["sfp_alarm"] = bool(alarm and alarm != "none")

    # ---- 计数：只有增量；整组缺失 = 首轮无基线 ----
    keys = (("d_crc", "CRC"), ("d_cksum", "校验和"), ("d_frame", "帧"),
            ("d_rxerr", "rx_err"), ("d_txerr", "tx_err"), ("d_carrier", "carrier"))
    have = [k for k, _ in keys if k in e]
    cnt = []
    for k, label in keys:
        v = e.get(k)
        if v is None:
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv:
            cnt.append("%s +%d" % (label, iv))
    n["cnt_txt"] = " · ".join(cnt)
    n["cnt_has"] = bool(have)
    n["cnt_zero"] = bool(have) and not cnt


_IPMAP_CACHE = {"ts": 0.0, "map": {}}


def _host_ip_map():
    """主机名 → IP：用 NAS 的 hosts 文件（和磁盘页同源），缓存 10 分钟。"""
    now = time.time()
    if _IPMAP_CACHE["map"] and now - _IPMAP_CACHE["ts"] < 600:
        return _IPMAP_CACHE["map"]
    try:
        from .disks import fetch_hosts
        m = {r["name"]: r["ip"] for r in fetch_hosts() if r.get("name") and r.get("ip")}
    except Exception:
        m = {}
    if m:
        _IPMAP_CACHE["map"], _IPMAP_CACHE["ts"] = m, now
    return _IPMAP_CACHE["map"] or _IPMAP_CACHE["map"]


def _fmt_ts(ep):
    return time.strftime("%m-%d %H:%M", time.localtime(ep)) if ep else ""


_EXT_SPLIT = re.compile(r'\s\|\s(.*)$')
# 尾部 `key=value` 序列（没有 `|` 分隔时兜底用）。只认已知 key，免得误伤正文里的 a=b。
_TRAIL_KV = re.compile(r'(?:\s+[a-z][a-z0-9_]*=[^\s|]+)+$')
_KNOWN_EXT_KEYS = {
    "kind", "ip", "mtu", "duplex", "link", "oper", "pcie", "cap", "pci",
    "drv_ver", "fw", "sfp_temp", "sfp_vcc", "sfp_txbias", "sfp_txpwr",
    "sfp_rxpwr", "sfp_alarm",
    "d_crc", "d_cksum", "d_frame", "d_rxerr", "d_txerr", "d_carrier",
    "d_too_short", "d_missed", "d_nopbuf",
}


def _split_ext(rest):
    """把 `… ✓ 正常 | mtu=9000 duplex=Full` 切成 (正文, {扩展字段})。

    脚本 v0.0.2 起会在盘点/异常行尾追加 `| key=value …`（见 docs 里的建议文档）。
    这里**先截断再解析正文**，所以有没有扩展区都能用；扩展区里认不出的 key 直接忽略，
    将来脚本加字段不用改这个模块。
    """
    rest = rest or ""
    m = _EXT_SPLIT.search(rest)
    if m:
        body, tail = rest[:m.start()], m.group(1)
    else:
        # 实施侧 v0.0.4 的 [异常] 行是 `… 实际 x4 kind=pcie`，没有 `|` 分隔。
        # 只在这种无分隔形态下、且尾部每个 key 都已知时才认，否则原样当正文。
        m2 = _TRAIL_KV.search(rest)
        if not m2:
            return rest, {}
        toks = m2.group(0).split()
        if not toks or any(t.split("=", 1)[0] not in _KNOWN_EXT_KEYS for t in toks):
            return rest, {}
        body, tail = rest[:m2.start()], " ".join(toks)
    ext = {}
    for kv in tail.split():
        k, _, v = kv.partition("=")
        if k and v:
            ext[k.strip()] = v.strip()
    return body, ext


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
        rest, ext = _split_ext(rest)
        ep = _epoch(ts) or _epoch(rec.get("_time"))
        ev = {"tag": tag, "ts": ts, "ep": ep or 0, "rest": rest, "raw": msg, "ext": ext}

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


def merged(show_vm: bool = False):
    """渲染用模型。结构见模板 nics.html。show_vm=True 时连虚拟机一起显示。"""
    now = time.time()
    key = bool(show_vm)
    if (_CACHE["data"] is not None and _CACHE.get("key") == key
            and now - _CACHE["ts"] < _TTL):
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
        # 窗口左界要晚于「上一轮结束」，否则上一轮的异常会被当成当前状态重复算一遍
        w0 = (last.get("ep") or 0) - 60
        if len(runs) >= 2:
            prev_end = runs[-2].get("ep_end") or runs[-2].get("ep") or 0
            w0 = max(w0, prev_end + 1)
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
        elif (ev.get("ext") or {}).get("ip"):
            h["ip"] = ev["ext"]["ip"]
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
            "spd": speed_class(sp.group("speed") if sp else ""),
            "ok": bool(sp and sp.group("mark") == "✓"),
            "ep": ev["ep"], "ts": ev["ts"],
            "stale": not (w0 <= ev["ep"] <= w1),
            "issues": [],
            # 扩展区（脚本 v0.0.2 起才有；没有就是空 dict，模板按有无决定显示）
            "ext": dict(ev.get("ext") or {}),
        }
        h["nics"][ev["dev"]]["owner_cls"] = owner_class(h["nics"][ev["dev"]]["owner"])
        _ext_display(h["nics"][ev["dev"]])
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
        ek = ((ev.get("ext") or {}).get("kind") or "").strip().lower()
        kind = KIND_NAMES.get(ek) or _kind(text)
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
        # 主机可能只有异常、没有盘点行（本轮连不上 / 新机器），也要建条目，
        # 否则异常会被静默丢掉——2026-11-06 自测时踩到
        hn = hosts.setdefault(host, {"name": host, "ip": ev.get("ip") or "", "nics": {},
                                     "summary": "", "updated": ev["ep"], "issues": []})
        if ev.get("ip") and _IPV4_RE.match(ev["ip"]):
            hn["ip"] = ev["ip"]
        if ev.get("dev"):
            n = hn["nics"].get(ev["dev"])
            if n:
                n["issues"].append(item)
            else:
                hn["issues"].append(item)          # 本轮没有盘点行的网卡
        else:
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

    # 主机 IP：扩展区里给了 ip= 就用它；否则用 NAS 的 hosts 文件反查。
    # （2026-11-06 实施侧去掉了行尾 [来源 host(ip)]，页面不能再从行里拿 IP）
    ipmap = _host_ip_map()
    for h in out:
        if not h["ip"]:
            h["ip"] = ipmap.get(h["name"], "")

    # ---- 虚拟机过滤：全是虚拟网卡驱动的当 VM，默认隐藏（可 ?vm=1 显示）----
    cfg = _load_cfg()
    for h in out:
        drv = {n["driver"] for n in h["nics"] if n["driver"]}
        h["virtual"] = bool(drv) and drv <= VIRTUAL_DRIVERS
        if h["name"] in cfg["show"]:
            h["virtual"] = False
        elif h["name"] in cfg["hide"]:
            h["virtual"] = True
    hidden = [h for h in out if h["virtual"]]
    if not show_vm:
        out = [h for h in out if not h["virtual"]]

    all_nics = [n for h in out for n in h["nics"]]
    speed = {}
    driver = {}
    for n in all_nics:
        s = n["speed"] or "未知"
        e = speed.setdefault(s, {"label": s, "count": 0, "spd": speed_class(n["speed"])})
        e["count"] += 1
        d = n["driver"] or "未知"
        driver[d] = driver.get(d, 0) + 1

    issues = [i for h in out for i in (h["issues"] if not h["nics"] else [])
              ] + [i for h in out for n in h["nics"] for i in n.get("issues") or []]
    issues.sort(key=lambda i: (i["level"] != "err", -(i.get("ep") or 0)))

    stats = {
        "has_ext": any(n.get("ext") for n in all_nics),
        "hosts": len(out),
        "nics": len(all_nics),
        "issues": len(issues),
        "bad_hosts": sum(1 for h in out if h["bad"]),
        "stale_hosts": sum(1 for h in out if h["stale"]),
        "hidden_vm": len(hidden),
        "show_vm": bool(show_vm),
        "last": _fmt_ts(last.get("ep")) if last else "",
        "last_elapsed": last.get("elapsed") if last else None,
        "last_text": (last.get("text") if last else ""),
        "speed": sorted(speed.values(), key=lambda e: -e["count"]),
        "driver": sorted(driver.items(), key=lambda kv: -kv[1]),
    }

    data = {"stats": stats, "hosts": out, "issues": issues, "runs": list(reversed(runs))[:10]}
    _CACHE["data"], _CACHE["ts"], _CACHE["key"] = data, now, key
    return data
