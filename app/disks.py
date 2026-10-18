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
# RAID 括号里的成员表：RAID1(e252/s0+e252/s1) / RAID1(sg1+sg2) / RAID5(e252/s0+s1+s2)
_RAID_MEMBERS_RE = re.compile(r'\(([^)]*)\)')
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
    out = {"size": "", "media": "", "carrier": "", "pslots": [], "role": "",
           "dclass": "", "members": []}
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
            # 成员表按「+」拆 —— 不依赖 EID/Slt 写法。
            # 实测 v5-ctr02 的 RAID1 成员写作 sg1+sg2（不是 e252/s0），
            # 只认 EID/Slt 的话会把「两块盘的镜像」当成一块盘，盘位数少算。
            mm = _RAID_MEMBERS_RE.search(p)
            if mm:
                out["members"] = [x.strip() for x in mm.group(1).split("+") if x.strip()]
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


# poll 在盘点结论后面追加的附加信息（2026-10-18 起）：
#   ✓ 健康 · SSD寿命 89%/99% · 使用 64745h
# 解析成结构化字段，鼠标悬浮卡单独成行显示；状态行里不再重复。
_HOURS_RE = re.compile(
    r"(?:使用时间|使用|通电时间|通电|运行时间|运行|Power[_ ]?On[_ ]?Hours)"
    r"[:：]?\s*(\d[\d,]*)\s*(?:h|H|小时)?", re.I)
_LIFE_RE = re.compile(r"SSD\s*寿命\s*[:：]?\s*(\d{1,3}\s*%?(?:\s*/\s*\d{1,3}\s*%?)?)", re.I)
# 状态行里属于「附加信息」的段：SSD寿命 / 使用时间 / 通电 …（鼠标卡片单独成行）
_AUX_PART = re.compile(
    r"^(?:SSD\s*寿命|使用时间|使用|通电时间|通电|运行时间|运行)"
    r"\s*[:：]?\s*\d", re.I)


def _strip_aux(text: str) -> str:
    """把状态行按「·」拆开，去掉附加信息段，保留结论。

        '✓ 健康 · SSD寿命 89%/99% · 使用 64745h'  →  '✓ 健康'
    """
    parts = [p.strip() for p in (text or "").split("·")]
    return " · ".join(p for p in parts if p and not _AUX_PART.match(p))


def parse_hours(text: str):
    """从状态文本里取通电小时数，取不到返回 None。"""
    m = _HOURS_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_life(text: str) -> str:
    """从状态文本里取 SSD 寿命（可能是 89% 或 89%/99%），取不到返回 ''。"""
    m = _LIFE_RE.search(text or "")
    if not m:
        return ""
    return re.sub(r"\s+", "", m.group(1))


def _fmt_hours(h) -> str:
    """64745 → '64,745h（7.4 年）'。"""
    if h is None:
        return ""
    try:
        h = int(h)
    except (TypeError, ValueError):
        return ""
    if h >= 8760:
        return f"{h:,}h（{h / 8760:.1f} 年）"
    if h >= 24:
        return f"{h:,}h（{h // 24} 天）"
    return f"{h:,}h"


def _severity(msg: str) -> str:
    """一条 [异常] 行的严重度：'' = 不是问题 | 'warn' = 报警 | 'err' = 错误。

    分档原则（2026-10-18 用户要求）：**一般的性能/趋势问题只报警，严重的才错误**。

    | 级别 | 判据 |
    |---|---|
    | err  | 坏块 / 未纠正读错误 / UECC / 介质错误>0 / 预测失败>0 / 增长缺陷超阈值 / 测速挂起 / OSD 掉线 / HEALTH_ERR |
    | warn | 速度极慢(卡顿) / 速度偏慢 / slow ops / HEALTH_WARN / 增长缺陷(未超阈值) / 延迟 / smartctl 读不到 |

    poll 的 [异常] 行里混了几类东西，只看「有没有 ✗」会大量误报：
      ✓ 开头  —— storcli 卡层「正常」指标（「✓ 介质错误: 0」），值是 0 不是错
      ⚠ 开头  —— 软性警告（「⚠ 其他错误: 6」「⚠ SMART 未知（跳过）」）
      状态:开头 —— generate_report 的结论行，明细另在别行
      ✗ 或无标记 —— 按上面的关键词再分「错误 / 报警」
    """
    m = (msg or "").strip()
    if m.startswith("✓") or m.startswith("状态:"):
        return ""
    # 严重：介质或数据面已经出问题
    if _ERR_PAT.search(m):
        return "err"
    # 一般：性能、趋势、监控覆盖不到 —— 报警即可
    if m.startswith("⚠") or _WARN_PAT.search(m):
        return "warn"
    return "err"


