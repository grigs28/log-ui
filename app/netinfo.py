"""主机地址表：`192.168.0.x` / `10.10.10.x` / BMC 带外地址。

数据来源是 `hw.bmc.map.sh` 上报的 `app_name:bmc-map`（.91 上跑，只发地址不采传感器）：

    [盘点] 2026-11-07 11:29:46  v5-ctr02  : bmc_ip=10.10.10.36 bmc_check=ok ip10=10.10.10.102 ip0=192.168.0.92
    [汇总] 2026-11-07 11:29:46  bmc-map   : 20 台主机，有 BMC 19，无 BMC 1

字段：
    bmc_ip     带外地址（没有带外是 none）
    bmc_check  表中地址 vs 带内实测：ok / mismatch / unknown
    ip10       主机的 10.10.10.x（带外网段上的主机口）
    ip0        主机的 192.168.0.x（管理/业务）

**为什么要单独一个模块**：整机页和网卡页都要在主机名下面显示这几个地址，
两边各查一次 VL 纯属浪费，而且缓存口径会不一致。
"""
import re
import time

from . import vl

_CACHE = {"ts": 0.0, "data": {}}
_TTL = 120

_TAG_RE = re.compile(r'^\[盘点\]\s*(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+)?(.*)$')


def _kv(text):
    out = {}
    for tok in (text or "").split():
        k, _, v = tok.partition("=")
        if k and v:
            out[k.strip()] = v.strip()
    return out


def map_by_host():
    """{主机名: {'bmc_ip', 'bmc_check', 'ip10', 'ip0'}}，取每个主机最新一条。"""
    now = time.time()
    if _CACHE["data"] and now - _CACHE["ts"] < _TTL:
        return _CACHE["data"]

    rows = vl.query_logs(expr="app_name:bmc-map", limit=500)
    out = {}
    for rec in rows:
        m = _TAG_RE.match((rec.get("_msg") or "").strip())
        if not m:
            continue
        body = m.group(1)
        if " : " not in body:
            continue
        host, _, rest = body.partition(" : ")
        host = host.strip().split("(")[0].replace("\r", "")
        # 有的行主机名后缀带 `#015` —— 那是 ssh 回来的 \r(0x0D) 被打印成了字面量，
        # 源头是脚本少了一个 `tr -d "\r"`（已反馈），这里先擦掉，否则同一台机器会算成两台
        host = re.sub(r'#0*15$', '', host).strip()
        if not host or host == "bmc-map":
            continue
        kv = _kv(rest)
        if not kv:
            continue
        # 查询默认倒序，第一次见到的就是最新的
        if host not in out:
            out[host] = {
                "bmc_ip": (kv.get("bmc_ip") or "").replace("none", "") or "",
                "bmc_check": (kv.get("bmc_check") or "").lower(),
                "ip10": kv.get("ip10") or "",
                "ip0": kv.get("ip0") or "",
            }
    if out:
        _CACHE["data"], _CACHE["ts"] = out, now
    return _CACHE["data"]
