"""VictoriaLogs API client (read-only): logs + aggregations."""
import json
import time
import httpx
from .config import VL_URL


def query_logs(expr: str = "*", limit: int = 100, start=None, end=None):
    """LogsQL query -> list of log dicts (newest first).  Returns [] on error."""
    params = {"query": expr, "limit": str(limit)}
    if start:
        params["start"] = str(start)
    if end:
        params["end"] = str(end)
    try:
        r = httpx.get(f"{VL_URL}/select/logsql/query", params=params, timeout=30)
        out = []
        for line in r.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out
    except Exception:
        return []


def field_values(field: str, expr: str = "*", limit: int = 100):
    """Distinct values of a field (dropdowns / auto-discovery)."""
    try:
        r = httpx.get(
            f"{VL_URL}/select/logsql/field_values",
            params={"query": expr, "field": field, "limit": str(limit)},
            timeout=15,
        )
        return [v.get("value") for v in r.json().get("values", [])]
    except Exception:
        return []


def _range(days: int = 7):
    now = int(time.time())
    return now - days * 86400, now


def stats_by(field: str, days: int = 7, expr: str = "*"):
    """{field_value: count} via `... | stats by(field) count()`."""
    s, e = _range(days)
    try:
        r = httpx.get(
            f"{VL_URL}/select/logsql/stats_query",
            params={"query": f"{expr} | stats by({field}) count()", "start": s, "end": e},
            timeout=30,
        )
        out = {}
        for res in r.json().get("data", {}).get("result", []):
            v = res["metric"].get(field) or "(none)"
            out[v] = int(float(res["value"][1]))
        return out
    except Exception:
        return {}


def stats_total(expr: str = "*", days: int = 7):
    s, e = _range(days)
    try:
        r = httpx.get(
            f"{VL_URL}/select/logsql/stats_query",
            params={"query": f"{expr} | stats count()", "start": s, "end": e},
            timeout=30,
        )
        res = r.json().get("data", {}).get("result", [])
        return int(float(res[0]["value"][1])) if res else 0
    except Exception:
        return 0


def stats_range(expr: str = "*", days: int = 7, step: str = "1h"):
    """[(unix_ts, count), ...] time series via stats_query_range."""
    s, e = _range(days)
    try:
        r = httpx.get(
            f"{VL_URL}/select/logsql/stats_query_range",
            params={"query": f"{expr} | stats count()", "step": step, "start": s, "end": e},
            timeout=30,
        )
        series = []
        for res in r.json().get("data", {}).get("result", []):
            for ts, val in res.get("values", []):
                series.append((int(ts), int(float(val))))
        series.sort()
        return series
    except Exception:
        return []


def last_seen(host: str):
    """Most recent _time for a host (None if no logs)."""
    safe = host.replace('"', '\\"')  # escape double-quotes in LogsQL string
    logs = query_logs(f'hostname:"{safe}"', limit=1)
    return logs[0].get("_time") if logs else None