# 严重（错误 / 红灯）
_ERR_PAT = re.compile(
    r"坏块|未纠正|UECC|介质错误:\s*[1-9]|预测失败:\s*[1-9]|"
    r"超阈值|测速挂起|OSD\s*(?:down|掉线)|HEALTH_ERR",
    re.I)
# 一般（报警 / 黄灯）
_WARN_PAT = re.compile(
    r"速度极慢|卡顿|速度偏慢|slow\s*ops|HEALTH_WARN|增长缺陷|"
    r"延迟|latency|smartctl\s*安装失败|SMART\s*未知",
    re.I)


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
    """按面板显示顺序重排盘位。返回写入的盘位数（物理盘位口径）。

    槽号 = **起始物理盘位**，由 _renumber 按各设备的 span 累加算出：
    镜像盘占 2 位，它后面的设备槽号就 +2 而不是 +1。

    **不凭空扩容**：面板尾部那几块「日志发现的盘」跟着一起提交，若直接按序编号
    就会把机箱从 8 位变成 9 位——用户只是拖了下位置，盘位数不该变。规则是
      · 总盘位数 = max(原声明盘位数, 提交内容实际占用的盘位数)
      · 多出来的位置优先丢**空槽**（空槽只是占位，丢了不损失信息），从后往前丢
      · 装了盘的块数真的超过盘位数时才扩容
    """
    d = load()
    s = _ensure(d, server, ip, seed_slots=True)   # 未建档的机器拖动也要能生效
    if s is None or not rows:
        return 0
    log_disks = _log_disks_of(s["name"])
    per_row = _clamp_per_row(s.get("per_row") or DEFAULT_PER_ROW)
    span = lambda dev: _span_of(dev, log_disks, per_row)      # noqa: E731

    declared = sum(span(x.get("dev") or "") for x in s["disks"] if x.get("slot") is not None)
    items = []
    for item in rows:
        dev, osd, note, size = (list(item) + ["", "", "", ""])[:4]
        items.append({"dev": (dev or "").strip(),
                      "osd": (osd or "").strip().lstrip(".").replace("osd.", ""),
                      "note": (note or "").strip(),
                      "size": (size or "").strip()})
    total = max(declared, sum(span(x["dev"]) for x in items if x["dev"]))
    excess = sum(span(x["dev"]) for x in items) - total
    for i in range(len(items) - 1, -1, -1):     # 从后往前丢空槽
        if excess <= 0:
            break
        if not items[i]["dev"]:
            excess -= span(items[i]["dev"])
            items.pop(i)
    while sum(span(x["dev"]) for x in items) < total:   # 不够就补空槽
        items.append({"dev": "", "osd": "", "note": "", "size": ""})
    s["disks"] = [dict(x, slot=i) for i, x in enumerate(items)]
    _renumber(s, log_disks, per_row)            # 槽号 = 起始物理盘位
    save(d)
    return sum(span(x["dev"]) for x in items)


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
    _renumber(s, _log_disks_of(server), _clamp_per_row(s.get("per_row") or DEFAULT_PER_ROW))
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
    out = []
    for row in rows:
        slot, dev, osd, note, size = (list(row) + [""] * 5)[:5]
        try:
            n = int(str(slot).strip())
        except (TypeError, ValueError):
            continue                      # 槽号留空 → 删掉该行
        out.append({"slot": n, "dev": (dev or "").strip(),
                    "osd": (osd or "").strip().lstrip(".").replace("osd.", ""),
                    "note": (note or "").strip(),
                    "size": parse_size(size) or (size or "").strip()})
    # 盘位号只用来定**顺序**，真正的盘位由 _renumber 按 span 累加算出。
    # 用稳定排序：两行填了同一个盘位号时不丢弃任何一行，保持提交顺序相邻排开
    # —— 此前是「同槽号取首个」，用户新增一行填了已占用的号，新盘会被静默吞掉。
    out.sort(key=lambda x: x["slot"])
    # 但**同一块盘不能占两个盘位**：重复的设备名只保留第一行
    dedup, seen_dev = [], set()
    for row in out:
        if row["dev"]:
            if row["dev"] in seen_dev:
                continue
            seen_dev.add(row["dev"])
        dedup.append(row)
    s["disks"] = dedup
    _renumber(s, _log_disks_of(server), _clamp_per_row(s.get("per_row") or DEFAULT_PER_ROW))
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
        _renumber(s, log_disks, _clamp_per_row(s.get("per_row") or DEFAULT_PER_ROW))
        save(d)
    return added


def _renumber(s: dict, log_disks: dict, per_row: int) -> None:
    """把槽号重排成**物理盘位**：每个设备的槽号 = 它在顺序里的起始盘位。

    镜像盘占 2 个盘位，所以它后面的设备槽号要 +2 而不是 +1 ——
    这样面板上标的号、编辑表里的号、poll 报的 e252/sN 三者才对得上。
    """
    s["disks"].sort(key=lambda x: (int(x.get("slot", 0)), (x.get("dev") or "")))
    pos = 0
    for row in s["disks"]:
        row["slot"] = pos
        pos += _span_of(row.get("dev") or "", log_disks, per_row)


