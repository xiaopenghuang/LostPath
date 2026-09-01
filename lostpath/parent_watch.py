"""Stop an engine spawned by the desktop shell when that shell disappears."""
from __future__ import annotations

import ctypes
import os
import threading
import time
from collections.abc import Callable, Mapping


PARENT_PID_ENV = "LOSTPATH_PARENT_PID"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def start_from_environment(
    env: Mapping[str, str] | None = None,
    *,
    interval: float = 1.0,
    is_alive: Callable[[int], bool] = process_alive,
    exit_process: Callable[[int], object] = os._exit,
) -> bool:
    """Start a daemon watchdog only when the desktop shell supplied its PID."""
    source = env if env is not None else os.environ
    raw = source.get(PARENT_PID_ENV)
    try:
        parent_pid = int(raw or "")
    except ValueError:
        return False
    if parent_pid <= 0 or parent_pid == os.getpid():
        return False

    def watch() -> None:
        while is_alive(parent_pid):
            time.sleep(interval)
        exit_process(0)

    threading.Thread(
        target=watch, name="lostpath-parent-watch", daemon=True,
    ).start()
    return True


__all__ = ["PARENT_PID_ENV", "process_alive", "start_from_environment"]
