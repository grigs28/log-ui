"""在 PVE 节点上执行单盘动作：完整检测 / LED 定位。

log-ui 以 root 跑在 .14，经 `/opt/grigs/w.sh ssh.run.sh <ip> -- <远端命令>`
下发命令 —— 这正是 `osd.poll.sh` 自己在用的通道（见其第 88 行示例）。

安全要点（本模块以 root 执行外部命令，输入全部来自网页）：
  · 设备名必须匹配 `^/dev/[a-zA-Z0-9]+$`
  · 物理槽位必须匹配 `^e\\d+/s\\d+$`
  · 目标 IP **只从 NAS hosts 文件按主机名解析**，绝不接受网页传来的地址
  · 命令行用 list 形式交给 subprocess，不经过本机 shell

LED 的两个坑（实测得出，改动前先看）：
  ① `-f` 只能在**第一个参数位**，写成 `osd.led.sh sdc -f` 会把 `-f` 当设备名
  ② 传纯数字超时会被当成 OSD 号（脚本用 `ceph osd ls` 消歧）——所以**不传超时**，
     改用 stdin：`{ sleep N; echo; sleep 3600; } | osd.led.sh ...`
     脚本的 `wait_for_input` 读到那个换行就当「用户主动停止」，时长精确可控
  ③ 健康盘走设备名形式**故意不点灯**（"全点亮等于没点"），必须给物理槽位 e252/s2
"""
import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import disks

GRIGS = "/opt/grigs"
W_SH = f"{GRIGS}/w.sh"
CHECK_SH = f"{GRIGS}/osd/osd.check.sh"
LED_SH = f"{GRIGS}/osd/osd.led.sh"

LED_SECONDS = 120          # 灯亮多久（网页触发，走 stdin 换行停）
CHECK_TIMEOUT = 900        # 完整检测最长等（含测速，坏盘可能挂 90s+）
JOB_TTL = 3600             # 任务结果保留多久
OUT_MAX = 200_000          # 输出截断上限

_DEV_RE = re.compile(r'^/dev/[a-zA-Z0-9]+$')
_SLOT_RE = re.compile(r'^e\d+/s\d+$')
_STATE_RE = re.compile(r'状态:\s*(\S.*)$', re.M)

JOBS: dict = {}
_LOCK = threading.Lock()


class Rejected(Exception):
    """输入校验不通过。"""


def resolve_ip(host: str) -> str:
    """主机名 → IP。只认 NAS hosts 文件里的记录，取不到就拒绝执行。"""
    host = (host or "").strip()
    for row in disks.fetch_hosts():
        if row.get("name") == host:
            return row.get("ip", "")
    return ""


def _slots_of(host: str, dev: str) -> list:
    """该设备占的物理槽位（MegaRAID 是虚拟盘，点灯必须按槽位点）。"""
    ent = (disks.poll_logs()["by_host"].get(host) or {}).get("disks", {}).get(dev) or {}
    return [f"e{s}" for s in (ent.get("pslots") or [])]


def build_remote(action: str, host: str, dev: str, seconds: int = LED_SECONDS) -> str:
    """构造在节点上执行的命令。所有插值都先过白名单校验。"""
    if not _DEV_RE.match(dev or ""):
        raise Rejected(f"设备名不合法: {dev!r}")

    if action == "check":
        return f"{CHECK_SH} {dev}"

    if action in ("led-fail", "led-locate"):
        flag = "-f " if action == "led-fail" else ""
        slots = [s for s in _slots_of(host, dev) if _SLOT_RE.match(s)]
        targets = " ".join(slots) if slots else dev
        n = max(10, min(int(seconds), 1800))
        # 不传超时参数（会与 OSD 号混淆）；用 stdin 在 N 秒后送一个换行，
        # 脚本的 wait_for_input 收到即「用户主动停止」，时长精确
        return f"{{ sleep {n}; echo; sleep 3600; }} | {LED_SH} {flag}{targets}"

    raise Rejected(f"未知动作: {action!r}")


