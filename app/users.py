"""本地用户权限与登录审计。

yz-login 的 `is_admin` 是身份来源，本地在此之上再判一次：

| 本地权限 | yz is_admin | 结果 |
|---|---|---|
| `auto`    默认(随yz) | 1 | 放行，管理员 |
| `auto`    默认(随yz) | 0 | 拒绝：「yz-login 非管理员」 |
| `admin`   管理员     | 任意 | 放行，管理员（可把 yz 普通用户提升进来） |
| `user`    用户(禁登) | 任意 | 拒绝 |
| `blocked` 封禁       | 任意 | 拒绝 |

兜底管理员 `grigs` **始终**是管理员：不落盘、不可改、不受 yz 身份影响。

数据结构与网管中心网关（.6 的 `/data/users.json`）保持一致，便于对照：

    {"users": {"<username>": {"role", "display", "first", "last",
                              "attempts", "last_result", "last_reason", "yz_admin"}},
     "login_log": [{"ts", "u", "d", "ip", "yz_admin", "result", "reason"}]}

登录日志保留最近 1000 条；`role_of()` 走 mtime 缓存，每个请求调一次也不贵。
"""
import json
import os
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PATH = BASE / "config" / "users.json"

SUPER_ADMIN = "grigs"
ROLES = ("auto", "admin", "user", "blocked")
ROLE_LABELS = {
    "auto": "默认(随yz)",
    "admin": "管理员",
    "user": "用户(禁登)",
    "blocked": "封禁",
}
MAX_LOG = 1000

_lock = threading.RLock()
_cache = {"mtime": None, "data": None}


def _blank() -> dict:
    return {"users": {}, "login_log": []}


def load() -> dict:
    """读 users.json（按 mtime 缓存）。文件不存在时返回空结构，不建文件。"""
    with _lock:
        try:
            mtime = PATH.stat().st_mtime
        except OSError:
            return _blank()
        if _cache["data"] is not None and _cache["mtime"] == mtime:
            return _cache["data"]
        try:
            d = json.loads(PATH.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        d.setdefault("users", {})
        d.setdefault("login_log", [])
        if not isinstance(d["users"], dict):
            d["users"] = {}
        if not isinstance(d["login_log"], list):
            d["login_log"] = []
        _cache["data"], _cache["mtime"] = d, mtime
        return d


def _save(d: dict) -> bool:
    """原子写：先写 .tmp 再 os.replace，避免读到一个写了一半的文件。"""
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PATH)
        _cache["mtime"] = None          # 强制下次重读
        return True
    except Exception:
        return False


def role_of(username: str, yz_admin) -> tuple[bool, bool, str, str]:
    """返回 (放行, 本地管理员, 本地角色, 原因)。"""
    name = (username or "").strip()
    if not name:
        return False, False, "auto", "缺少用户名"
    if name == SUPER_ADMIN:
        return True, True, "admin", "superadmin"

    role = (load()["users"].get(name) or {}).get("role", "auto")
    if role not in ROLES:
        role = "auto"

    if role == "blocked":
        return False, False, role, "本地已封禁"
    if role == "user":
        return False, False, role, "本地权限为「用户」（禁止登录本系统）"
    if role == "admin":
        return True, True, role, "本地管理员"
    try:
        is_yz_admin = int(yz_admin or 0) == 1
    except (TypeError, ValueError):
        is_yz_admin = False
    if is_yz_admin:
        return True, True, role, "ok"
    return False, False, role, "yz-login 非管理员"


def log_attempt(username: str, display: str, ip: str, yz_admin,
                result: str, reason: str = "") -> None:
    """记一次登录尝试。成功与被拒绝都记（拒绝的正是要留档的部分）。"""
    name = (username or "").strip() or "?"
    with _lock:
        d = load()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        u = d["users"].get(name) or {"role": "auto", "first": now, "attempts": 0}
        u.update({
            "display": display or u.get("display") or name,
            "last": now,
            "attempts": int(u.get("attempts", 0)) + 1,
            "last_result": result,
            "last_reason": reason,
            "yz_admin": bool(yz_admin),
        })
        d["users"][name] = u
        d["login_log"].append({
            "ts": now, "u": name, "d": display or "", "ip": ip or "",
            "yz_admin": bool(yz_admin), "result": result, "reason": reason,
        })
        d["login_log"] = d["login_log"][-MAX_LOG:]
        _save(d)


def list_users() -> list[dict]:
    """用户列表（最近登录倒序，兜底管理员置顶）。"""
    d = load()
    out = []
    for name, u in d["users"].items():
        locked = name == SUPER_ADMIN
        out.append({
            "username": name,
            "display": u.get("display", "") or name,
            "role": "admin" if locked else (u.get("role") or "auto"),
            "attempts": int(u.get("attempts", 0)),
            "last": u.get("last", ""),
            "last_result": u.get("last_result", ""),
            "last_reason": u.get("last_reason", ""),
            "yz_admin": bool(u.get("yz_admin", False)),
            "first": u.get("first", ""),
            "locked": locked,
        })
    if SUPER_ADMIN not in d["users"]:
        # 还没登录过也要出现在表里 —— 它是兜底管理员
        out.append({
            "username": SUPER_ADMIN, "display": SUPER_ADMIN, "role": "admin",
            "attempts": 0, "last": "", "last_result": "", "last_reason": "",
            "yz_admin": False, "first": "", "locked": True,
        })
    out.sort(key=lambda x: x.get("last", ""), reverse=True)
    out.sort(key=lambda x: not x["locked"])     # 稳定排序：兜底管理员置顶
    return out


def save_role(username: str, role: str) -> tuple[bool, str]:
    """改本地权限。返回 (ok, msg)。"""
    name = (username or "").strip()
    if not name or role not in ROLES:
        return False, "参数错误"
    if name == SUPER_ADMIN:
        return False, f"兜底管理员 {SUPER_ADMIN} 始终为管理员，不可修改"
    with _lock:
        d = load()
        u = d["users"].setdefault(name, {
            "display": name,
            "first": time.strftime("%Y-%m-%d %H:%M:%S"),
            "attempts": 0,
        })
        u["role"] = role
        if not _save(d):
            return False, "保存失败"
    return True, ""


def delete_user(username: str) -> tuple[bool, str]:
    """删除一条本地用户记录（登录日志保留 —— 那是审计，不随记录删）。

    删掉后该用户回到默认档：yz 管理员照常能进，其他人仍被拒。
    兜底管理员不可删除。
    """
    name = (username or "").strip()
    if not name:
        return False, "参数错误"
    if name == SUPER_ADMIN:
        return False, f"兜底管理员 {SUPER_ADMIN} 不可删除"
    with _lock:
        d = load()
        if name not in d["users"]:
            return False, "用户不存在"
        d["users"].pop(name, None)
        if not _save(d):
            return False, "保存失败"
    return True, ""


def snapshot() -> dict:
    """给前端的一次性快照（结构对齐网管中心 users_ui.js）。"""
    d = load()
    return {
        "ok": True,
        "users": list_users(),
        "log": list(reversed(d["login_log"][-100:])),
        "super_admin": SUPER_ADMIN,
        "roles": ROLE_LABELS,
    }
