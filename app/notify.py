"""钉钉通知：走内网 DingTalkVPN 微服务（默认 http://192.168.0.18:5555）。

接口见 /opt/webapp/DingTalkVPN/docs/API使用说明.md：
  POST /api/v1/messages/send    单发   {phone, title, content, message_type}
  POST /api/v1/messages/batch   群发   {phones[], title, content, message_type}
  Header: Authorization: Bearer <api_key>

配置存在 config/settings.yaml 的 `notify` 段（settings 页可改，读时现取）。
"""
import re

import httpx

from .config import get_notify

DEFAULT_URL = "http://192.168.0.18:5555"
DEFAULT_KEY = "sk-dingtalk-api-2024-001"
TIMEOUT = 10

_PHONE_RE = re.compile(r'^1\d{10}$')      # 钉钉要 11 位手机号


def clean_phones(text) -> list:
    """把「每行一个 / 逗号 / 分号 / 空格分隔」的输入拆成合法手机号列表。

    去重、保序、剔掉不合法的 —— 与其把错的发给服务端，不如在这里挡掉。
    """
    if isinstance(text, (list, tuple)):
        parts = list(text)
    else:
        parts = re.split(r'[\s,;，；、]+', text or "")
    seen, out = set(), []
    for p in parts:
        p = (p or "").strip()
        if not p or p in seen:
            continue
        if _PHONE_RE.match(p):
            seen.add(p)
            out.append(p)
    return out


def send(title: str, content: str, phones=None, message_type: str = "markdown") -> dict:
    """发钉钉消息。返回 {'ok': bool, 'msg': str, 'sent': [...], 'raw': ...}。

    没启用、没配接收人时不报错，直接返回 ok=False + 原因 ——
    调用方（比如磁盘异常通知）不该因为通知没配就炸掉。
    """
    cfg = get_notify()
    if not cfg["enabled"]:
        return {"ok": False, "msg": "钉钉通知未启用", "sent": []}

    targets = clean_phones(phones) if phones else cfg["phones"]
    if not targets:
        return {"ok": False, "msg": "没有配置接收人（手机号）", "sent": []}

    url = (cfg["url"] or DEFAULT_URL).rstrip("/")
    key = cfg["api_key"] or DEFAULT_KEY
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"title": (title or "")[:100], "content": (content or "")[:2000],
            "message_type": message_type or "markdown"}

    try:
        if len(targets) == 1:
            r = httpx.post(f"{url}/api/v1/messages/send",
                           json={**body, "phone": targets[0]},
                           headers=headers, timeout=TIMEOUT)
        else:
            r = httpx.post(f"{url}/api/v1/messages/batch",
                           json={**body, "phones": targets},
                           headers=headers, timeout=TIMEOUT)
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "msg": f"连不上通知服务（{url}）：{type(e).__name__}",
                "sent": []}

    try:
        raw = r.json()
    except Exception:                                        # noqa: BLE001
        raw = {"raw": (r.text or "")[:300]}

    if r.status_code == 200 and raw.get("success"):
        return {"ok": True, "msg": raw.get("message") or "已提交", "sent": targets, "raw": raw}
    if r.status_code == 401:
        return {"ok": False, "msg": "API Key 无效（401）", "sent": [], "raw": raw}
    if r.status_code == 429:
        return {"ok": False, "msg": "触发频率限制（429），稍后再试", "sent": [], "raw": raw}
    return {"ok": False,
            "msg": raw.get("message") or raw.get("detail") or f"服务返回 {r.status_code}",
            "sent": [], "raw": raw}


def status() -> dict:
    """当前配置概览（给设置页显示，api_key 打码）。"""
    cfg = get_notify()
    key = cfg["api_key"] or ""
    return {**cfg,
            "api_key_masked": (key[:8] + "…" + key[-4:]) if len(key) > 14 else ("已配置" if key else "未配置"),
            "url": cfg["url"] or DEFAULT_URL}
