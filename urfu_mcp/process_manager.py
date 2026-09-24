"""Manage the foreground stdio MCP process without external services."""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from urfu_mcp.config import ConfigError

_STATE = Path.home() / ".urfu-mcp" / "processes.json"


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_state() -> dict[str, int]:
    try:
        raw = json.loads(_STATE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        result = {}
        for name, item in raw.items():
            if name != "mcp" or not isinstance(item, dict):
                continue
            pid = item.get("pid")
            created_at = item.get("created_at")
            if (item.get("started_by") == "urfu-mcp"
                and type(pid) is int and pid > 0
                and type(created_at) in (int, float)
                and _process_created_at(pid) == created_at):
                result[name] = pid
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def _process_created_at(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _alive(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _terminate(pid: int) -> None:
    """Best-effort portable graceful termination; never send unsupported SIGKILL on Windows."""
    if not _alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.1)
    if _alive(pid):
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _write_state(state: dict[str, int]) -> None:
    _STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(_STATE.parent, 0o700)
    records = {}
    for name, pid in state.items():
        created_at = _process_created_at(pid)
        if created_at is None:
            raise RuntimeError("Managed process exited before state could be recorded")
        records[name] = {"pid": pid, "created_at": created_at, "started_by": "urfu-mcp"}
    temporary = _STATE.with_name(f".{_STATE.name}.{secrets.token_hex(8)}.tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
            json.dump(records, stream)
        os.replace(temporary, _STATE)
    finally:
        temporary.unlink(missing_ok=True)


def _run_stdio_server() -> int:
    from urfu_mcp.runtime import serve
    return serve()


def start() -> int:
    try:
        current = _read_state()
        if any(_alive(pid) for pid in current.values()):
            print("urfu-mcp MCP server is already running.", file=sys.stderr)
            return 1
        _STATE.unlink(missing_ok=True)
        _write_state({"mcp": os.getpid()})
        # MCP stdio belongs to the client launching this command; do not detach it.
        return _run_stdio_server()
    except (ConfigError, OSError, RuntimeError, ValueError):
        print("Could not start urfu-mcp. Check config.yaml and authentication.", file=sys.stderr)
        return 1
    finally:
        _STATE.unlink(missing_ok=True)


def stop() -> int:
    state = _read_state()
    for name in ("mcp",):
        if name in state and state[name] != os.getpid():
            _terminate(state[name])
    _STATE.unlink(missing_ok=True)
    print("Stopped managed urfu-mcp processes.")
    return 0
