"""Settings: loaded from config/settings.yaml. SSO values are read live (editable)."""
import yaml
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
_CFG_PATH = BASE / "config" / "settings.yaml"


def _load():
    return yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8"))


_cfg = _load()
VL_URL = _cfg["vl_url"].rstrip("/")
CALLBACK_URL = _cfg.get("callback_url", "")
LISTEN_HOST = _cfg.get("listen_host", "0.0.0.0")
LISTEN_PORT = int(_cfg.get("listen_port", 80))
SECRET_KEY = _cfg["secret_key"]


def get_flag(key: str, default: bool = True) -> bool:
    """布尔开关（如 selfheal），读不到取默认值。"""
    c = _load()
    v = c.get(key, default)
    return bool(v) if not isinstance(v, str) else v.strip().lower() not in ("false", "0", "no")


RETENTION_CHOICES = (180, 365, 730)   # 网络安全法：日志留存不少于六个月(180天)，故下限 180


def get_retention() -> int:
    """日志最长保存天数（VictoriaLogs retentionPeriod）。"""
    c = _load()
    try:
        v = int(c.get("retention_days", 180))
    except (TypeError, ValueError):
        return 180
    return v if v >= 180 else 180


def save_retention(days: int) -> None:
    c = _load()
    c["retention_days"] = int(days)
    _CFG_PATH.write_text(
        yaml.safe_dump(c, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def get_sso_config() -> dict:
    """Live SSO config (re-read each call so UI edits take effect without restart)."""
    c = _load()
    return {
        "yz_login_url": c.get("yz_login_url", "").rstrip("/"),
        "yz_app_id": str(c.get("yz_app_id", "")),
        "callback_url": c.get("callback_url", ""),
    }


def save_sso_config(yz_login_url: str, yz_app_id: str) -> None:
    c = _load()
    c["yz_login_url"] = (yz_login_url or "").rstrip("/")
    c["yz_app_id"] = str(yz_app_id or "")
    _CFG_PATH.write_text(
        yaml.safe_dump(c, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
