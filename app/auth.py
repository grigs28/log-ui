"""yz-login SSO (ticket callback, mode 1). SSO config read live via get_sso_config().

验票只解决「你是谁」；「能不能进」由本地 `users.role_of()` 决定 ——
默认档（auto）与网管中心一致：只有 yz 管理员能进，普通用户被拒并留档。
拒绝后**不能回跳 /auth/login**：yz-login 的会话还在，会立刻带新 ticket 回来，
形成死循环。所以统一跳到 /auth/denied 说明原因。
"""
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from .config import get_sso_config
from . import users

router = APIRouter()


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


def handle_ticket(request: Request, ticket):
    if not ticket:
        return RedirectResponse("/auth/login")
    sso = get_sso_config()
    try:
        r = httpx.get(
            f'{sso["yz_login_url"]}/api/ticket/verify',
            params={"ticket": ticket},
            timeout=10,
        )
        data = r.json()
    except Exception:
        return RedirectResponse("/auth/login")
    if r.status_code != 200 or not data.get("ok"):
        return RedirectResponse("/auth/login")

    username = data.get("username") or ""
    display = data.get("display_name") or ""
    yz_admin = data.get("is_admin", 0)

    allow, is_admin, role, reason = users.role_of(username, yz_admin)
    # 成功和被拒绝都要留档 —— 「包括拒绝的」正是这次要记录的重点
    users.log_attempt(username, display, _client_ip(request), yz_admin,
                      "ok" if allow else "denied", reason)
    if not allow:
        return RedirectResponse(
            "/auth/denied?reason=" + quote(reason) + "&u=" + quote(username),
            status_code=303,
        )

    request.session["user"] = {
        "id": data.get("id"),
        "username": username,
        "display_name": display,
        # 本地判定后的权限：所有 `if user.get("is_admin")` 门禁都跟着它走
        "is_admin": is_admin,
        # 保留 yz 原始身份，供每次请求复核时使用（本地权限可能随时被改）
        "yz_admin": bool(yz_admin),
        "local_role": role,
    }
    return RedirectResponse("/", status_code=302)


@router.get("/auth/login")
def login():
    sso = get_sso_config()
    return RedirectResponse(f'{sso["yz_login_url"]}/login?from=id:{sso["yz_app_id"]}', status_code=302)


@router.get("/auth/callback")
def callback(request: Request, ticket: str | None = None):
    return handle_ticket(request, ticket)


@router.get("/auth/logout")
def logout(request: Request):
    request.session.clear()
    sso = get_sso_config()
    return RedirectResponse(f'{sso["yz_login_url"]}/logout?from=id:{sso["yz_app_id"]}', status_code=302)
