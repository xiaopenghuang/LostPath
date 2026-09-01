"""用户可控的路径规则。

规则只会收紧计划器，不会直接删除或修改文件。忽略一条路径后，它和下面的子目录
都会被标记为用户保留，重新扫描也不会再次进入执行候选。
"""
from __future__ import annotations

import json
import ntpath
import os
import tempfile
from datetime import datetime, timezone

from ..storage import paths as lp_paths


def _normalize(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(path).strip())).rstrip("\\")


def _within(path: str, ancestor: str) -> bool:
    a = _normalize(path)
    b = _normalize(ancestor)
    return bool(a and b) and (a == b or a.startswith(b + "\\"))


def _read() -> dict:
    try:
        with open(lp_paths.rules_config(), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {"ignored_paths": []}
    if not isinstance(raw, dict):
        return {"ignored_paths": []}
    entries = raw.get("ignored_paths")
    if not isinstance(entries, list):
        entries = []
    cleaned = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        key = _normalize(entry["path"])
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append({
            "path": entry["path"].strip(),
            "reason": str(entry.get("reason") or "").strip()[:300],
            "created_at": entry.get("created_at"),
        })
    return {"ignored_paths": cleaned}


def _write(payload: dict) -> None:
    target = lp_paths.rules_config()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(name, target)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def list_ignored() -> list[dict]:
    return list(_read()["ignored_paths"])


def ignored_rule(path: str) -> dict | None:
    for entry in list_ignored():
        if _within(path, entry["path"]):
            return entry
    return None


def add_ignored(path: str, reason: str | None = None) -> dict:
    raw = str(path or "").strip()
    drive, _ = ntpath.splitdrive(raw)
    if not raw or not drive or not ntpath.isabs(raw) or drive.startswith("\\\\"):
        raise ValueError("路径必须是本机绝对路径")
    payload = _read()
    key = _normalize(raw)
    entries = payload["ignored_paths"]
    for entry in entries:
        if _normalize(entry["path"]) == key:
            return entry
    entry = {
        "path": ntpath.normpath(raw),
        "reason": str(reason or "").strip()[:300],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    entries.append(entry)
    _write(payload)
    return entry


def remove_ignored(path: str) -> bool:
    key = _normalize(path)
    payload = _read()
    before = len(payload["ignored_paths"])
    payload["ignored_paths"] = [
        x for x in payload["ignored_paths"] if _normalize(x["path"]) != key
    ]
    if len(payload["ignored_paths"]) == before:
        return False
    _write(payload)
    return True
