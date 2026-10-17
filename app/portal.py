"""网管中心各服务的健康探测 —— 给全局顶栏的健康灯用。

顶栏本身是从网管中心（.6）原样搬过来的，那条 `nm-topbar.js` 会 `fetch('/portal/status')`
取状态。它原来在 .6 的 810x 端口上能用，是因为 nginx 把该路径同源代理到网关；
**log-ui 跑在 .14，跨域拿不到**（实测 .6/portal/status 返回 302 需登录，且无 CORS），
所以由 log-ui 后端自己探一遍，用同一个路径 `/portal/status` 吐出去 —— 前端脚本不用改。

判据与网关一致：**有响应即算活着**（302 跳登录页也算正常，说明服务在跑）。
"""
import concurrent.futures as cf
import time

import httpx

GATEWAY = "http://192.168.0.6"
# 与 nm-topbar.js 的 SYS 一一对应（logui 是本机，不探）
SERVICES = {
    "netbox": 8100,
    "librenms": 8101,
    "zabbix": 8102,
    "portgrid": 8103,
    "netdisco": 8104,
    "semaphore": 8105,
}
TIMEOUT = 1.2
CACHE_TTL = 60          # 与顶栏的刷新周期一致

_cache = {"ts": 0.0, "data": None}


def _one(item):
    key, port = item
    try:
        r = httpx.get(f"{GATEWAY}:{port}/", timeout=TIMEOUT, follow_redirects=False)
        return key, r.status_code < 500          # 302 = 服务在跑，算正常
    except Exception:
        return key, False


def status() -> dict:
    """{'netbox': True, 'librenms': False, ..., 'logui': True}。60 秒缓存。"""
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    out = {"logui": True}        # 本机：能渲染这一页就说明它活着
    # 并行探，避免某台挂了把整条请求拖到 6×超时
    with cf.ThreadPoolExecutor(max_workers=len(SERVICES)) as ex:
        for k, ok in ex.map(_one, SERVICES.items()):
            out[k] = ok
    _cache["data"], _cache["ts"] = out, now
    return out
