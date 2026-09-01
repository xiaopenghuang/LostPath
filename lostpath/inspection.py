"""自动巡检偏好与轻量后台调度。

巡检只负责按用户设定触发已有的只读扫描管道，不新增写盘动作，也不绕过扫描单例锁。
配置损坏时退回关闭状态，避免启动时因为一份偏好文件阻塞主服务。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .storage import paths, snapshots

DEFAULT_INTERVAL_HOURS = 24
ALLOWED_INTERVAL_HOURS = (6, 12, 24, 72)


def _default() -> dict:
    return {"enabled": False, "interval_hours": DEFAULT_INTERVAL_HOURS}


def load_config() -> dict:
    p = paths.inspection_config()
    if not p.is_file():
        return _default()
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return _default()
    if not isinstance(raw, dict):
        return _default()
    enabled = raw.get("enabled") is True
    interval = raw.get("interval_hours", DEFAULT_INTERVAL_HOURS)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        interval = DEFAULT_INTERVAL_HOURS
    interval = int(interval)
    if interval not in ALLOWED_INTERVAL_HOURS:
        interval = DEFAULT_INTERVAL_HOURS
    return {"enabled": enabled, "interval_hours": interval}


def save_config(enabled: bool, interval_hours: int) -> dict:
    if interval_hours not in ALLOWED_INTERVAL_HOURS:
        raise ValueError("巡检间隔只能是 6、12、24 或 72 小时")
    value = {"enabled": bool(enabled), "interval_hours": int(interval_hours)}
    target = paths.inspection_config()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=1)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return value


def status() -> dict:
    cfg = load_config()
    _items, meta = snapshots.load_latest()
    scanned_at = meta.get("scanned_at") if meta.get("present") else None
    due = False
    if cfg["enabled"]:
        if not scanned_at:
            due = True
        else:
            try:
                stamp = datetime.fromisoformat(scanned_at.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - stamp).total_seconds()
                due = age >= cfg["interval_hours"] * 3600
            except (TypeError, ValueError):
                due = True
    return {
        **cfg,
        "last_scanned_at": scanned_at,
        "due": due,
    }


class Scheduler:
    """每分钟检查一次配置，触发已有扫描任务。"""

    def __init__(self, callback: Callable[[], None]):
        self._callback = callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_trigger = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="lostpath-inspection", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(60):
            cfg = status()
            if not cfg["enabled"] or not cfg["due"]:
                continue
            now = time.time()
            # 失败或取消时不要每分钟重复拉起，给磁盘和用户留出喘息时间。
            if now - self._last_trigger < 600:
                continue
            self._last_trigger = now
            try:
                self._callback()
            except Exception:
                # 调度器不能影响主服务，扫描自身会把失败写进 scan.log。
                continue


__all__ = [
    "ALLOWED_INTERVAL_HOURS",
    "DEFAULT_INTERVAL_HOURS",
    "Scheduler",
    "load_config",
    "save_config",
    "status",
]