def _log_disks_of(name: str) -> dict:
    return (poll_logs()["by_host"].get((name or "").strip()) or {}).get("disks", {}) or {}


def prune_missing() -> int:
    """自动清掉「已经不在机器上」的盘。返回清理的盘数。

    **只清空那一格的内容（dev/osd/note/size），保留它的盘位** ——
    这样同机其它盘一个都不会移位（用户明确要求）。

    两条判据都必须满足，避免误删：
      ① 该主机**正在被轮询**：它最新那条盘记录不超过 HOST_ALIVE。
         整机失联时绝不动手 —— 否则一台机器掉线就会把它的档案全清空。
      ② 这块盘的记录比该主机最新记录早 GONE_AFTER 以上（即连续好几轮轮询
         都没报它了）—— 说明它不在机器上了。

    判据 ② 是**相对**主机自己最新记录的，不是绝对时间：
    轮询停摆时所有盘一起变旧，`newest - ts ≈ 0`，不会有盘被判成「已拔掉」。

    只覆盖「还在日志窗口内、但已经陈旧」的情况；日志滚过窗口之后彻底查不到的，
    走「清理未上报」按钮手动清（那条路无法区分「拔掉了」和「手工录入的盘」）。
    """
    d = load()
    log = poll_logs().get("by_host") or {}
    now, changed = time.time(), 0
    for s in d["servers"]:
        lg = (log.get(s["name"]) or {}).get("disks") or {}
        if not lg:
            continue
        newest = max((e.get("ts") or 0) for e in lg.values())
        if not newest or now - newest > HOST_ALIVE:
            continue                       # 整机没数据 → 不动手
        for row in s["disks"]:
            dev = (row.get("dev") or "").strip()
            if not dev:
                continue
            ent = lg.get(dev)
            if ent is not None and newest - (ent.get("ts") or 0) > GONE_AFTER:
                # 清空这一格，盘位留着 —— 其它盘的位置一个不动
                row.update({"dev": "", "osd": "", "note": "", "size": ""})
                changed += 1
    if changed:
        save(d)
    return changed


def remove_missing(server: str) -> int:
    """删掉档案里「日志查不到记录」的盘。返回删除条数。

    典型场景：坏盘拔掉后，档案里那条还在、盘位图照旧显示它。
    档案是用户排好的布局，不自动改 —— 要清得显式点一下。
    """
    d = load()
    s = get_server(d, (server or "").strip())
    if s is None:
        return 0
    log_disks = _log_disks_of(s["name"])
    keep = [x for x in s["disks"]
            if not (x.get("dev") or "").strip() or (x.get("dev") or "") in log_disks]
    n = len(s["disks"]) - len(keep)
    if n:
        s["disks"] = keep
        _renumber(s, log_disks, _clamp_per_row(s.get("per_row") or DEFAULT_PER_ROW))
        save(d)
    return n


