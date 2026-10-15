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
from .disks import _ANSI_RE

GRIGS = "/opt/grigs"
W_SH = f"{GRIGS}/w.sh"
# 远端一律经 w.sh 下发（w.sh 会先从 NAS 拉最新脚本再执行）：
#   /opt/grigs/w.sh ssh.run.sh <ip> -- "/opt/grigs/w.sh osd.check.sh <设备>"

LED_SECONDS = 120          # 灯亮多久（网页触发，走 stdin 换行停）
CHECK_TIMEOUT = 900        # 完整检测最长等（含测速，坏盘可能挂 90s+）
JOB_TTL = 3600             # 任务结果保留多久
OUT_MAX = 200_000          # 输出截断上限

_DEV_RE = re.compile(r'^/dev/[a-zA-Z0-9]+$')
_SLOT_RE = re.compile(r'^e\d+/s\d+$')
# 必须要求结论字符：脚本里还有一行「OSD 状态: osd.18 = osd.18」也含「状态:」，
# 裸锚点会匹配到它（osd.poll.sh 的规格文档里记过同一个坑）
_STATE_RE = re.compile(r'状态:\s*([✓✗⚠].*)$', re.M)

_HAS_VERDICT = re.compile(r'状态:\s*[✓✗⚠]')


def _run_wsh(ip: str, remote: str, timeout: int, expect_verdict: bool = False):
    """调用 w.sh 把命令下发到节点。带重试。

    两类瞬时故障都要重试：
      · w.sh 每次运行都会**强制自更新**（重下并替换自己），替换那一瞬间
        `/opt/grigs/w.sh` 不存在 —— 实测连续调用撞上过 FileNotFoundError
      · w.sh 自更新期间下发失败时，ssh.run.sh 会把**上一次**的日志内容打出来
        （日志目录按命令名复用），看起来像成功但其实是旧数据。
        所以检测类命令要求输出里必须出现「状态: ✓/✗/⚠」，没有就重试
    """
    delay, last = 1.0, None
    for attempt in range(3):
        try:
            p = subprocess.run([W_SH, "ssh.run.sh", ip, "--", remote],
                               capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            last = e
            p = None
        if p is not None:
            if not (expect_verdict and p.returncode != 0
                    and not _HAS_VERDICT.search(p.stdout or "")):
                return p
            last = p
        if attempt < 2:
            time.sleep(delay)
            delay *= 1.7
    if isinstance(last, subprocess.CompletedProcess):
        return last
    raise last if last is not None else RuntimeError("w.sh 调用失败")


JOBS: dict = {}
_LOCK = threading.Lock()
# ssh.run.sh 的日志目录是 /tmp/ssh.run/<命令名>，同一台主机并发跑会互相覆盖日志，
# 所以按主机串行；不同主机互不影响（黄灯是脱离式的，不占锁）
_HOST_LOCKS: dict = {}
_HOST_LOCKS_GUARD = threading.Lock()


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
        # 经 w.sh 下发：它会先从这个 NAS 拉最新脚本再执行，避免用到节点上过期的旧版
        # （实测节点上的 osd.check.sh 比 NAS 上小 262 字节）
        return f"{W_SH} osd.check.sh {dev}"

    if action == "led-off":
        # osd.led.sh 有 trap cleanup EXIT INT TERM HUP，SIGTERM 就能触发它自己的
        # 关灯逻辑（storcli ... stop locate）。按**目标字符串**精确匹配，
        # 免得把别的盘上的灯一起关掉；-f 与不带 -f 两种命令行分别匹配（互不包含）
        slots = [s for s in _slots_of(host, dev) if _SLOT_RE.match(s)]
        targets = " ".join(slots) if slots else dev
        esc = re.escape(targets)
        # 只按**目标**匹配，不依赖开关组合（-f / -a 的个数和顺序会变）。
        # 末尾加 ([^0-9]|$) 是为了不误伤相邻槽位：目标是 e252/s2 时，
        # 无边界会连 e252/s20 的进程一起杀掉。
        return (f"pkill -TERM -f 'osd\\.led\\.sh .*{esc}([^0-9]|$)' || true; "
                f"echo 已发送关灯")

    if action in ("led-fail", "led-locate"):
        flag = "-f " if action == "led-fail" else ""
        slots = [s for s in _slots_of(host, dev) if _SLOT_RE.match(s)]
        targets = " ".join(slots) if slots else dev
        n = max(10, min(int(seconds), 1800))
        # 不传超时参数（纯数字会与 OSD 号混淆）；用 stdin 在 N 秒后送一个换行，
        # 脚本的 wait_for_input 收到即「用户主动停止」，时长精确。
        # 外面套 nohup + & ：ssh 立即返回，灯自己在节点上闪 ——
        # 这样一次可以点好几块盘同时闪，不用等前一块亮完。
        # -a / --all-members：跳过健康闸门，VD 全成员点灯（正常盘也点）。
        # 不加这个，健康的 MegaRAID 虚拟盘会被脚本拒绝（"没有需要指的坏盘，不点灯"），
        # 只能退而用物理槽位形式；加了之后设备名形式也能点亮。
        inner = f"{{ sleep {n}; echo; sleep 3600; }} | {W_SH} osd.led.sh {flag}-a {targets}"
        return f"nohup bash -c '{inner}' >/tmp/osd.led.out 2>&1 & echo 已下发 pid=$!"

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
        with _HOST_LOCKS_GUARD:
            hlock = _HOST_LOCKS.setdefault(host, threading.Lock())
        with hlock:
            p = _run_wsh(ip, remote, timeout, expect_verdict=(action == "check"))
        out = _ANSI_RE.sub("", (p.stdout or "") + (("\n" + p.stderr) if p.stderr else ""))
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
    if action not in ("check", "led-fail", "led-locate", "led-off"):
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
