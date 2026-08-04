"""yz-login SSO (ticket callback, mode 1). SSO config read live via get_sso_config()."""
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from .config import get_sso_config

router = APIRouter()


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
    request.session["user"] = {
        "id": data.get("id"),
        "username": data.get("username"),
        "display_name": data.get("display_name"),
        "is_admin": data.get("is_admin", 0),
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