def set_slot_count(server: str, count, ip: str = "") -> int:
    """声明机箱**物理盘位数**：面板显示 0..count-1 号盘位，装了几块亮几块。

    注意一个设备可能占多位（RAID1 镜像占 2 位），所以 count 个盘位未必放得下
    count 个设备。已装盘的设备一个都不删，不够就扩容。返回最终生效的盘位数。
    """
    d = load()
    s = _ensure(d, server, ip, seed_slots=True)
    if s is None:
        return 0
    try:
        n = int(count)
    except (TypeError, ValueError):
        return 0
    if n < 1 or n > 200:
        return 0
    log_disks = _log_disks_of(s["name"])
    per_row = _clamp_per_row(s.get("per_row") or DEFAULT_PER_ROW)
    kept, used = [], 0
    for row in s["disks"]:
        sp = _span_of(row.get("dev") or "", log_disks, per_row)
        # 装了盘的无论如何保留；空槽位只在还有预算时保留
        if (row.get("dev") or "").strip() or used + sp <= n:
            kept.append(row)
            used += sp
    while used < n:                       # 不够就补空盘位（每个 1 位）
        kept.append({"slot": used, "dev": "", "osd": "", "note": "", "size": ""})
        used += 1
    s["disks"] = kept
    _renumber(s, log_disks, per_row)
    save(d)
    return used


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
               "issues": [], "warns": [], "ts": newest}
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
        if info["members"]:
            ent["members"] = info["members"]
        if not ent.get("size"):               # 括号里没有就从正文里抠一次
            for _ep, _tag, body, _o, _a, _ad in batch:
                sz = parse_size(body)
                if sz:
                    ent["size"] = sz
                    break
        ent["issue"] = ent["issues"][0] if ent["issues"] else ""
        ent["issues"] = ent["issues"] + ent["warns"]     # 展示用：异常在前，警告在后
        # poll 在盘点结论后追加的附加信息：SSD 寿命 / 通电使用时间
        ent["hours"] = parse_hours(ent.get("status"))
        ent["life_text"] = parse_life(ent.get("status"))

        # 容量已知且过小 → 不是硬盘（虚拟块设备/残留），不进盘位图。
        # 容量认不出来的照常保留，宁可多显示也不误杀。
        tbv = size_tb(ent.get("size") or "")
        if tbv is not None and tbv < MIN_SIZE_TB:
            h = by_host.setdefault(host, {"ip": "", "disks": {}, "poll_errors": []})
            h.setdefault("small", {})[dev] = ent.get("size", "")
            continue
        h = by_host.setdefault(host, {"ip": "", "disks": {}, "poll_errors": []})
        h["disks"][dev] = ent

    # 拔掉的盘不该继续显示：日志窗口里还留着它最后一次的记录，
    # 于是「档案已清空（槽位空）」但「日志侧仍当成新发现的盘」挂在「未归档」里。
    # 用与 prune_missing 完全相同的判据把它剔掉：
    #   主机**正在被轮询**（最新记录不超过 HOST_ALIVE），且这块盘比主机最新记录
    #   旧 GONE_AFTER 以上（连续好几轮都没报它了）→ 不在机器上了，连「未归档」都不给。
    #   整机失联时不动手 —— 否则一台机器掉线，它的盘会全部消失。
    now_ts = time.time()
    for host, h in by_host.items():
        ds = h.get("disks") or {}
        if not ds:
            continue
        newest = max((e.get("ts") or 0) for e in ds.values())
        if not newest or now_ts - newest > HOST_ALIVE:
            continue
        for dev in [d for d, e in ds.items() if newest - (e.get("ts") or 0) > GONE_AFTER]:
            ds.pop(dev, None)

    for host, a in host_addr.items():
        h = by_host.setdefault(host, {"ip": "", "disks": {}, "poll_errors": []})
        h.update(a)
    for host, errs in host_errors.items():
        h = by_host.setdefault(host, {"ip": "", "disks": {}, "poll_errors": []})
        # 只留最近一批主机级失败
        newest_err = max(e[0] for e in errs)
        batch = [b for ep, b in sorted(errs) if ep >= newest_err - BURST_SLACK]
        # 还要跟「最近一次**成功**」比：这台机器后来查成功过，早先那次失败就是陈旧的。
        # 否则一天前失败过一次，之后一直正常，徽章却永远挂着「轮询异常」。
        # （主机整体查不通时不会产生盘记录，所以「盘记录比错误新」就说明后来成功了）
        newest_ok = max((e.get("ts") or 0) for e in (h.get("disks") or {}).values()) if h.get("disks") else 0
        h["poll_errors"] = [] if newest_ok > newest_err else batch

    data = {"by_host": by_host, "runs": runs[:12]}
    _logs_cache["data"], _logs_cache["ts"] = data, now
    return data


# ---------------- 单盘历史（警告及以上） ----------------

