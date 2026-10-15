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
DEFAULT_PER_ROW = 4
PER_ROW_RANGE = (1, 24)

# [盘点] /dev/sda[ 系统盘 ]  v5-ctr02(192.168.93.92)  osd.2 : ✓ 健康
_POLL_RE = re.compile(
    r'^\[(?P<tag>盘点|异常)\]\s+'
    r'(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+'
    r'(?P<dev>/dev/[^\s\[]+)(?:\[(?P<attr>[^\]]*)\])?\s+'
    r'(?P<host>[^\s(]+)\((?P<ip>[^)]*)\)\s+'
    r'osd\.(?P<osd>[^\s]+)\s+:\s+(?P<msg>.*)$'
)

_hosts_cache = {"ts": 0.0, "rows": []}
_logs_cache = {"ts": 0.0, "data": None}
_HOSTS_TTL = 600      # hosts 文件缓存 10 分钟
_LOGS_TTL = 120       # 日志解析缓存 2 分钟


# ---------------- 布局（可编辑，持久化） ----------------

def _blank():
    return {"settings": {"default_per_row": DEFAULT_PER_ROW,
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
             "per_row": _clamp_per_row(per_row or d["settings"]["default_per_row"]),
             "disks": []}
        d["servers"].append(s)
    else:
        if ip:
            s["ip"] = ip.strip()
        if per_row is not None:
            s["per_row"] = _clamp_per_row(per_row)
        s.setdefault("disks", [])
    save(d)
    return d


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
    """整表保存。rows = [(slot, dev, osd, note), ...]；槽号留空的行即删除。"""
    d = load()
    s = get_server(d, (server or "").strip())
    if s is None:
        return
    if per_row is not None:
        s["per_row"] = _clamp_per_row(per_row)
    out, seen = [], set()
    for slot, dev, osd, note in rows:
        try:
            n = int(str(slot).strip())
        except (TypeError, ValueError):
            continue                      # 槽号留空 → 删掉该行
        if n in seen:
            continue                      # 同槽号重复提交，取首个
        seen.add(n)
        out.append({"slot": n, "dev": (dev or "").strip(),
                    "osd": (osd or "").strip().lstrip(".").replace("osd.", ""),
                    "note": (note or "").strip()})
    s["disks"] = sorted(out, key=lambda x: x["slot"])
    save(d)


def fill_slots(server: str, count) -> None:
    """按数量补齐 0..count-1 的槽位（已有槽位保留，不覆盖）。"""
    d = load()
    s = get_server(d, (server or "").strip())
    if s is None:
        return
    try:
        count = int(count)
    except (TypeError, ValueError):
        return
    if count < 1 or count > 200:
        return
    have = {int(x.get("slot", -1)) for x in s["disks"]}
    for i in range(count):
        if i not in have:
            s["disks"].append({"slot": i, "dev": "", "osd": "", "note": ""})
    s["disks"].sort(key=lambda x: int(x.get("slot", 0)))
    save(d)


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

def poll_logs() -> dict:
    """解析 osd-poll 日志 → {'dev': {dev: {...}}, 'host': {...}, 'runs': [...]}。

    返回结构：
      by_host: {host: {'ip':.., 'disks': {dev: {'osd','attr','status','issue'}}}}
      runs:    [{'ts','start','end','osds','hosts','issues','ok'}]（轮询记录）
    """
    now = time.time()
    if _logs_cache["data"] is not None and now - _logs_cache["ts"] < _LOGS_TTL:
        return _logs_cache["data"]

    logs = vl.query_logs(expr="app_name:osd-poll", limit=2000)
    by_host = {}
    runs = []
    cur = None
    for rec in logs:
        msg = rec.get("_msg") or ""
        m = _POLL_RE.match(msg)
        if m:
            host = m.group("host")
            h = by_host.setdefault(host, {"ip": m.group("ip"), "disks": {}})
            if m.group("ip"):
                h["ip"] = m.group("ip")
            dev = m.group("dev")
            ent = h["disks"].setdefault(dev, {})
            ent["osd"] = m.group("osd") if m.group("osd") != "?" else ""
            ent["attr"] = (m.group("attr") or "").strip()
            if m.group("tag") == "异常":
                ent["issue"] = m.group("msg").strip()
                # 结论行「✗ 异常」不算详情，仍以是否有 issue 判异常
            else:
                ent["status"] = m.group("msg").strip()
            continue
        if "[开始]" in msg:
            cur = {"ts": rec.get("_time", ""), "lines": [msg.strip()]}
            runs.append(cur)
        elif "[结束]" in msg:
            if cur is not None:
                cur["lines"].append(msg.strip())
            cur = None
    data = {"by_host": by_host, "runs": runs[:12]}
    _logs_cache["data"], _logs_cache["ts"] = data, now
    return data


# ---------------- 合并视图 ----------------

def merged(include_hosts: bool = True) -> dict:
    """布局 + 日志 → 渲染用模型。日志只覆盖「归属/状态/异常」，不改布局。"""
    d = load()
    logs = poll_logs()
    by_host = logs["by_host"]

    servers = []
    seen = set()
    for s in d["servers"]:
        servers.append(_assemble(s, by_host.get(s["name"])))
        seen.add(s["name"])
    # 日志里出现但布局中没有的主机：只读展示，提示「未建档」
    for name, h in sorted(by_host.items()):
        if name in seen:
            continue
        servers.append(_assemble({"name": name, "ip": h.get("ip", ""),
                                  "per_row": d["settings"]["default_per_row"],
                                  "disks": []}, h, auto=True))
    # hosts 文件里未建档的：占位（有 IP 无盘位）
    if include_hosts:
        for row in fetch_hosts():
            if row["name"] in seen or row["name"] in by_host:
                continue
            servers.append(_assemble(
                {"name": row["name"], "ip": row["ip"],
                 "per_row": d["settings"]["default_per_row"],
                 "disks": [], "unbuilt": True}, None, auto=True))
    servers.sort(key=lambda x: x["name"])
    stats = {
        "servers": len(servers),
        "disks": sum(len(s["disks"]) for s in servers),
        "bad": sum(s["bad"] for s in servers),
        "unbuilt": sum(1 for s in servers if s.get("unbuilt")),
    }
    return {"settings": d["settings"], "servers": servers,
            "runs": logs["runs"], "stats": stats}


# 状态 → (徽章类, 短标签)：绿=日志确认健康；红=有异常；黄=已录但日志无数据/未建档
_STATE_LABEL = {"ok": "健康", "err": "异常", "warn": "未知", "none": ""}


def _disk_state(ent: dict, has_dev: bool) -> str:
    if ent:
        if ent.get("issue"):
            return "err"
        return "warn" if "⚠" in (ent.get("status") or "") else "ok"
    return "warn" if has_dev else "none"


def _assemble(s: dict, log_h: dict, auto: bool = False) -> dict:
    """把一台服务器的布局与日志合并成渲染行。"""
    per_row = _clamp_per_row(s.get("per_row") or DEFAULT_PER_ROW)
    log_disks = (log_h or {}).get("disks", {}) or {}
    disks = []
    used_devs = set()
    for row in s.get("disks") or []:
        dev = (row.get("dev") or "").strip()
        ent = log_disks.get(dev) if dev else None
        used_devs.add(dev)
        st = _disk_state(ent, bool(dev))
        disks.append({
            "slot": int(row.get("slot", 0)),
            "dev": dev,
            "osd": (row.get("osd") or "").strip() or (ent or {}).get("osd", ""),
            "attr": (ent or {}).get("attr", ""),
            "note": (row.get("note") or "").strip(),
            "issue": (ent or {}).get("issue", ""),
            "status": ("异常" if (ent or {}).get("issue")
                       else (ent or {}).get("status", "")),
            "known": bool(ent),
            "state": st, "label": _STATE_LABEL[st],
        })
    # 日志里有、布局里没录的盘：自动补进图（提示未建档）
    for dev, ent in sorted(log_disks.items()):
        if dev in used_devs:
            continue
        st = _disk_state(ent, True)
        disks.append({"slot": None, "dev": dev, "osd": ent.get("osd", ""),
                      "attr": ent.get("attr", ""), "note": "", "issue": ent.get("issue", ""),
                      "status": ("异常" if ent.get("issue") else ent.get("status", "")),
                      "known": True, "unfiled": True,
                      "state": st, "label": _STATE_LABEL[st]})
    disks.sort(key=lambda x: (x["slot"] is None, x["slot"] if x["slot"] is not None else 0))
    # 面板分行：每排 per_row 个
    slots = [x for x in disks if x["slot"] is not None]
    extra = [x for x in disks if x["slot"] is None]
    rows = [slots[i:i + per_row] for i in range(0, len(slots), per_row)]
    if extra:
        rows.append(extra)
    bad = sum(1 for x in disks if x["state"] == "err")
    unknown = sum(1 for x in disks if x["state"] == "warn")
    if bad:
        st = "err"
    elif unknown or s.get("unbuilt"):
        st = "warn"
    else:
        st = "ok" if disks else "none"
    return {"name": s["name"], "ip": (s.get("ip") or (log_h or {}).get("ip", "")),
            "per_row": per_row, "rows": rows, "disks": disks,
            "auto": auto, "unbuilt": s.get("unbuilt", False),
            "count": len(disks), "bad": bad, "unknown": unknown,
            "state": st,
            "label": f"{bad} 异常" if bad else (f"{unknown} 未知" if unknown else
                                               ("未建档" if s.get("unbuilt") else "全部健康"))}