def _run(job_id: str, action: str, host: str, dev: str, seconds: int) -> None:
    job = JOBS[job_id]
    try:
        ip = resolve_ip(host)
        if not ip:
            raise Rejected(f"{host} 不在 NAS hosts 清单里，无法定位节点")
        remote = build_remote(action, host, dev, seconds)
        job["cmd"] = remote

        timeout = CHECK_TIMEOUT if action == "check" else max(60, seconds + 90)
        p = subprocess.run([W_SH, "ssh.run.sh", ip, "--", remote],
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        with _LOCK:
            job["rc"] = p.returncode
            job["output"] = out[:OUT_MAX]
            if action == "check":
                job["verdict"] = _parse_verdict(out)
                _save_manual_check(host, dev, job["verdict"], out)
    except Rejected as e:
        with _LOCK:
            job["rc"] = -1
            job["output"] = f"✗ {e}"
    except subprocess.TimeoutExpired:
        with _LOCK:
            job["rc"] = -2
            job["output"] = f"✗ 超时（>{timeout}s）"
    except Exception as e:                                   # noqa: BLE001
        with _LOCK:
            job["rc"] = -3
            job["output"] = f"✗ {type(e).__name__}: {e}"
    finally:
        with _LOCK:
            job["state"] = "done"
            job["ended"] = time.time()


def start(action: str, host: str, dev: str, seconds: int = LED_SECONDS) -> dict:
    """起一个后台任务，立即返回任务信息（检测要几十秒，不能同步等）。"""
    _gc()
    host = (host or "").strip()
    if not host:
        raise Rejected("缺少主机名")
    if action not in ("check", "led-fail", "led-locate"):
        raise Rejected(f"未知动作: {action!r}")
    build_remote(action, host, dev, seconds)      # 先校验，非法就当场拒绝

    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        JOBS[job_id] = {"id": job_id, "action": action, "host": host, "dev": dev,
                        "state": "running", "started": time.time(), "ended": None,
                        "rc": None, "output": "", "verdict": None, "cmd": "",
                        "seconds": seconds}
    threading.Thread(target=_run, args=(job_id, action, host, dev, seconds),
                     daemon=True).start()
    return JOBS[job_id]


def get(job_id: str) -> dict:
    return JOBS.get(job_id) or {}


def _gc() -> None:
    now = time.time()
    with _LOCK:
        for k in [k for k, v in JOBS.items()
                  if v.get("ended") and now - v["ended"] > JOB_TTL]:
            JOBS.pop(k, None)


def _parse_verdict(out: str) -> dict:
    """从检测输出里抠出结论行。

    osd.check.sh 的总结段无条件输出「状态: ✓ 健康 / ✗ 异常 / ⚠ …」，
    poll.sh 用的也是这个锚点，这里保持一致。
    """
    m = _STATE_RE.search(out or "")
    status = m.group(1).strip() if m else ""
    if not status:
        return {}
    issue = ""
    for line in (out or "").splitlines():
        t = re.sub(r'^\[[^\]]*\]\s*', '', line).strip()
        if t.startswith("✗"):
            issue = t
            break
    return {"status": status, "issue": issue}


# ---------------- 手工检测结果（面板据此更新状态） ----------------

CHECKS = Path(__file__).resolve().parent.parent / "config" / "manual_checks.json"


def _load_checks() -> dict:
    try:
        return json.loads(CHECKS.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_manual_check(host: str, dev: str, verdict: dict, out: str) -> None:
    if not verdict:
        return
    d = _load_checks()
    d.setdefault(host, {})[dev] = {
        "ts": time.time(), "status": verdict.get("status", ""),
        "issue": verdict.get("issue", ""), "tail": (out or "")[-4000:],
    }
    try:
        CHECKS.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def manual_checks() -> dict:
    return _load_checks()
