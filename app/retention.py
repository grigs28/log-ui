"""把「日志最长保存时间」应用到生产 VictoriaLogs。

VL 的 -retentionPeriod 是启动参数：改 /opt/logging/docker-compose.yml 的
command 行 → docker compose config 校验 → up -d victorialogs 重建容器
（数据在宿主 bind-mount，重建不丢）→ 健康检查；失败回滚备份并重建。

注意：缩短保留期会让 VL 在后台清理超期旧数据（不可恢复）；延长则只影响
后续清理。UI 只提供 >=180 天选项（网络安全法下限），本模块再兜一道。
"""
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx

COMPOSE = Path("/opt/logging/docker-compose.yml")
VICTORIALOGS_URL = "http://localhost:9428"
_RET_PAT = re.compile(r"-retentionPeriod=\d+d")


def _sh(cmd: list, cwd=None, timeout=180) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def _vl_alive() -> bool:
    try:
        return httpx.get(f"{VICTORIALOGS_URL}/health", timeout=10).status_code == 200
    except Exception:
        return False


def current_retention_days() -> "int | None":
    """从 compose 文件读当前 retentionPeriod；读不到返回 None。"""
    try:
        m = _RET_PAT.search(COMPOSE.read_text(encoding="utf-8"))
        return int(m.group(0).split("=")[1][:-1]) if m else None
    except Exception:
        return None


def apply(days: int) -> tuple[bool, str]:
    days = int(days)
    if days < 180:
        return False, "不能低于 180 天（《网络安全法》要求日志留存不少于六个月）"

    if not COMPOSE.exists():
        return False, f"compose 文件不存在（{COMPOSE}）"
    cur = current_retention_days()
    if cur == days:
        return True, "no-change"

    backup = COMPOSE.with_suffix(".yml.bak-retention")
    try:
        text = COMPOSE.read_text(encoding="utf-8")
        new = _RET_PAT.sub(f"-retentionPeriod={days}d", text, count=1)
        if new == text:
            return False, "未找到 -retentionPeriod 配置行"

        shutil.copy2(COMPOSE, backup)
        COMPOSE.write_text(new, encoding="utf-8")

        ok, out = _sh(["docker", "compose", "config", "-q"], cwd=str(COMPOSE.parent))
        if not ok:
            raise RuntimeError(f"docker compose config 校验失败: {out[-200:]}")

        ok, out = _sh(["docker", "compose", "up", "-d", "victorialogs"],
                      cwd=str(COMPOSE.parent))
        if not ok:
            raise RuntimeError(f"重建 victorialogs 失败: {out[-200:]}")

        # 健康检查：最多等 30 秒
        for _ in range(10):
            if _vl_alive():
                return True, f"ok（{cur}天 -> {days}天，容器已重建）"
            time.sleep(3)
        raise RuntimeError("重建后 VictoriaLogs /health 30 秒内未恢复")

    except Exception as e:
        try:
            if backup.exists():
                shutil.copy2(backup, COMPOSE)
                _sh(["docker", "compose", "up", "-d", "victorialogs"],
                    cwd=str(COMPOSE.parent))
        except Exception:
            pass
        return False, f"应用失败已回滚: {e}"
