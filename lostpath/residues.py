"""从当前快照找出可能属于已卸载软件或未登记应用的 C 盘残留。

这是只读的提示层。判定必须保守，只有应用类归因、当前台账里找不到对应登记，
且目录仍然存在并达到体积门槛时才展示。它不能仅凭一次扫描断言软件已卸载，
后续动作仍交给用户确认。
"""
from __future__ import annotations

import ntpath
import os
from collections import defaultdict

MIN_RESIDUE_BYTES = 50 * 1024 * 1024
RESIDUE_KINDS = {"app", "app_unregistered"}


def _token(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def detect(data: dict, min_size: int = MIN_RESIDUE_BYTES, check_exists: bool = True) -> dict:
    entities = data.get("software") or []
    installed = {
        _token(e.get("name"))
        for e in entities
        if isinstance(e, dict) and e.get("source") != "trace"
    }
    groups: dict[str, dict] = defaultdict(lambda: {
        "owner": "", "size": 0, "entries": 0, "paths": [], "kind": "app",
    })
    scanned_at = (data.get("snapshot") or {}).get("scanned_at")
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner")
        kind = item.get("owner_kind")
        path = item.get("path")
        if not owner or kind not in RESIDUE_KINDS or not isinstance(path, str):
            continue
        if _token(owner) in installed:
            continue
        try:
            if check_exists and not os.path.isdir(path):
                continue
        except OSError:
            continue
        size = max(0, int(item.get("size") or 0))
        key = _token(owner) or ntpath.normcase(owner)
        group = groups[key]
        group["owner"] = owner
        group["size"] += size
        group["entries"] += 1
        group["kind"] = kind
        if len(group["paths"]) < 8:
            group["paths"].append(path)

    candidates = []
    for group in groups.values():
        if group["size"] < min_size:
            continue
        group["paths"].sort(key=lambda p: p.lower())
        kind = group["kind"]
        confidence = 0.86 if kind == "app" else 0.7
        candidates.append({
            **group,
            "confidence": confidence,
            "evidence": [{
                "source": "registry_absence",
                "conf": confidence,
                "detail": "当前软件台账中没有对应登记，但快照中的痕迹目录仍然存在",
            }],
            "last_seen": scanned_at,
        })
    candidates.sort(key=lambda x: x["size"], reverse=True)
    return {
        "candidates": candidates,
        "summary": {
            "count": len(candidates),
            "total_size": sum(x["size"] for x in candidates),
        },
    }


__all__ = ["MIN_RESIDUE_BYTES", "detect"]
