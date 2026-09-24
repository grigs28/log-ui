"""Server registry + ignore list, persisted in config/servers.yaml.

ignored 项结构: {name, ip, type}。老格式（裸字符串）读取时自动规范化，
首次写回时升级——生产机的 servers.yaml 不需要手工迁移。
"""
import re
import yaml
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
REG_PATH = BASE / "config" / "servers.yaml"

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _norm_ignored(raw) -> list:
    """ignored 规范化为 [{name, ip, type}]；兼容老格式裸字符串。"""
    out = []
    for x in raw or []:
        if isinstance(x, str):
            out.append({"name": x, "ip": "", "type": ""})
        elif isinstance(x, dict) and x.get("name"):
            out.append({"name": x["name"], "ip": x.get("ip", ""), "type": x.get("type", "")})
    return out


def _load_all():
    try:
        d = yaml.safe_load(REG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        d = {}
    return d.get("servers", []), _norm_ignored(d.get("ignored"))


def _save_all(servers: list, ignored: list) -> None:
    REG_PATH.write_text(
        yaml.safe_dump({"servers": servers, "ignored": ignored},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def load_registry() -> list:
    s, _ = _load_all()
    return s


def load_ignore() -> list:
    _, i = _load_all()
    return [x["name"] for x in i]


def load_ignore_full() -> list:
    _, i = _load_all()
    return i


def add_server(name: str, ip: str = "", stype: str = "", note: str = "") -> None:
    s, i = _load_all()
    name = (name or "").strip()
    if name and not any(x.get("name") == name for x in s):
        s.append({"name": name, "ip": (ip or "").strip(), "type": (stype or "").strip(),
                  "note": (note or "").strip()})
        _save_all(s, i)


def remove_server(name: str) -> None:
    s, i = _load_all()
    _save_all([x for x in s if x.get("name") != name], i)


def update_server(orig_name: str, name: str, ip: str = "", stype: str = "", note: str = "") -> None:
    s, i = _load_all()
    orig = (orig_name or "").strip()
    new_name = (name or "").strip() or orig
    for x in s:
        if x.get("name") == orig:
            x["name"] = new_name
            x["ip"] = (ip or "").strip()
            x["type"] = (stype or "").strip()
            x["note"] = (note or "").strip()
            break
    _save_all(s, i)


def add_ignore(name: str, ip: str = "", stype: str = "") -> None:
    s, i = _load_all()
    name = (name or "").strip()
    if name and not any(x["name"] == name for x in i):
        # 已注册主机被忽略时带走登记的 IP/类型；名称本身是 IP 时自动带上
        if not ip and _IP_RE.match(name):
            ip = name
        i.append({"name": name, "ip": (ip or "").strip(), "type": (stype or "").strip()})
        _save_all(s, i)


def remove_ignore(name: str) -> None:
    s, i = _load_all()
    _save_all(s, [x for x in i if x["name"] != name])
