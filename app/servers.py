"""Server registry: persisted in config/servers.yaml."""
import yaml
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
REG_PATH = BASE / "config" / "servers.yaml"


def load_registry() -> list:
    try:
        d = yaml.safe_load(REG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        d = {}
    return d.get("servers", [])


def save_registry(servers: list) -> None:
    REG_PATH.write_text(
        yaml.safe_dump({"servers": servers}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def add_server(name: str, ip: str = "", stype: str = "", note: str = "") -> None:
    s = load_registry()
    name = (name or "").strip()
    if name and not any(x.get("name") == name for x in s):
        s.append({"name": name, "ip": (ip or "").strip(), "type": (stype or "").strip(), "note": (note or "").strip()})
        save_registry(s)


def remove_server(name: str) -> None:
    save_registry([x for x in load_registry() if x.get("name") != name])
