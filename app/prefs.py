"""Per-user preferences (stored as JSON under config/prefs/)."""
import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PREFS_DIR = BASE / "config" / "prefs"
DEFAULTS = {"refresh_sec": 10, "limit": 100, "theme": "dark"}


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name)) or "user"


def load_prefs(username: str) -> dict:
    p = PREFS_DIR / f"{_safe(username)}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {**DEFAULTS, **data}


def save_prefs(username: str, prefs: dict) -> None:
    PREFS_DIR.mkdir(parents=True, exist_ok=True)
    (PREFS_DIR / f"{_safe(username)}.json").write_text(
        json.dumps(prefs), encoding="utf-8"
    )
