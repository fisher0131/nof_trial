from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.ipc import DEFAULT_IPC_ADDRESS, ipc_is_alive, ipc_request
from app.utils.io import load_json_file

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
DAEMON_STATUS_FILE = _ROOT / "daemon_status.json"
DEFAULT_DAEMON_INTERVAL_SEC = 300
GMT8 = timezone(timedelta(hours=8))


def _ipc_status_to_dict(ipc_resp: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(ipc_resp, dict) or not ipc_resp.get("ok"):
        return None
    return {k: v for k, v in ipc_resp.items() if k not in ("ok",)}


def load_daemon_status() -> dict[str, object]:
    default = {
        "pid": 0,
        "state": "offline",
        "enabled": False,
        "interval_sec": DEFAULT_DAEMON_INTERVAL_SEC,
        "session_id": "",
        "started_at": "",
        "heartbeat_at": "",
        "next_run_at": "",
        "last_cycle_at": "",
        "last_error": "",
        "last_snapshot": None,
        "last_record": None,
    }
    try:
        resp = ipc_request({"action": "status"}, address=DEFAULT_IPC_ADDRESS, timeout=1.0)
        if resp.get("ok"):
            merged = dict(default)
            merged.update({k: v for k, v in resp.items() if k != "ok"})
            return merged
    except Exception:
        pass
    return load_json_file(DAEMON_STATUS_FILE, default)


def parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=GMT8)
    return parsed


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        if os.name != "nt":
            return False
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            return False
        output = (result.stdout or "").strip().lower()
        return bool(output) and "no tasks are running" not in output and str(pid) in output


def is_daemon_alive(status: dict[str, object] | None = None) -> bool:
    current_status = status or load_daemon_status()
    pid = int(current_status.get("pid", 0) or 0)
    heartbeat = parse_iso_datetime(str(current_status.get("heartbeat_at", "") or ""))
    interval_sec = int(current_status.get("interval_sec", DEFAULT_DAEMON_INTERVAL_SEC) or DEFAULT_DAEMON_INTERVAL_SEC)
    if heartbeat is None:
        return False
    age = (datetime.now(GMT8) - heartbeat).total_seconds()
    allowed_age = max(15, interval_sec * 2 + 30)
    return is_pid_alive(pid) and age <= allowed_age


def start_daemon_process(wait_for_heartbeat_sec: float = 8.0) -> tuple[bool, str]:
    status = load_daemon_status()
    if is_daemon_alive(status):
        return True, "daemon 已在运行"

    command = [sys.executable, "-m", "app.daemon"]
    try:
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            subprocess.Popen(
                command,
                cwd=str(_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
        else:
            subprocess.Popen(
                command,
                cwd=str(_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception as exc:
        return False, f"启动 daemon 失败: {exc}"

    if wait_for_heartbeat_sec <= 0:
        return True, "daemon 进程已启动"

    deadline = time.monotonic() + wait_for_heartbeat_sec
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if ipc_is_alive(address=DEFAULT_IPC_ADDRESS):
            return True, "daemon 进程已启动"

    latest_status = load_daemon_status()
    pid = int(latest_status.get("pid", 0) or 0)
    if is_pid_alive(pid):
        return True, "daemon 进程已启动，IPC 未就绪"

    return False, "daemon 启动后未写入有效心跳，请检查 Python 环境和依赖"
