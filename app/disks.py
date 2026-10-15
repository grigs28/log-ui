"""磁盘盘位图：服务器 → 槽位 → 设备 → OSD。

三个数据源合并，各司其职：
  布局（槽号/设备/OSD/备注）—— config/disks.yaml，UI 可编辑（唯一可写源）
  归属与状态（✓健康/✗异常）—— VL 的 osd-poll 日志，只读，按设备名匹配覆盖
  服务器清单              —— NAS 的 hosts 文件（自动）+ 日志出现过的主机

设计要点：
  - 日志里只有「异常盘」有明细（osd.poll.sh 设计如此），故盘位图以手录为主、
    日志为辅：日志能对上的盘自动带出归属与状态，对不上的照常显示手录内容。
  - 匹配键用「设备名」（/dev/sda）。将来 osd.poll.sh 上报槽位后可改为槽位优先。
"""
import re
import time

import httpx
import yaml
from pathlib import Path

from . import vl

BASE = Path(__file__).resolve().parent.parent
PATH = BASE / "config" / "disks.yaml"

HOSTS_URL = "http://192.168.0.79/sxiad/grigs/hosts"
DEFAULT_PER_ROW = 8           # 每排 8 个
DEFAULT_SLOTS = 8             # 没建档的机器默认按 8 盘位画（比空面板有信息量）
DEFAULT_ORIENT = "v"          # 盘向默认竖装（窄高格子，像机箱正面）
PER_ROW_RANGE = (1, 24)
SLOT_RANGE = (1, 200)

# [盘点] /dev/sda[ 系统盘 ]  v5-ctr02(192.168.93.92)  osd.2 : ✓ 健康
_POLL_RE = re.compile(
    r'^\[(?P<tag>盘点|异常)\]\s+'
    r'(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+'
    r'(?P<dev>/dev/[^\s\[]+)(?:\[(?P<attr>[^\]]*)\])?\s+'
    r'(?P<host>[^\s(]+)\((?P<ip>[^)]*)\)\s+'
    r'osd\.(?P<osd>[^\s]+)\s+:\s+(?P<msg>.*)$'
)

# 日志里的 ANSI 色码：真实 ESC 形式 或 被日志管道吃掉 ESC 后残留的 "#033[0;31m"
_ANSI_RE = re.compile(r'(?:\x1b|#033)?\[\d+(?:;\d+)*m')

# 容量：1.8T / 1.82TB / 894G / 400GiB —— 必须带单位字母，避免把「扇区: 9」当成容量。
# 当前 poll 脚本不上报容量，这里先解析着；脚本补报后自动生效，不必再改代码。
_SIZE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*([KMGT])(?:i?B)?\b', re.I)
_SIZE_TB = {"K": 1e-9, "M": 1e-6, "G": 1e-3, "T": 1.0}


_SIZE_ONLY = re.compile(r'^\d+(?:\.\d+)?\s*[KMGT](?:i?B)?$', re.I)
_MEDIA = ("SSD", "HDD", "NVME", "SSD-SATA", "SAS", "SATA")
_CARRIER_RE = re.compile(r'^(RAID\d*|ZFS|LVM|MD\d*|JBOD|MIRROR|STRIPE)\b', re.I)
_PSLOT_RE = re.compile(r'e(\d+)/s(\d+)')
# poll 里主机名后的括号有时不是 IP（本机轮询时是「本机」），别拿它当地址用
_IPV4_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')


def parse_size(text: str) -> str:
    """从一段文本里抠出容量，归一化成 1.8T / 894G 这种短写法。抠不到返回 ''。"""
    m = _SIZE_RE.search(text or "")
    return f"{m.group(1)}{m.group(2).upper()}" if m else ""


