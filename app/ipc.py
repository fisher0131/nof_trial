from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from multiprocessing.connection import Client, Listener
from typing import Any

GMT8 = timezone(timedelta(hours=8))
DEFAULT_IPC_PORT = 6000
DEFAULT_IPC_ADDRESS = ("localhost", DEFAULT_IPC_PORT)
IPC_AUTHKEY = b"nof_ipc_v1"


class DaemonSharedState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.pid = os.getpid()
        self.enabled = False
        self.interval_sec = 300
        self.session_id = ""
        self.state = "idle"
        self.started_at = ""
        self.heartbeat_at = ""
        self.next_run_at = ""
        self.last_cycle_at = ""
        self.last_error = ""
        self.last_snapshot: dict[str, object] | None = None
        self.last_record: dict[str, object] | None = None
        self._pending_command: dict[str, object] | None = None

    def update(self, **kwargs: object) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "pid": self.pid,
                "enabled": self.enabled,
                "interval_sec": self.interval_sec,
                "session_id": self.session_id,
                "state": self.state,
                "started_at": self.started_at,
                "heartbeat_at": self.heartbeat_at,
                "next_run_at": self.next_run_at,
                "last_cycle_at": self.last_cycle_at,
                "last_error": self.last_error,
                "last_snapshot": self.last_snapshot,
                "last_record": self.last_record,
            }

    def set_command(self, cmd: dict[str, object] | None) -> None:
        with self._lock:
            self._pending_command = cmd

    def get_and_clear_command(self) -> dict[str, object] | None:
        with self._lock:
            cmd = self._pending_command
            self._pending_command = None
            return cmd


class IpcServer:
    def __init__(
        self,
        address: tuple[str, int] = DEFAULT_IPC_ADDRESS,
        state: DaemonSharedState | None = None,
    ) -> None:
        self.address = address
        self.state = state or DaemonSharedState()
        self.listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self.listener is not None:
            try:
                self.listener.close()
            except Exception:
                pass

    def _serve(self) -> None:
        while self._running:
            try:
                self.listener = Listener(self.address, authkey=IPC_AUTHKEY)
                break
            except OSError:
                import time
                time.sleep(0.5)

        while self._running:
            try:
                conn = self.listener.accept()
                self._handle(conn)
            except Exception:
                if not self._running:
                    break
                continue

    def _handle(self, conn: Any) -> None:
        try:
            msg: dict[str, object] = conn.recv()
        except Exception:
            return

        try:
            action = str(msg.get("action", ""))
            resp = self._dispatch(action, msg)
            conn.send(resp)
        except Exception as exc:
            try:
                conn.send({"ok": False, "error": str(exc)})
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _dispatch(self, action: str, msg: dict[str, object]) -> dict[str, object]:
        if action == "ping":
            status = self.state.snapshot()
            return {
                "ok": True,
                "pid": status["pid"],
                "state": status["state"],
                "heartbeat_at": status.get("heartbeat_at", ""),
                "enabled": status.get("enabled", False),
            }

        if action == "status":
            status = self.state.snapshot()
            return {"ok": True, **status}

        if action == "heartbeat":
            self.state.update(heartbeat_at=datetime.now(GMT8).isoformat())
            return {"ok": True, **self.state.snapshot()}

        if action == "start":
            interval_sec = max(5, int(msg.get("interval_sec", 300) or 300))
            session_id = str(msg.get("session_id", "") or "")
            self.state.update(
                enabled=True,
                interval_sec=interval_sec,
                session_id=session_id,
                state="starting",
            )
            self.state.set_command({
                "action": "start",
                "interval_sec": interval_sec,
                "session_id": session_id,
            })
            return {"ok": True}

        if action == "stop":
            self.state.update(enabled=False, state="stopping")
            self.state.set_command({"action": "stop"})
            return {"ok": True}

        return {"ok": False, "error": f"unknown action: {action}"}


def ipc_connect(
    address: tuple[str, int] = DEFAULT_IPC_ADDRESS,
    timeout: float = 3.0,
) -> Any | None:
    try:
        client = Client(address, authkey=IPC_AUTHKEY)
        return client
    except Exception:
        return None


def ipc_request(
    command: dict[str, object],
    address: tuple[str, int] = DEFAULT_IPC_ADDRESS,
    timeout: float = 3.0,
) -> dict[str, object]:
    conn = Client(address, authkey=IPC_AUTHKEY)
    try:
        conn.send(command)
        if conn.poll(timeout):
            return conn.recv()
        return {"ok": False, "error": "timeout"}
    finally:
        conn.close()


def ipc_ping(
    address: tuple[str, int] = DEFAULT_IPC_ADDRESS,
    timeout: float = 2.0,
) -> bool:
    try:
        resp = ipc_request({"action": "ping"}, address=address, timeout=timeout)
        return bool(resp.get("ok", False))
    except Exception:
        return False


def ipc_is_alive(
    address: tuple[str, int] = DEFAULT_IPC_ADDRESS,
    heartbeat_max_age_sec: float = 60.0,
) -> bool:
    try:
        resp = ipc_request({"action": "ping"}, address=address, timeout=2.0)
        if not resp.get("ok"):
            return False
        heartbeat_str = str(resp.get("heartbeat_at", "") or "")
        if not heartbeat_str:
            return False
        dt = _parse_iso(heartbeat_str)
        if dt is None:
            return False
        age = (datetime.now(GMT8) - dt).total_seconds()
        return age <= heartbeat_max_age_sec
    except Exception:
        return False


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=GMT8)
        return parsed
    except ValueError:
        return None
