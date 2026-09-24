"""把「主机采集策略」同步进生产 Vector（vector.toml 受管块）。

策略来源 config/servers.yaml：
  ignored 名单               → 全拒
  注册主机 min_level         → 只收该级别以上（level 已归一化为
                               error/warning/info/debug；空 = info 全收）

受管块由 BEGIN/END 标记包裹，log-ui 只改写标记之间的内容：
    # ==== BEGIN log-ui managed: ignore-list (auto, do not edit) ====
    [transforms.ui_ignore_filter]
    type = "filter"
    inputs = ["journald_filter", "taf_prep"]
    condition = '<单行 VRL：按 hostname 查策略表>'
    # ==== END log-ui managed: ignore-list ====

块不存在时自动安装（追加块 + 把 sinks.victorialogs.inputs 改为
["ui_ignore_filter"]；sink inputs 行与预期不符则拒绝写入，防手改冲突）。

护栏：写前备份 → vector validate 校验 → docker restart → 健康检查，
任一步失败自动回滚备份文件并再次重启，采集配置回到已知良好状态。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

VECTOR_TOML = Path("/opt/logging/vector.toml")
VECTOR_CTR = "vector"

BEGIN = "# ==== BEGIN log-ui managed: ignore-list (auto, do not edit) ===="
END = "# ==== END log-ui managed: ignore-list ===="

# 首次安装块时，sink inputs 必须是以下已知形态之一（否则提示人工处理）。
# 注意：生产的 "rclone_prep" 直连形态是历史手改——rclone_prep 同时也在
# level_normalize 的 inputs 里，直连会导致 rclone 恢复发日志时双路重复入库；
# 接线时统一理顺（rclone 只走归一化链）。
_KNOWN_SINK_INPUTS = [
    'inputs = ["journald_filter", "taf_prep"]',
    'inputs = ["journald_filter", "taf_prep", "rclone_prep"]',
]

_BLOCK_TMPL = (
    "{begin}\n"
    '[transforms.ui_ignore_filter]\n'
    'type = "filter"\n'
    'inputs = ["journald_filter", "taf_prep"]\n'
    "condition = '''{cond}'''\n"
    "{end}"
)


def _arr(items: list) -> str:
    return "[" + ", ".join(json.dumps(x) for x in items) + "]"


def _condition() -> str:
    """从 servers.yaml 生成单行 VRL 策略条件。忽略 > 级别 > 默认放行。

    includes() 是数组成员检查（contains 是字符串子串查找，传数组会 E110）。
    """
    from . import servers as srv
    ignored = srv.load_ignore()
    reg = srv.load_registry()
    warn_hosts = [s["name"] for s in reg if s.get("min_level") == "warning"
                  and s["name"] not in ignored]
    err_hosts = [s["name"] for s in reg if s.get("min_level") == "error"
                 and s["name"] not in ignored]

    if not ignored and not warn_hosts and not err_hosts:
        return "true"
    lvl = '(to_string(.level) ?? "info")'
    # VRL 实测约束：if-else 链必须整体在同一行（`}` 换行后独立 `else` 会 E203），
    # 赋值语句与 if 链之间用换行分隔
    branches = []
    if ignored:
        branches.append(f'if includes({_arr(ignored)}, h) {{ false }}')
    if warn_hosts:
        pre = "else " if branches else ""
        branches.append(f'{pre}if includes({_arr(warn_hosts)}, h) {{ {lvl} == "warning" || {lvl} == "error" }}')
    if err_hosts:
        pre = "else " if branches else ""
        branches.append(f'{pre}if includes({_arr(err_hosts)}, h) {{ {lvl} == "error" }}')
    chain = " ".join(branches) + " else { true }"
    return f'h = to_string(.hostname) ?? "-"\n{chain}'


def _install_block(text: str, cond: str) -> str:
    """无块时：改 sink inputs + 文件末尾追加块。结构不符抛异常。"""
    for expected in _KNOWN_SINK_INPUTS:
        if expected in text:
            text = text.replace(expected, 'inputs = ["ui_ignore_filter"]', 1)
            break
    else:
        raise RuntimeError(
            "sinks.victorialogs.inputs 与已知形态不符（可能被手改），refusing 自动接线；"
            "请人工确认后重试")
    block = _BLOCK_TMPL.format(begin=BEGIN, end=END, cond=cond)
    return text.rstrip("\n") + "\n\n\n" + block + "\n"


def _rewrite_condition(text: str, cond: str) -> str:
    block = _BLOCK_TMPL.format(begin=BEGIN, end=END, cond=cond)
    return re.sub(re.escape(BEGIN) + r".*?" + re.escape(END),
                  lambda m: block, text, count=1, flags=re.S)


def _validate() -> tuple[bool, str]:
    r = subprocess.run(
        ["docker", "exec", VECTOR_CTR, "vector", "validate", "--no-environment",
         "/etc/vector/vector.toml"],
        capture_output=True, text=True, timeout=60)
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0, out[-400:]


def _restart_vector() -> bool:
    # 不用 SIGHUP 热重载：Vector 0.54 在拓扑变更时 reload 有 fanout panic bug
    # （实测 topology_build_failed + 容器退出）。restart 中断仅数秒，可靠优先。
    r = subprocess.run(["docker", "restart", VECTOR_CTR],
                       capture_output=True, text=True, timeout=90)
    return r.returncode == 0


def _vector_healthy() -> tuple[bool, str]:
    """重启后确认：容器在跑 + 近 30s 无致命错误。"""
    r = subprocess.run(["docker", "inspect", VECTOR_CTR, "--format", "{{.State.Running}}"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or r.stdout.strip() != "true":
        return False, f"vector 容器未运行 ({r.stdout.strip()})"
    import time as _t
    _t.sleep(4)
    l = subprocess.run(["docker", "logs", "--since", "30s", VECTOR_CTR],
                       capture_output=True, text=True, timeout=30)
    bad = [ln for ln in (l.stdout + l.stderr).splitlines()
           if any(k in ln for k in ("topology_build_failed", "panicked", "ERROR"))]
    if bad:
        return False, "; ".join(bad[-2:])
    return True, ""


def sync() -> tuple[bool, str]:
    """按 servers.yaml 当前策略写受管块并重启 Vector（含 validate/健康/回滚护栏）。"""
    if not VECTOR_TOML.exists():
        return False, f"vector.toml 不存在（{VECTOR_TOML}），策略已存但未生效"

    backup = VECTOR_TOML.with_suffix(".toml.bak-logui")
    try:
        text = VECTOR_TOML.read_text(encoding="utf-8")
        cond = _condition()
        new = _rewrite_condition(text, cond) if BEGIN in text else _install_block(text, cond)
        if new == text:
            return True, "no-change"

        shutil.copy2(VECTOR_TOML, backup)
        VECTOR_TOML.write_text(new, encoding="utf-8")

        ok, detail = _validate()
        if not ok:
            raise RuntimeError(f"vector validate 失败: {detail}")
        if not _restart_vector():
            raise RuntimeError("docker restart vector 失败")
        h, hmsg = _vector_healthy()
        if not h:
            raise RuntimeError(f"重启后 Vector 不健康: {hmsg}")
        return True, "ok"

    except Exception as e:
        # 回滚：还原备份并重启，保证采集配置回到已知良好状态
        try:
            if backup.exists():
                shutil.copy2(backup, VECTOR_TOML)
                _restart_vector()
        except Exception:
            pass
        return False, f"同步失败已回滚: {e}"