def parse_attr(attr: str) -> dict:
    """拆解盘行的属性括号。poll 新版是「·」分隔的结构化字段：

        /dev/sdf[ 2.2T·HDD·RAID0(e252/s6)·osd.22 ]
        /dev/sda[ 446.1G·SSD·RAID1(e252/s0+e252/s1)·系统盘 ]
        /dev/sdc[ 3.6T·HDD·ZFS:tank ]
        /dev/sdb[ 278.5G·HDD·RAID0(e252/s2)·osd.84·hdd-small ]
        旧版：osd.74·hdd-small    更旧：空

    返回 {size, media, carrier, pslots, role, dclass}，认不出的一律留空 ——
    老日志、格式微调都不会解析出错，只是少显示点东西。
    """
    out = {"size": "", "media": "", "carrier": "", "pslots": [], "role": "", "dclass": ""}
    for raw in (attr or "").split("·"):
        p = raw.strip()
        if not p:
            continue
        if not out["size"] and _SIZE_ONLY.match(p):
            out["size"] = parse_size(p)
        elif p.upper() in _MEDIA:
            out["media"] = p.upper()
        elif _CARRIER_RE.match(p):
            out["carrier"] = p
            out["pslots"] = ["%s/%s" % (e, s) for e, s in _PSLOT_RE.findall(p)]
        elif p.startswith("osd."):
            continue                        # OSD 号另有字段，不重复收
        elif "盘" in p:
            out["role"] = p                 # 系统盘 / 数据盘 …
        else:
            out["dclass"] = p               # Ceph 设备类，如 hdd-small
    return out


def size_tb(s: str):
    """'1.8T' → 1.8（TB）。认不出返回 None，供合计时跳过。"""
    m = re.match(r'^(\d+(?:\.\d+)?)([KMGT])$', (s or "").strip().upper())
    if not m:
        return None
    return float(m.group(1)) * _SIZE_TB[m.group(2)]


def _fmt_tb(v: float) -> str:
    if v >= 1000:
        return f"{v / 1000:.2f}P"
    if v >= 100:
        return f"{v:.0f}T"
    return f"{v:.1f}T"


def _severity(msg: str) -> str:
    """一条 [异常] 行的严重度：'' = 不是问题 | 'warn' | 'err'。

    poll 的 [异常] 行里其实混了三类东西，只看「有没有 ✗」会大量误报：
      ✓ 开头  —— storcli 卡层「正常」指标（「✓ 介质错误: 0」），值是 0 不是错
      ⚠ 开头  —— 软性警告（「⚠ 其他错误: 6」「⚠ SMART 未知（跳过）」）
      状态:开头 —— generate_report 的结论行，明细另在别行
      ✗ 或无标记（「smartctl 安装失败」「命令失败」）—— 真异常
    """
    m = (msg or "").strip()
    if m.startswith("✓") or m.startswith("状态:"):
        return ""
    if m.startswith("⚠"):
        return "warn"
    return "err"


BURST_SLACK = 900      # 秒。同一块盘一次轮询内几条记录的最大间隔，远小于两轮间隔

_hosts_cache = {"ts": 0.0, "rows": []}
_logs_cache = {"ts": 0.0, "data": None}
_HOSTS_TTL = 600      # hosts 文件缓存 10 分钟
_LOGS_TTL = 120       # 日志解析缓存 2 分钟


# ---------------- 布局（可编辑，持久化） ----------------

def _blank():
    return {"settings": {"default_per_row": DEFAULT_PER_ROW,
                         "default_slots": DEFAULT_SLOTS,
                         "default_mode": "h",     # h=横排(多列) | v=竖排(一列)
                         "default_orient": "v",   # 盘向：v=竖装(窄高) | h=横装(宽扁)
                         "cell_label": "slot"},   # slot | dev
            "servers": []}


def load() -> dict:
    try:
        d = yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        d = {}
    base = _blank()
    base["settings"].update(d.get("settings") or {})
    base["servers"] = d.get("servers") or []
    return base