_HIST_HOST_RE = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
_HIST_DEV_RE = re.compile(r'^/dev/[A-Za-z0-9._-]{1,32}$')
_MSG_TS_RE = re.compile(r'\[异常\]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
_hist_cache = {}          # (server, dev, days) -> (ts, payload)
_HIST_TTL = 60


def _hist_text(msg: str) -> str:
    """从一条 poll 异常行里抠出结论（保留开头的 状态:/✓/⚠/✗，供 _severity 判定）。

        [异常] 2026-10-18 13:40:50  /dev/sdd[ … ]  m4-nod11(…)  osd.55 : ✗ 速度极慢! …
        → ✗ 速度极慢! 硬盘当前处于卡顿状态
    """
    return (msg or "").rsplit(" : ", 1)[-1].strip()


def history(server: str, dev: str, days: int = 30, limit: int = 20) -> dict:
    """单盘「警告及以上」历史（近 days 天），按结论去重折叠。

    数据源就是 VictoriaLogs 里的 osd-poll 日志 —— 不另建库。
    同一结论多次出现折叠成一条：{text, level, count, first, last}。
    """
    server = (server or "").strip()
    dev = (dev or "").strip()
    if not _HIST_HOST_RE.match(server) or not _HIST_DEV_RE.match(dev):
        return {"entries": [], "error": "参数不合法", "days": days}
    try:
        days = max(1, min(int(days or 30), 180))
    except (TypeError, ValueError):
        days = 30
    try:
        limit = max(1, min(int(limit or 20), 100))
    except (TypeError, ValueError):
        limit = 20

    key = (server, dev, days)
    now = time.time()
    hit = _hist_cache.get(key)
    if hit and now - hit[0] < _HIST_TTL:
        return hit[1]

    expr = f'app_name:osd-poll AND _msg:~"{server}" AND _msg:~"{dev}"'
    logs = vl.query_logs(expr=expr, limit=2000,
                         start=int(now - days * 86400), end=int(now))

    groups = {}
    for rec in logs:
        msg = _ANSI_RE.sub("", rec.get("_msg") or "").strip()
        if "[异常]" not in msg:
            continue
        raw = _hist_text(msg)                 # 结论（保留 状态:/✓/⚠/✗ 前缀）
        sev = _severity(raw)                  # ← 用结论判定，不能用整行
        if not sev:
            continue
        text = re.sub(r"^状态:\s*", "", raw).strip()   # 「状态: ✗ 异常」本身没信息量
        if not text:
            continue
        m = _MSG_TS_RE.search(msg)
        ts = m.group(1) if m else (rec.get("_time") or "")[:16].replace("T", " ")
        g = groups.get(text)
        if g is None:
            groups[text] = {"text": text, "level": sev, "count": 1,
                            "first": ts, "last": ts}
        else:
            g["count"] += 1
            if ts < g["first"]:
                g["first"] = ts
            if ts > g["last"]:
                g["last"] = ts
            if sev == "err":
                g["level"] = "err"          # 同一结论只要出现过错误级，就按错误算

    entries = sorted(groups.values(), key=lambda x: x["last"], reverse=True)[:limit]
    payload = {"entries": entries, "days": days, "host": server, "dev": dev}
    _hist_cache[key] = (now, payload)
    return payload


# ---------------- 合并视图 ----------------

def merged(include_hosts: bool = True) -> dict:
    """布局 + 日志 → 渲染用模型。日志只覆盖「归属/状态/异常」，不改布局。"""
    d = load()
    logs = poll_logs()
    by_host = logs["by_host"]
    dslots = int(d["settings"].get("default_slots") or DEFAULT_SLOTS)

    from . import actions
    manual = actions.manual_checks()

    servers = []
    seen = set()
    for s in d["servers"]:
        sv = _assemble(s, by_host.get(s["name"]), default_slots=dslots)
        _apply_manual(sv["disks"], s["name"], manual)
        servers.append(_recount(sv))
        seen.add(s["name"])
    # 日志里出现但布局中没有的主机：只读展示，提示「未建档」
    for name, h in sorted(by_host.items()):
        if name in seen:
            continue
        sv = _assemble({"name": name, "ip": h.get("ip", ""),
                                  "mode": d["settings"].get("default_mode"),
                                  "orient": d["settings"].get("default_orient"),
                                  "per_row": d["settings"]["default_per_row"],
                                  "disks": []}, h, auto=True, default_slots=dslots)
        _apply_manual(sv["disks"], name, manual)
        servers.append(_recount(sv))
    # hosts 文件里未建档的：占位（有 IP 无盘位）
    if include_hosts:
        for row in fetch_hosts():
            if row["name"] in seen or row["name"] in by_host:
                continue
            sv = _assemble(
                {"name": row["name"], "ip": row["ip"],
                 "mode": d["settings"].get("default_mode"),
                 "orient": d["settings"].get("default_orient"),
                 "per_row": d["settings"]["default_per_row"],
                 "disks": [], "unbuilt": True}, None, auto=True, default_slots=dslots)
            servers.append(_recount(sv))
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
    """黄灯的三种来源，标签分清楚：
       有 ⚠ 明细   → 警告
       日志里查不到该盘 → 无记录（盘可能已拔掉，或设备名变了）
       其它         → 未知
    """
    if st == "warn":
        if (ent or {}).get("warns"):
            return "警告"
        return "未知" if ent else "无记录"
    return _STATE_LABEL.get(st, "")


def _disk_state(ent: dict, has_dev: bool) -> str:
    """绿=确认健康 / 黄=报警 / 红=错误。

    判据以**最近一次轮询**为准（poll_logs 已按盘归拢到最新一批）：
      有 err 级明细            → 红
      只有 warning 级明细      → 黄（速度慢、slow ops、增长缺陷未超阈值…）
      盘点结论 ✗ 但没有明细     → 红（不明原因的错误）
      盘点正文里的 ⚠           → 黄
      ✓                       → 绿
      有设备但日志没这条盘的记录 → 黄（未验证）
    """
    if not ent:
        return "warn" if has_dev else "none"
    s = (ent.get("status") or "").strip()
    if ent.get("issue"):
        return "err"                      # 有 err 级明细
    if ent.get("warns"):
        return "warn"                     # 只有 warning 级明细
    if s.startswith("✗"):
        return "err"                      # 结论 ✗ 却没有明细：宁可报红
    if s.startswith("⚠") or "⚠" in s:
        return "warn"
    if s.startswith("✓"):
        return "ok"
    return "warn" if has_dev else "none"


def _status_text(ent) -> str:
    """盘位显示的短状态：异常时给出主因，否则回显日志原文。

    「SSD寿命 / 使用时间」这类附加段单独成行显示（悬浮卡），不塞在状态里。
    """
    e = ent or {}
    if e.get("issue"):
        return e["issue"]
    st = (e.get("status") or "").strip()
    return _strip_aux(st)


SPAN_MAX = 8               # 单个设备最多占几个物理盘位（RAID5/6 也不会更多了）

# 小于这个容量的设备不当硬盘看。实测 m4-nod12 的 /dev/rbd0 只有 4M ——
# Ceph RBD 的虚拟块设备，不是物理盘，混进盘位图毫无意义。
# 阈值取 100G：全量盘表里真实物理盘最小 278.5G，4M~100G 之间没有任何设备，
# 不会误杀（2026-10-18 用户要求「屏蔽小于 100G 的盘」）。
MIN_SIZE_TB = 0.1         # 100G

# 自动清理「已不在机器上」的盘（见 prune_missing）
GONE_AFTER = 2 * 3600     # 相对该主机最新记录，超过这么久没上报 → 认为已拔掉
                          # （轮询约半小时~1 小时一轮，2 小时 ≈ 连续 2~4 轮都没报它）
STALE_AFTER = 26 * 3600   # 面板数据算「陈旧」的阈值。轮询基础节奏是**每天 00:30 一次**
                          # （其余是零散的手工运行），所以 26 小时 = 漏掉了当天的定时那一次。
                          # 定 2 小时会让一整天大部分时间都标黄，纯噪音。
HOST_ALIVE = 12 * 3600    # 该主机最新记录超过这么久没更新 → 整机失联，不动手


def _span_of(dev: str, log_disks: dict, per_row: int = 0) -> int:
    """这个设备占几个**物理盘位**。

    poll 的 RAID1(e252/s0+e252/s1) 报两个物理槽位 —— 一块 /dev/sdX 实际是两块盘，
    机箱里就占两个位置。取不到槽位信息的按 1 位。

    **不再按每排数量截断**：竖排时每排只有 1 格，截断会让 RAID1 的两块盘塌成 1 个盘位，
    盘位数凭空少一个（实测 x5-nod18 竖排下 8 盘位显示成 7）。
    视觉上占几列由 `place` 单独控制（见 _assemble）。
    """
    ent = (log_disks or {}).get((dev or "").strip()) or {}
    n = max(len(ent.get("pslots") or []), len(ent.get("members") or []), 1)
    return min(n, SPAN_MAX)


def _ago(ts: float) -> str:
    """相对时间：刚刚 / N 分钟前 / N.N 小时前 / N 天前。"""
    if not ts:
        return ""
    d = max(0.0, time.time() - ts)
    if d < 90:
        return "刚刚"
    if d < 5400:
        return "%d 分钟前" % round(d / 60)
    if d < 2 * 86400:
        return "%.1f 小时前" % (d / 3600)
    return "%d 天前" % round(d / 86400)


def _pshort(pslots) -> str:
    """['252/0','252/1'] → 's0+s1'（去掉 EID，只留槽位号）。"""
    return "+".join("s" + p.split("/")[-1] for p in (pslots or []) if "/" in p)


def _log_fields(ent) -> dict:
    """日志侧带出来的硬件信息（档案里不存，每次从日志现取）。"""
    e = ent or {}
    ps = e.get("pslots") or []
    car = e.get("carrier", "")
    return {"media": e.get("media", ""), "role": e.get("role", ""),
            "carrier": car, "pslots": ps, "pshort": _pshort(ps),
            "raid": car.split("(")[0] if car else "",
            "dclass": e.get("dclass", ""),
            "hours": e.get("hours"),
            "hours_text": _fmt_hours(e.get("hours")),
            "life_text": e.get("life_text", "")}


def _recount(sv: dict) -> dict:
    """按 sv['disks'] 的当前状态重算卡片级统计。

    手工检测覆盖完单盘状态后要重算一遍，否则卡片头部的「N 异常」和徽章颜色
    还是覆盖前的旧值。
    """
    disks_ = sv["disks"]
    poll_errors = sv.get("poll_errors") or []
    sv["bad"] = bad = sum(1 for x in disks_ if x["state"] == "err")
    sv["unknown"] = unknown = sum(1 for x in disks_ if x["state"] == "warn")
    sv["installed"] = sum(1 for x in disks_ if x["dev"] and x["slot"] is not None)
    sv["installed_any"] = sum(1 for x in disks_ if x["dev"])
    if bad:
        sv["state"], sv["label"] = "err", f"{bad} 异常"
    elif unknown or poll_errors or sv.get("unbuilt"):
        sv["state"] = "warn"
        if unknown:
            # 格子上分「警告」（有 ⚠ 明细）和「无记录」（日志查不到），
            # 卡片汇总也照这个口径说清楚，别一律叫「未知」
            warns = sum(1 for x in disks_ if x["state"] == "warn" and x.get("issues"))
            miss = sum(1 for x in disks_ if x["state"] == "warn"
                       and not x.get("issues") and not x["known"] and x["dev"])
            parts = []
            if warns:
                parts.append(f"{warns} 警告")
            if miss:
                parts.append(f"{miss} 无记录")
            sv["label"] = " · ".join(parts) if parts else f"{unknown} 未知"
        else:
            sv["label"] = "未建档" if sv.get("unbuilt") else "轮询异常"
    else:
        sv["state"], sv["label"] = ("ok", "全部健康") if disks_ else ("none", "")
    return sv


def _apply_manual(disks_: list, host: str, manual: dict) -> None:
    """手工单盘检测的结果比日志里那条新时，用它覆盖状态。

    osd.check.sh 单独跑的输出只落在终端，不进 VictoriaLogs，所以「右键检测完
    刷新页面能看到新状态」只能靠这份本地记录。下一轮 poll 出了更新的记录后，
    它自然被压过去。
    """
    mc = (manual or {}).get(host) or {}
    for x in disks_:
        m = mc.get(x.get("dev") or "")
        if not m:
            continue
        if m.get("ts", 0) <= (x.get("_poll_ts") or 0):
            continue
        st = (m.get("status") or "").strip()
        if m.get("issue") or st.startswith("✗"):
            x["state"], x["label"] = "err", "异常"
        elif st.startswith("⚠"):
            x["state"], x["label"] = "warn", "警告"
        elif st.startswith("✓"):
            x["state"], x["label"] = "ok", "健康"
        x["status"] = m.get("issue") or st
        x["issues"] = [m["issue"]] if m.get("issue") else []
        x["manual"] = int(m.get("ts", 0))
        x["manual_at"] = time.strftime("%m-%d %H:%M", time.localtime(m.get("ts", 0)))
        x["manual_status"] = st


def _assemble(s: dict, log_h: dict, auto: bool = False, default_slots: int = DEFAULT_SLOTS) -> dict:
    """把一台服务器的布局与日志合并成渲染行。"""
    mode = _clamp_mode(s.get("mode"))
    orient = _clamp_orient(s.get("orient") or DEFAULT_ORIENT)
    h_per_row = _clamp_per_row(s.get("per_row") or DEFAULT_PER_ROW)
    per_row = 1 if mode == "v" else h_per_row   # 竖排：一列；横排：多列
    log_disks = (log_h or {}).get("disks", {}) or {}
    # 没建过档的机器：先按默认盘位数摆一排空槽位，比空面板有信息量，
    # 也给了「日志发现的盘」一个可拖进去的目标。真实档案一旦存在就用档案。
    small = (log_h or {}).get("small") or {}      # 容量过小、不算硬盘的设备
    # 档案里也可能留着过小的设备（如 m4-nod12 的 /dev/rbd0）。轮询侧一旦把它
    # 剔掉，`prune_missing()` 就再也看不到它（它只遍历日志里还在的盘），
    # 于是这条档案会永远挂在面板上。所以这里按**档案自己的容量**再挡一次。
    small = dict(small)
    for _row in (s.get("disks") or []):
        _dev = (_row.get("dev") or "").strip()
        _tb = size_tb(_row.get("size") or "")
        if _dev and _tb is not None and _tb < MIN_SIZE_TB:
            small.setdefault(_dev, _row.get("size", ""))
    layout = [x for x in (s.get("disks") or []) if (x.get("dev") or "") not in small]
    if not layout and not s.get("disks"):
        layout = [{"slot": i, "dev": "", "osd": "", "note": ""}
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
            "known": bool(ent), "_poll_ts": (ent or {}).get("ts", 0),
            "state": st, "label": _state_label(st, ent),
            "span": _span_of(dev, log_disks, per_row),
            "place": min(_span_of(dev, log_disks, per_row), per_row),
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
                      "known": True, "unfiled": True, "_poll_ts": ent.get("ts", 0),
                      "state": st, "label": _state_label(st, ent),
                      "span": _span_of(dev, log_disks, per_row),
                      "place": min(_span_of(dev, log_disks, per_row), per_row),
                      **_log_fields(ent)})
    disks.sort(key=lambda x: (x["slot"] is None, x["slot"] if x["slot"] is not None else 0))
    # 面板分行：每排 per_row 个
    # 行打包：每排 per_row 个**物理盘位**，横跨 2 格的镜像盘吃掉 2 个位置。
    # 放不下就换行，避免 CSS grid 把跨格元素挤到下一行留下空洞。
    def pack(items, bay0=0):
        """按**视觉占位**（place）分行，按**物理盘位数**（span）编号。

        两者在竖排下不相等：镜像盘视觉上只占 1 列（每排就 1 格），
        但机箱里实打实占 2 个盘位 —— 编号必须按 2 走，否则会跟后一格重号。
        """
        out, cur = [], []
        used, bay = 0, bay0
        for x in items:
            sp = x["place"]
            if cur and used + sp > per_row:
                out.append({"cells": cur, "used": used})
                cur, used = [], 0
            x["bay"] = bay
            bay += x["span"]
            cur.append(x)
            used += sp
        if cur:
            out.append({"cells": cur, "used": used})
        return out, bay

    framed = [x for x in disks if x["slot"] is not None]
    extra = [x for x in disks if x["slot"] is None]
    rows, nxt_bay = pack(framed)
    layout_rows = len(rows)
    if extra:
        extra_rows, _ = pack(extra, bay0=nxt_bay)
        rows += extra_rows

    # 右灯带：每条灯带挂它**右边**那块盘。右边没人盖住它时，这块盘就把自己的
    # 右灯带按**自己的颜色**画出来。三种情况要补：
    #   ① 这一排里右边是空槽   —— 空槽不画灯带，盖不住
    #   ② 行末（横排）        —— 换行处右边就是空的；不补的话这排右边缘是秃的
    #   ③ 最后一行的末格      —— 整条导轨的收尾
    # 竖排每行只有一格，按 ② 补会变成「右条 + 缝 + 下格左条」的双线，所以排除。
    row_end = set()
    for row in rows:
        if row["cells"]:
            row_end.add(id(row["cells"][-1]))
    flat = [x for row in rows for x in row["cells"]]
    for i, x in enumerate(flat):
        if not x.get("dev"):
            x["right_bar"] = False                      # 空槽自己不画灯带
        elif per_row == 1:
            # **竖排**：上下相邻，两条灯带是**上下接续**、不在同一个位置，
            # 谁也不盖谁 —— 所以每格左右都画（「单盘 | HDD |」就是这个意思）。
            x["right_bar"] = True
        else:
            # **横排**：左右相邻，中缝处「右边盘的左灯带」盖住「左边盘的右灯带」，
            # 只留一条；只有下面三种情况才补自己的右灯带：
            nxt = flat[i + 1] if i + 1 < len(flat) else None
            x["right_bar"] = (nxt is None                     # ① 序列末尾（收尾）
                              or not nxt.get("dev")           # ② 右边是空槽（盖不住）
                              or id(x) in row_end)            # ③ 行末（换行处右边是空的）
    # 盘位数 = 机箱物理盘位总数（含空槽），日志发现的「未建档」行不算进机箱
    total_units = sum(x["span"] for x in framed)
    # 工具条里「共 N 排」用的是**横排口径**（每排 h_per_row），与当前面板是否竖排无关。
    # 按物理盘位算 —— 镜像盘占 2 个位置，「每排 8 × 3 排」就是 24 个盘位。
    h_rows = (-(-total_units // h_per_row)) if total_units else 0
    # 数据更新时间：这台机器最近一次轮询上报是什么时候。
    # 比 GONE_AFTER 还旧就该打个问号了 —— 面板上的状态可能已经不是现状。
    up_ts = max((e.get("ts") or 0 for e in log_disks.values()), default=0)
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
    # 档案里有、日志里查不到记录的盘（多半是拔掉了，或设备名变了）
    missing = [x["dev"] for x in disks if x["dev"] and not x["known"] and x["slot"] is not None]
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
            # slots = 机箱**物理盘位**总数（一个 RAID1 设备占 2 个）；devices = 设备数
            "slots": total_units, "devices": sum(1 for x in framed if x["dev"]),
            "phys": sum(x["span"] for x in disks if x["dev"]),
            "layout_rows": layout_rows, "h_rows": h_rows,
            "auto": auto, "unbuilt": s.get("unbuilt", False),
            # count = 面板格子数（含空槽）；installed = **有槽号的**格子里装了盘的数量。
            # 日志发现但未建档的盘没有槽号、不占机箱槽位，故不计入 installed ——
            # 否则卡片头部「已装 1 / 共 9 槽」会和工具条「总槽位数 8」对不上。
            # 这些盘由 unfiled 单独计数，头部另有一枚「未建档」标签说明。
            "count": len(disks),
            "installed": sum(1 for x in disks if x["dev"] and x["slot"] is not None),
            "installed_any": sum(1 for x in disks if x["dev"]),
            "size_known": len(known), "total_size": total,
            "ignored": [{"dev": d, "size": z} for d, z in sorted(small.items())],
            "missing": missing,
            "media": media, "sys_count": sysn, "classes": classes,
            "updated_ts": up_ts, "updated": _ago(up_ts),
            "updated_at": (time.strftime("%m-%d %H:%M", time.localtime(up_ts)) if up_ts else ""),
            "stale": bool(up_ts and time.time() - up_ts > STALE_AFTER),
            "bad": bad, "unknown": unknown,
            "poll_errors": poll_errors,
            "unfiled": sum(1 for x in disks if x.get("unfiled")),
            "state": st, "label": label}
