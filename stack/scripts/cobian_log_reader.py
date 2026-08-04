#!/usr/bin/env python3
"""Cobian Backup log converter - directly sends structured JSON to VictoriaLogs."""

import sys
import os
import glob
import json
import re
import urllib.request

LOG_DIR = "/mnt/cobian-logs"
STATE_FILE = "/var/lib/cobian-log-state.json"
VICTORIALOGS_URL = "http://localhost:9428/insert/jsonline"
BATCH_SIZE = 500

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"last_files": {}}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def parse_line(line):
    """Parse a Cobian log line and extract structured fields."""
    level = "info"
    if line.startswith("ERR"):
        level = "error"
        line = line[3:].strip()
    elif line.startswith("WRN") or line.startswith("WAR"):
        level = "warning"
        line = line[3:].strip()
    else:
        line = line.strip()
    
    # Cobian 用 ERR 前缀标记一些非备份错误的更新相关通知（新版本提示/检查更新失败），归为 info
    if level == "error" and "新版本" in line:
        level = "info"

    source_host = ""
    task_name = ""
    file_path = ""
    error_type = ""
    
    # Extract source host from UNC paths: \\192.168.0.X\...
    m = re.search(r'\\\\([0-9.]+)\\', line)
    if m:
        source_host = m.group(1)
    
    # Also check for D:\ paths (local on 192.168.0.28)
    if not source_host and re.search(r'D:\\\\', line):
        source_host = "192.168.0.28"
    
    # Extract task name
    m = re.search(r'任务["""]([^"""]+)["""]', line)
    if m:
        task_name = m.group(1)
    
    # Extract file path
    m = re.search(r'文件["""]([^"""]+)["""]', line)
    if m:
        file_path = m.group(1)
    
    # Error type
    if level == "error":
        if "拒绝访问" in line:
            error_type = "access_denied"
        elif "文件名、目录名或卷标语法不正确" in line:
            error_type = "invalid_path"
        elif "请求被中止" in line:
            error_type = "connection_aborted"
        elif "Connection Closed" in line:
            error_type = "connection_closed"
        elif "找不到" in line or "not found" in line.lower():
            error_type = "not_found"
        elif "无法复制" in line:
            error_type = "copy_failed"
        elif "不存在或无法访问" in line:
            error_type = "source_unavailable"
        else:
            error_type = "other"
    
    # Extract timestamp from line: 2026-04-05 00:05 ...
    ts_match = re.match(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', line)
    timestamp = ts_match.group(1).replace(' ', 'T') + ':00+08:00' if ts_match else ""
    
    return {
        "_msg": line,
        "_time": timestamp,
        "app_name": "cobian-backup",
        "hostname": "192.168.0.28",  #  Cobian 引擎本机，不按源 IP 散开
        "level": level,
        "cobian_source": source_host or "192.168.0.28",
        "cobian_task": task_name,
        "cobian_error": error_type,
        "cobian_file": file_path[:200] if file_path else "",  # truncate long paths
        "log_source": source_host or "192.168.0.28"
    }

def send_batch(batch):
    """Send a batch of JSON lines to VictoriaLogs. Returns True on success, False on failure."""
    if not batch:
        return True
    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in batch) + "\n"
    try:
        req = urllib.request.Request(VICTORIALOGS_URL, data=payload.encode("utf-8"),
                                     method="POST")
        req.add_header("Content-Type", "application/x-ndjson")
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as e:
        print(f"# VL insert error: {e}", file=sys.stderr)
        return False


def read_new_lines(filepath, state):
    """Read new lines and send to VL in batches.

    Returns (committed_pos, mtime, ok):
      committed_pos - file byte offset up to which data was SUCCESSFULLY sent.
                      The cursor is only advanced to here, so un-sent data is
                      re-read on the next run (no silent loss on VL outage).
      ok            - True if the whole file was processed & sent; False if a
                      send failed (stopped early).
    """
    fname = os.path.basename(filepath)
    mtime = os.path.getmtime(filepath)
    prev_pos = state.get("last_files", {}).get(fname, 0)
    prev_mtime = state.get("last_files", {}).get(fname + "_mtime", 0)

    fsize = os.path.getsize(filepath)
    if mtime != prev_mtime or prev_pos > fsize:
        prev_pos = 0

    committed_pos = prev_pos  # furthest position confirmed sent to VL
    batch = []
    try:
        with open(filepath, encoding="utf-16", errors="replace") as f:
            f.seek(prev_pos)
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                record = parse_line(line)
                batch.append(record)
                if len(batch) >= BATCH_SIZE:
                    if send_batch(batch):
                        committed_pos = f.tell()
                        batch = []
                    else:
                        # VL send failed: discard this unsent batch (it will be
                        # re-read from committed_pos next run) and stop.
                        return committed_pos, mtime, False
            # flush remaining tail
            if batch:
                if send_batch(batch):
                    committed_pos = f.tell()
                else:
                    return committed_pos, mtime, False
    except Exception as e:
        print(f"# Error reading {filepath}: {e}", file=sys.stderr)
        return committed_pos, mtime, False

    return committed_pos, mtime, True


def main():
    state = load_state()
    files = sorted(glob.glob(os.path.join(LOG_DIR, "log *.txt")), reverse=True)
    for f in files:
        committed_pos, mtime, ok = read_new_lines(f, state)
        fname = os.path.basename(f)
        state["last_files"][fname] = committed_pos
        state["last_files"][fname + "_mtime"] = mtime
        if not ok:
            # VL unavailable: save progress so far and stop. Unsent data and
            # remaining files are retried on the next run without loss.
            print(f"# Stopped at {fname} (VL unavailable); will retry next run.",
                  file=sys.stderr)
            break
    save_state(state)


if __name__ == "__main__":
    main()