def save(d: dict) -> None:
    PATH.write_text(
        yaml.safe_dump(d, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _clamp_per_row(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_PER_ROW
    lo, hi = PER_ROW_RANGE
    return min(max(n, lo), hi)


def _clamp_mode(v) -> str:
    """面板排布：h=横排(多列) | v=竖排(一列)。"""
    v = (v or "").strip().lower()
    return v if v in ("h", "v") else "h"


def _clamp_orient(v) -> str:
    """盘向：v=竖装(窄高格子) | h=横装(宽扁格子)。"""
    v = (v or "").strip().lower()
    return v if v in ("h", "v") else "v"


def get_server(d: dict, name: str):
    for s in d["servers"]:
        if s.get("name") == name:
            return s
    return None


def upsert_server(name: str, ip: str = "", per_row=None) -> dict:
    d = load()
    name = (name or "").strip()
    if not name:
        return d
    s = get_server(d, name)
    if s is None:
        s = {"name": name, "ip": (ip or "").strip(),
             "mode": _clamp_mode(d["settings"].get("default_mode")),
             "orient": _clamp_orient(d["settings"].get("default_orient")),
             "per_row": _clamp_per_row(per_row or d["settings"]["default_per_row"]),
             "disks": _blank_layout(d)}      # 与面板默认一致，不给 0 槽的空白档案
        d["servers"].append(s)
    else:
        if ip:
            s["ip"] = ip.strip()
        if per_row is not None:
            s["per_row"] = _clamp_per_row(per_row)
        s.setdefault("disks", [])
    save(d)
    return d


def _blank_layout(d: dict) -> list:
    """没用档案时的默认盘位（默认 8 个空槽）。"""
    n = max(1, int(d["settings"].get("default_slots") or DEFAULT_SLOTS))
    return [{"slot": i, "dev": "", "osd": "", "note": ""} for i in range(n)]


def _ensure(d: dict, name: str, ip: str = "", seed_slots: bool = False) -> dict | None:
    """取服务器档案；不存在就建一个。

    视图设置、槽位数、拖动排序都是「针对这台机器」的操作，日志发现但未建档的
    机器不该静默失败——顺手建档，用户不想要可以点「删档案」。
    seed_slots：建档时把面板上已经画出来的默认空槽位一并落档，否则「共 N 槽」
    会从 8 跳成 0 再跳回来。
    """
    name = (name or "").strip()
    if not name:
        return None
    s = get_server(d, name)
    if s is None:
        s = {"name": name, "ip": (ip or "").strip(),
             "mode": _clamp_mode(d["settings"].get("default_mode")),
             "orient": _clamp_orient(d["settings"].get("default_orient")),
             "per_row": _clamp_per_row(d["settings"]["default_per_row"]),
             "disks": _blank_layout(d) if seed_slots else []}
        d["servers"].append(s)
    elif ip and not s.get("ip"):
        s["ip"] = ip.strip()
    s.setdefault("disks", [])
    return s


def set_view(server: str, mode: str = "", per_row=0, orient: str = "",
             ip: str = "", rows=0) -> None:
    """改视图：面板横竖 + 每排数量 + 盘向（竖装/横装）+ 排数。

    **总槽位数是派生值，不是独立输入**：总槽位数 = 每排 × 排数，恒为整排矩形。
    这样三个数字永远自洽，不会再出现「填了 24 却按 40 生效」那种两个输入框
    各管一半、点错按钮就白改的情况。

    已装盘的槽号仍受保护：排数不够放下它们时自动向上取整（总槽位数跟着变大），
    宁可多给空槽位，也不把盘挤出面板。
    """
    d = load()
    s = _ensure(d, server, ip)
    if s is None:
        return
    if mode:
        s["mode"] = _clamp_mode(mode)
    if orient:
        s["orient"] = _clamp_orient(orient)
    if per_row and int(per_row) > 0:
        s["per_row"] = _clamp_per_row(per_row)
    save(d)
    if rows and int(rows) > 0:
        pr = s["per_row"]
        n = min(max(int(rows), 1), 200)
        need = max((int(x.get("slot", -1)) for x in s["disks"]
                    if (x.get("dev") or "").strip()), default=-1) + 1
        if pr * n < need:                 # 放不下已装的盘 → 排数补到够
            n = -(-need // pr)
        set_slot_count(server, pr * n, ip)


def save_order(server: str, rows: list, ip: str = "") -> int:
    """按面板显示顺序重排盘位：槽号 = 视觉顺序号（0..N-1）。返回写入的盘位数。

    **不凭空扩容**：面板尾部那几块「日志发现的盘」跟着一起提交，若直接编号就会
    把机箱从 8 槽变成 9 槽——用户只是拖了下位置，槽位数不该变。规则是
      · 总槽位数 = max(原声明槽位数, 实际装了盘的块数)
      · 多出来的格子优先丢**空槽**（空槽只是占位，丢了不损失信息），从后往前丢
      · 盘数真的超过槽位数时才扩容
    """
    d = load()
    s = _ensure(d, server, ip, seed_slots=True)   # 未建档的机器拖动也要能生效
    if s is None or not rows:
        return 0
    declared = sum(1 for x in s["disks"] if x.get("slot") is not None)
    items = []
    for item in rows:
        dev, osd, note, size = (list(item) + ["", "", "", ""])[:4]
        items.append({"dev": (dev or "").strip(),
                      "osd": (osd or "").strip().lstrip(".").replace("osd.", ""),
                      "note": (note or "").strip(),
                      "size": (size or "").strip()})
    total = max(declared, sum(1 for x in items if x["dev"]))
    excess = len(items) - total
    for i in range(len(items) - 1, -1, -1):     # 从后往前丢空槽
        if excess <= 0:
            break
        if not items[i]["dev"]:
            items.pop(i)
            excess -= 1
    while len(items) < total:                   # 不够就补空槽
        items.append({"dev": "", "osd": "", "note": "", "size": ""})
    out = [dict(x, slot=i) for i, x in enumerate(items)]
    s["disks"] = out
    save(d)
    return len(out)


def remove_server(name: str) -> None:
    d = load()
    d["servers"] = [s for s in d["servers"] if s.get("name") != name]
    save(d)


def upsert_disk(server: str, slot, dev: str = "", osd: str = "", note: str = "") -> None:
    """按槽号新增/修改一块盘。"""
    d = load()
    s = get_server(d, (server or "").strip())
    if s is None:
        return
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        return
    row = next((x for x in s["disks"] if int(x.get("slot", -1)) == slot), None)
    if row is None:
        row = {"slot": slot}
        s["disks"].append(row)
    row["dev"] = (dev or "").strip()
    row["osd"] = (osd or "").strip().lstrip(".").replace("osd.", "")
    row["note"] = (note or "").strip()
    s["disks"].sort(key=lambda x: int(x.get("slot", 0)))
    save(d)


def remove_disk(server: str, slot) -> None:
    d = load()
    s = get_server(d, (server or "").strip())
    if s is None:
        return
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        return
    s["disks"] = [x for x in s["disks"] if int(x.get("slot", -1)) != slot]
    save(d)


def save_table(server: str, per_row, rows: list) -> None:
    """整表保存。rows = [(slot, dev, osd, note, size), ...]；槽号留空的行即删除。"""
    d = load()
    s = get_server(d, (server or "").strip())
    if s is None:
        return
    if per_row and int(per_row) > 0:      # 视图已由工具条单独管，这里留空即不动
        s["per_row"] = _clamp_per_row(per_row)
    out, seen = [], set()
    for row in rows:
        slot, dev, osd, note, size = (list(row) + [""] * 5)[:5]
        try:
            n = int(str(slot).strip())
        except (TypeError, ValueError):
            continue                      # 槽号留空 → 删掉该行
        if n in seen:
            continue                      # 同槽号重复提交，取首个
        seen.add(n)
        out.append({"slot": n, "dev": (dev or "").strip(),
                    "osd": (osd or "").strip().lstrip(".").replace("osd.", ""),
                    "note": (note or "").strip(),
                    "size": parse_size(size) or (size or "").strip()})
    s["disks"] = sorted(out, key=lambda x: x["slot"])
    save(d)


def archive_unfiled(name: str) -> int:
    """把日志里发现、档案里没有的盘落档。返回新增条数。

    优先**填进已声明的空槽位**（槽位数设了 12、只装 8 块时，剩下的 4 个空位
    正好收下日志发现的盘）；空位填满后才往后顺延新槽号。日志不带槽位信息，
    顺延出来的只是顺序位，事后可在「编辑盘位」改成真实槽号。
    服务器本身没建档的，一并建。
    """
    name = (name or "").strip()
    if not name:
        return 0
    log_disks = (poll_logs()["by_host"].get(name) or {}).get("disks", {}) or {}
    if not log_disks:
        return 0
    d = load()
    s = get_server(d, name)
    if s is None:
        # 面板刚才是按默认盘位数画的，建档要落到同样的数，否则「共 N 槽」会跳变
        s = {"name": name, "ip": (poll_logs()["by_host"].get(name) or {}).get("ip", ""),
             "mode": _clamp_mode(d["settings"].get("default_mode")),
             "orient": _clamp_orient(d["settings"].get("default_orient")),
             "per_row": d["settings"]["default_per_row"],
             "disks": [{"slot": i, "dev": "", "osd": "", "note": ""}
                       for i in range(max(1, int(d["settings"].get("default_slots") or DEFAULT_SLOTS)))]}
        d["servers"].append(s)
    have = {x.get("dev") for x in s["disks"] if x.get("dev")}
    # 空槽位（有槽号、没装盘）按槽号顺序待填
    empties = sorted((x for x in s["disks"] if not (x.get("dev") or "").strip()),
                     key=lambda x: int(x.get("slot", 0)))
    nxt = max([int(x.get("slot", -1)) for x in s["disks"]] or [-1]) + 1
    added = 0
    for dev, ent in sorted(log_disks.items()):
        if dev in have:
            continue
        if empties:
            row = empties.pop(0)          # 先填空槽位，保留槽号与备注
        else:
            row = {"slot": nxt, "dev": "", "osd": "", "note": ""}
            s["disks"].append(row)
            nxt += 1
        row["dev"] = dev
        row["osd"] = ent.get("osd", "")
        if ent.get("size") and not row.get("size"):
            row["size"] = ent["size"]
        added += 1
    if added:
        s["disks"].sort(key=lambda x: int(x.get("slot", 0)))
        save(d)
    return added


def set_slot_count(server: str, count, ip: str = "") -> int:
    """声明机箱槽位数：面板显示 0..count-1 共 count 个格子，装了几块就几块亮。

    未占用的格子是空槽（可被「建档」或拖动填充）；多出来的空格子直接删掉。
    **已装盘的槽号一律不动**——若最高已装槽位 ≥ count，count 会被顶到它 +1，
    避免设置值小于实际装盘数时把盘挤出面板。返回最终生效的槽位数。
    """
    d = load()
    s = _ensure(d, server, ip)
    if s is None:
        return 0
    try:
        n = int(count)
    except (TypeError, ValueError):
        return 0
    if n < 1 or n > 200:
        return 0
    occupied = [int(x.get("slot", -1)) for x in s["disks"] if (x.get("dev") or "").strip()]
    if occupied:
        n = max(n, max(occupied) + 1)
    # 超出 n 的空槽删掉（有盘的一律保留）
    s["disks"] = [x for x in s["disks"]
                  if (x.get("dev") or "").strip() or int(x.get("slot", -1)) < n]
    have = {int(x.get("slot", -1)) for x in s["disks"]}
    for i in range(n):
        if i not in have:
            s["disks"].append({"slot": i, "dev": "", "osd": "", "note": ""})
    s["disks"].sort(key=lambda x: int(x.get("slot", 0)))
    save(d)
    return n


# ---------------- 服务器清单（NAS hosts 文件） ----------------

def fetch_hosts() -> list:
    """从 NAS 拉 hosts 文件 → [{'ip':..,'name':..}]。失败返回上次缓存/空。"""
    now = time.time()
    if _hosts_cache["rows"] and now - _hosts_cache["ts"] < _HOSTS_TTL:
        return _hosts_cache["rows"]
    rows = []
    try:
        r = httpx.get(HOSTS_URL, timeout=6)
        if r.status_code == 200:
            for line in r.text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    rows.append({"ip": parts[0], "name": parts[1]})
    except Exception:
        pass
    if rows:
        _hosts_cache["rows"], _hosts_cache["ts"] = rows, now
    return _hosts_cache["rows"]


# ---------------- 日志（osd-poll：归属与状态） ----------------

def _epoch(ts: str):
    """把盘行里的 'YYYY-MM-DD HH:MM:SS' 转成秒。解析不了返回 None。

    用消息自带的时间戳而不是 _time —— 时间戳就在行里，且字符串形式本身
    就按时间可排序，不依赖日志库返回的顺序或 _time 的格式。
    """
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None


def poll_logs() -> dict:
    """解析 osd-poll 日志 → {'by_host': {...}, 'runs': [...]}。

    **每块盘只认它最近一次轮询的结果**（这是关键）：
    轮询日志是流水，历史上坏过的盘会一直留在日志里。如果按「见过就记」累加，
    一块盘上次报异常、这次盘点说健康，旧的 issue 会永远挂着 —— 永远不会恢复。
    反过来只看 status 又会漏掉老格式的异常行。

    所以先按 (主机, 设备) 归拢所有记录，再只保留**该盘时间戳最新那一批**的记录，
    在这批里重新判定状态。同一块盘在一个批次里的记录（盘点行 + 若干异常行）
    是解析循环里连着发的、时间戳几乎相同；而两轮轮询至少间隔几十分钟。

      by_host: {host: {'ip','addr_note','disks': {dev: {...}}, 'poll_errors'}}
      runs:    [{'ts','lines'}]（最近若干次轮询的 [开始]/[结束] 记录）
    """
    now = time.time()
    if _logs_cache["data"] is not None and now - _logs_cache["ts"] < _LOGS_TTL:
        return _logs_cache["data"]

    logs = vl.query_logs(expr="app_name:osd-poll", limit=2000)

    # (host, dev) → 该盘的全部记录；主机级失败另存
    dev_events = {}          # key -> [(epoch, tag, body, osd, attr, addr)]
    host_addr = {}           # host -> {'ip','addr_note'}
    host_errors = {}         # host -> [(epoch, body)]
    runs = []
    cur = None

    for rec in logs:
        msg = rec.get("_msg") or ""
        m = _POLL_RE.match(msg)
        if m:
            host = m.group("host")
            addr = (m.group("ip") or "").strip()
            a = host_addr.setdefault(host, {})
            # 主机括号里可能是真 IP，也可能是「本机」（本机自轮询）——
            # 只认 IP 形态，免得把「本机」当地址覆盖掉真实 IP
            if _IPV4_RE.match(addr):
                a["ip"] = addr
            elif addr and not a.get("ip"):
                a["addr_note"] = addr
            ep = _epoch(m.group("ts"))
            if ep is None:
                continue
            body = _ANSI_RE.sub("", m.group("msg")).strip()
            dev = m.group("dev")
            if dev == "/dev/?":
                # 主机级失败（ssh/命令/无 IP），不是某块盘的问题
                host_errors.setdefault(host, []).append((ep, body))
                continue
            dev_events.setdefault((host, dev), []).append(
                (ep, m.group("tag"), body, m.group("osd"), (m.group("attr") or "").strip(), addr))
            continue
        if "[开始]" in msg:
            cur = {"ts": rec.get("_time", ""), "lines": [msg.strip()]}
            runs.append(cur)
        elif "[结束]" in msg:
            if cur is not None:
                cur["lines"].append(msg.strip())
            cur = None

    by_host = {}
    for (host, dev), evs in dev_events.items():
        newest = max(e[0] for e in evs)
        # 只留最近一批：该盘最新记录往前 BURST_SLACK 秒内的
        batch = sorted((e for e in evs if e[0] >= newest - BURST_SLACK), key=lambda e: e[0])
        ent = {"osd": "", "attr": "", "status": "", "issue": "",
               "issues": [], "warns": []}
        seen = set()
        for ep, tag, body, osd, attr, _addr in batch:
            if tag == "异常":
                sev = _severity(body)
                if sev and body not in seen:
                    seen.add(body)
                    (ent["issues"] if sev == "err" else ent["warns"]).append(body)
            else:
                ent["status"] = body          # 同批内取最后一条盘点为准
            if osd and osd != "?":
                ent["osd"] = osd
            if attr:
                ent["attr"] = attr
        info = parse_attr(ent["attr"])
        for k in ("size", "media", "carrier", "role", "dclass"):
            if info[k]:
                ent[k] = info[k]
        if info["pslots"]:
            ent["pslots"] = info["pslots"]
        if not ent.get("size"):               # 括号里没有就从正文里抠一次
            for _ep, _tag, body, _o, _a, _ad in batch:
                sz = parse_size(body)
                if sz:
                    ent["size"] = sz
                    break
        ent["issue"] = ent["issues"][0] if ent["issues"] else ""
        ent["issues"] = ent["issues"] + ent["warns"]     # 展示用：异常在前，警告在后
        h = by_host.setdefault(host, {"ip": "", "disks": {}, "poll_errors": []})
        h["disks"][dev] = ent

    for host, a in host_addr.items():
        h = by_host.setdefault(host, {"ip": "", "disks": {}, "poll_errors": []})
        h.update(a)
    for host, errs in host_errors.items():
        h = by_host.setdefault(host, {"ip": "", "disks": {}, "poll_errors": []})
        # 同样只留最近一批主机级失败
        newest = max(e[0] for e in errs)
        h["poll_errors"] = [b for ep, b in sorted(errs) if ep >= newest - BURST_SLACK]

    data = {"by_host": by_host, "runs": runs[:12]}
    _logs_cache["data"], _logs_cache["ts"] = data, now
    return data


# ---------------- 合并视图 ----------------

def merged(include_hosts: bool = True) -> dict:
    """布局 + 日志 → 渲染用模型。日志只覆盖「归属/状态/异常」，不改布局。"""
    d = load()
    logs = poll_logs()
    by_host = logs["by_host"]
    dslots = int(d["settings"].get("default_slots") or DEFAULT_SLOTS)

    servers = []
    seen = set()
    for s in d["servers"]:
        servers.append(_assemble(s, by_host.get(s["name"]), default_slots=dslots))
        seen.add(s["name"])
    # 日志里出现但布局中没有的主机：只读展示，提示「未建档」
    for name, h in sorted(by_host.items()):
        if name in seen:
            continue
        servers.append(_assemble({"name": name, "ip": h.get("ip", ""),
                                  "mode": d["settings"].get("default_mode"),
                                  "orient": d["settings"].get("default_orient"),
                                  "per_row": d["settings"]["default_per_row"],
                                  "disks": []}, h, auto=True, default_slots=dslots))
    # hosts 文件里未建档的：占位（有 IP 无盘位）
    if include_hosts:
        for row in fetch_hosts():
            if row["name"] in seen or row["name"] in by_host:
                continue
            servers.append(_assemble(
                {"name": row["name"], "ip": row["ip"],
                 "mode": d["settings"].get("default_mode"),
                 "orient": d["settings"].get("default_orient"),
                 "per_row": d["settings"]["default_per_row"],
                 "disks": [], "unbuilt": True}, None, auto=True, default_slots=dslots))
    servers.sort(key=lambda x: x["name"])
    stats = {
        "servers": len(servers),
        "disks": sum(len(s["disks"]) for s in servers),
        "bad": sum(s["bad"] for s in servers),
        "unbuilt": sum(1 for s in servers if s.get("unbuilt")),
    }
    return {"settings": d["settings"], "servers": servers,
            "runs": logs["runs"], "stats": stats}


# 状态 → 短标签：绿=日志确认健康；红=有异常；黄=软性警告或日志无数据
_STATE_LABEL = {"ok": "健康", "err": "异常", "warn": "未知", "none": ""}


def _state_label(st: str, ent) -> str:
    """黄灯有两种来源，标签分清楚：有 ⚠ 明细的是「警告」，没数据的是「未知」。"""
    if st == "warn":
        return "警告" if (ent or {}).get("warns") else "未知"
    return _STATE_LABEL.get(st, "")


def _disk_state(ent: dict, has_dev: bool) -> str:
    """绿=确认健康 / 红=异常 / 黄=未知。

    判据以**最近一次轮询**为准（poll_logs 已按盘归拢到最新一批）：
      ✗ 或任何一条真异常 → 红
      盘点正文里的 ⚠ → 黄
      ✓ → 绿
      有设备但日志没这条盘的记录 → 黄（未验证）
    """
    if not ent:
        return "warn" if has_dev else "none"
    s = (ent.get("status") or "").strip()
    if ent.get("issue") or s.startswith("✗"):
        return "err"                      # 有 ✗ 明细，或盘点结论就是 ✗
    if ent.get("warns") or s.startswith("⚠") or "⚠" in s:
        return "warn"                     # 只有软性警告 / SMART 未知
    if s.startswith("✓"):
        return "ok"
    return "warn" if has_dev else "none"


def _status_text(ent) -> str:
    """盘位显示的短状态：异常时给出主因，否则回显日志原文。"""
    e = ent or {}
    if e.get("issue"):
        return e["issue"]
    st = (e.get("status") or "").strip()
    return st


def _log_fields(ent) -> dict:
    """日志侧带出来的硬件信息（档案里不存，每次从日志现取）。"""
    e = ent or {}
    return {"media": e.get("media", ""), "role": e.get("role", ""),
            "carrier": e.get("carrier", ""), "pslots": e.get("pslots", []),
            "dclass": e.get("dclass", "")}


def _assemble(s: dict, log_h: dict, auto: bool = False, default_slots: int = DEFAULT_SLOTS) -> dict:
    """把一台服务器的布局与日志合并成渲染行。"""
    mode = _clamp_mode(s.get("mode"))
    orient = _clamp_orient(s.get("orient") or DEFAULT_ORIENT)
    h_per_row = _clamp_per_row(s.get("per_row") or DEFAULT_PER_ROW)
    per_row = 1 if mode == "v" else h_per_row   # 竖排：一列；横排：多列
    log_disks = (log_h or {}).get("disks", {}) or {}
    # 没建过档的机器：先按默认盘位数摆一排空槽位，比空面板有信息量，
    # 也给了「日志发现的盘」一个可拖进去的目标。真实档案一旦存在就用档案。
    layout = s.get("disks") or [{"slot": i, "dev": "", "osd": "", "note": ""}
                                for i in range(max(1, int(default_slots or DEFAULT_SLOTS)))]
    disks = []
    used_devs = set()
    for row in layout:
        dev = (row.get("dev") or "").strip()
        ent = log_disks.get(dev) if dev else None
        used_devs.add(dev)
        st = _disk_state(ent, bool(dev))
        disks.append({
            "slot": int(row.get("slot", 0)),
            "dev": dev,
            "osd": (row.get("osd") or "").strip() or (ent or {}).get("osd", ""),
            "attr": (ent or {}).get("attr", ""),
            "size": (row.get("size") or "").strip() or (ent or {}).get("size", ""),
            "note": (row.get("note") or "").strip(),
            "issue": (ent or {}).get("issue", ""),
            "status": _status_text(ent),
            "issues": (ent or {}).get("issues", []),
            "known": bool(ent),
            "state": st, "label": _state_label(st, ent),
            **_log_fields(ent),
        })
    # 日志里有、布局里没录的盘：自动补进图（提示未建档）
    for dev, ent in sorted(log_disks.items()):
        if dev in used_devs:
            continue
        st = _disk_state(ent, True)
        disks.append({"slot": None, "dev": dev, "osd": ent.get("osd", ""),
                      "attr": ent.get("attr", ""), "size": ent.get("size", ""),
                      "note": "", "issue": ent.get("issue", ""),
                      "status": _status_text(ent), "issues": ent.get("issues", []),
                      "known": True, "unfiled": True,
                      "state": st, "label": _state_label(st, ent), **_log_fields(ent)})
    disks.sort(key=lambda x: (x["slot"] is None, x["slot"] if x["slot"] is not None else 0))
    # 面板分行：每排 per_row 个
    framed = [x for x in disks if x["slot"] is not None]
    extra = [x for x in disks if x["slot"] is None]
    rows = [framed[i:i + per_row] for i in range(0, len(framed), per_row)]
    if extra:
        rows.append(extra)
    # 排数：只数有槽号的格子（日志发现的「未建档」行不算进机箱排数）
    layout_rows = (len(framed) + per_row - 1) // per_row if framed else 0
    # 工具条里「共 N 排」用的是**横排口径**（每排 h_per_row），与当前面板是否竖排无关
    h_rows = (-(-len(framed) // h_per_row)) if framed else 0
    bad = sum(1 for x in disks if x["state"] == "err")
    unknown = sum(1 for x in disks if x["state"] == "warn")
    poll_errors = list((log_h or {}).get("poll_errors") or [])
    if bad:
        st, label = "err", f"{bad} 异常"
    elif unknown or poll_errors or s.get("unbuilt"):
        st = "warn"
        label = (f"{unknown} 未知" if unknown else
                 ("未建档" if s.get("unbuilt") else "轮询异常"))
    else:
        st, label = ("ok", "全部健康") if disks else ("none", "")
    # 容量合计：只累加认得出来的（1.8T / 894G / 400GiB …），认不出的跳过
    sizes = [size_tb(x["size"]) for x in disks if x["dev"]]
    known = [v for v in sizes if v is not None]
    total = _fmt_tb(sum(known)) if known else ""
    # 硬件构成：介质 / 系统盘 / 设备类。只统计装了盘的格子。
    filled = [x for x in disks if x["dev"]]
    media = {"SSD": sum(1 for x in filled if x["media"] == "SSD"),
             "HDD": sum(1 for x in filled if x["media"] == "HDD")}
    sysn = sum(1 for x in filled if x["role"] == "系统盘")
    classes = sorted({x["dclass"] for x in filled if x["dclass"]})
    return {"name": s["name"], "ip": (s.get("ip") or (log_h or {}).get("ip", "")),
            "addr_note": (log_h or {}).get("addr_note", ""),
            "mode": mode, "orient": orient, "cols": per_row, "h_per_row": h_per_row,
            "row_count": len(rows), "rows": rows, "disks": disks,
            "slots": len(framed), "layout_rows": layout_rows, "h_rows": h_rows,
            "auto": auto, "unbuilt": s.get("unbuilt", False),
            # count = 面板格子数（含空槽）；installed = **有槽号的**格子里装了盘的数量。
            # 日志发现但未建档的盘没有槽号、不占机箱槽位，故不计入 installed ——
            # 否则卡片头部「已装 1 / 共 9 槽」会和工具条「总槽位数 8」对不上。
            # 这些盘由 unfiled 单独计数，头部另有一枚「未建档」标签说明。
            "count": len(disks),
            "installed": sum(1 for x in disks if x["dev"] and x["slot"] is not None),
            "installed_any": sum(1 for x in disks if x["dev"]),
            "size_known": len(known), "total_size": total,
            "media": media, "sys_count": sysn, "classes": classes,
            "bad": bad, "unknown": unknown,
            "poll_errors": poll_errors,
            "unfiled": sum(1 for x in disks if x.get("unfiled")),
            "state": st, "label": label}
