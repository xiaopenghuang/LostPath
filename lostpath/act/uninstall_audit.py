"""软件卸载前后差异审计，以及残留的可回滚清理编排。"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from lostpath import startup, sysdirs
from lostpath.software_identity import match_registry_entity

from . import environment, executor, manifest, registry_health


GENERIC_ENVIRONMENT_NAMES = {"path", "pathext", "temp", "tmp", "comspec"}


class UninstallAuditError(RuntimeError):
    """深度卸载审计缺少基线，或清理请求不在审计候选内。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidate_id(kind: str, identity: str) -> str:
    raw = f"{kind}|{identity.casefold()}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def _entity_snapshot(entity: dict) -> dict:
    traces = []
    for trace in entity.get("traces") or []:
        traces.append({
            "path": trace.get("path"),
            "name": trace.get("name"),
            "role": trace.get("role"),
            "cat": trace.get("cat"),
            "owner": trace.get("owner"),
            "size": trace.get("size"),
            "files": trace.get("files"),
            "conf": trace.get("conf"),
        })
    return {
        "id": entity.get("id"),
        "name": entity.get("name"),
        "publisher": entity.get("publisher"),
        "source": entity.get("source"),
        "icon": entity.get("icon"),
        "location": entity.get("location"),
        "estimated_size": entity.get("estimated_size"),
        "shared_vendor": bool(entity.get("shared_vendor")),
        "redirects": list(entity.get("redirects") or []),
        "traces": traces,
    }


def capture_baseline(entry: dict, entities: list[dict]) -> dict | None:
    """在启动原生卸载器前保存关系基线，不保存变量值或卸载命令。"""
    relation = match_registry_entity(entry, entities)
    if not relation:
        return None
    entity = next((item for item in entities
                   if item.get("id") == relation.get("entity_id")), None)
    if not entity:
        return None

    environment_items = environment.report(entities).get("items", [])
    registry_items = registry_health.report(entities).get("items", [])
    # 卸载一旦启动就无法重建“卸载前”的启动链路。首次直接进入卸载页时后台采集
    # 可能尚未跑过，因此这里等待共享任务完成，而不是把 loading 的空列表当基线。
    startup_state = startup.wait_for_report(entities)
    entity_id = entity.get("id")
    return {
        "captured_at": _now(),
        "entity": _entity_snapshot(entity),
        "environment": [{"id": item.get("id"), "name": item.get("name"),
                          "scope": item.get("scope")}
                         for item in environment_items
                         if any(row.get("entity_id") == entity_id
                                for row in item.get("relations") or [])],
        "registry": [{"id": item.get("id"), "path": item.get("registry_path")}
                     for item in registry_items
                     if (item.get("entity") or {}).get("entity_id") == entity_id],
        "startup": [{"id": item.get("id"), "kind": item.get("kind")}
                    for item in startup_state.get("items") or []
                    if item.get("owner_id") == entity_id],
        "startup_state": startup_state.get("state"),
    }


def _audit_entities(baseline: dict, current_entities: list[dict]) -> list[dict]:
    target = baseline["entity"]
    if any(item.get("id") == target.get("id") for item in current_entities):
        return current_entities
    return [*current_entities, target]


def _is_junction(path: str) -> bool:
    try:
        return Path(path).is_junction()
    except (AttributeError, OSError):
        return os.path.islink(path)


def _file_candidates(entity: dict) -> list[dict]:
    if entity.get("shared_vendor"):
        return []
    rows = []
    seen: set[str] = set()
    sources = []
    if entity.get("location"):
        sources.append({
            "path": entity["location"],
            "name": "卸载后安装目录",
            "role": "软件本体残留",
            "cat": "软件本体",
            "size": entity.get("estimated_size"),
            "files": None,
            "conf": 1.0,
            "recommended": False,
        })
    for trace in entity.get("traces") or []:
        sources.append({**trace, "recommended": trace.get("cat") == "可再生缓存"})

    for source in sources:
        path = source.get("path")
        if not isinstance(path, str) or not path or not os.path.isdir(path):
            continue
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        confidence = float(source.get("conf") or 0)
        junction = _is_junction(path)
        protected = sysdirs.protected_system_dir(path)
        is_root = len(os.path.abspath(path).rstrip("\\")) <= 2
        source_drive = os.path.splitdrive(os.path.abspath(path))[0].casefold()
        recycle_drive = os.path.splitdrive(
            os.path.abspath(str(manifest.recycle_dir())))[0].casefold()
        same_volume = source_drive == recycle_drive
        can_clean = (confidence >= 0.75 and not junction and not protected
                     and not is_root and same_volume)
        category = source.get("cat") or "未定性"
        rows.append({
            "id": _candidate_id("file", normalized),
            "kind": "file",
            "action": "recycle_directory" if can_clean else "manual",
            "name": source.get("role") or source.get("name") or os.path.basename(path),
            "path": path,
            "size": source.get("size"),
            "files": source.get("files"),
            "category": category,
            "can_clean": can_clean,
            "recommended": bool(source.get("recommended") and can_clean),
            "risk": "safe" if source.get("recommended") else "review",
            "reason": ("卸载前已由证据链归属于该软件，清理时只移入 LostPath 回收区"
                       if can_clean else
                       "路径置信度不足、跨盘、属于 junction 或受系统目录保护，只保留人工核对入口"),
            "confidence": confidence,
        })
    return rows


def _environment_candidates(entity_id: str, report: dict) -> list[dict]:
    rows = []
    for item in report.get("items") or []:
        relation = next((row for row in item.get("relations") or []
                         if row.get("entity_id") == entity_id), None)
        if not relation:
            continue
        relations = item.get("relations") or []
        generic = item.get("name", "").casefold() in GENERIC_ENVIRONMENT_NAMES
        exclusive = len(relations) == 1
        can_clean = item.get("scope") == "user" and exclusive and not generic
        rows.append({
            # 把读取指纹纳入候选身份。预演后若其它程序改了变量，实际提交时旧候选
            # 会直接失效，而不是重新读取新值后把用户从未确认过的内容删掉。
            "id": _candidate_id(
                "environment",
                f"{item.get('scope')}|{item.get('name')}|{item.get('fingerprint')}",
            ),
            "kind": "environment",
            "action": "env_delete" if can_clean else "manual",
            "name": item.get("name"),
            "scope": item.get("scope"),
            "masked": bool(item.get("masked")),
            "fingerprint": item.get("fingerprint"),
            "can_clean": can_clean,
            "recommended": can_clean and relation.get("confidence") == 1.0,
            "risk": "safe" if can_clean else "manual",
            "reason": (relation.get("reason") if can_clean else
                       "系统级、通用变量或同时关联多个软件，不自动删除"),
            "confidence": relation.get("confidence"),
        })
    return rows


def _registry_candidates(entity_id: str, report: dict) -> list[dict]:
    rows = []
    for item in report.get("items") or []:
        relation = item.get("entity") or {}
        if relation.get("entity_id") != entity_id:
            continue
        can_clean = bool(item.get("can_clean"))
        rows.append({
            "id": _candidate_id("registry", item.get("id") or ""),
            "item_id": item.get("id"),
            "kind": "registry",
            "action": "registry_cleanup" if can_clean else "manual",
            "name": item.get("name"),
            "path": item.get("registry_path"),
            "status": item.get("status"),
            "scope": item.get("scope"),
            "can_clean": can_clean,
            "recommended": can_clean,
            "risk": "safe" if can_clean else "manual",
            "reason": (item.get("reason") if can_clean else
                       "仅 HKCU 中安装目录与卸载器均失效的登记允许自动清理"),
            "confidence": relation.get("confidence"),
        })
    return rows


def _startup_candidates(entity_id: str, report: dict) -> list[dict]:
    rows = []
    for item in report.get("items") or []:
        if item.get("owner_id") != entity_id:
            continue
        manage = item.get("manage") or {}
        can_clean = bool(manage.get("can_disable"))
        rows.append({
            "id": _candidate_id("startup", item.get("id") or ""),
            "item_id": item.get("id"),
            "kind": "startup",
            "startup_kind": item.get("kind"),
            "action": "startup_disable" if can_clean else "manual",
            "name": item.get("name"),
            "source": item.get("source"),
            "can_clean": can_clean,
            "recommended": can_clean,
            "risk": "safe" if can_clean else "manual",
            "reason": (manage.get("reason") if manage.get("reason") else
                       "服务与计划任务保持只读，只列出供人工核对"),
            "confidence": item.get("owner_confidence"),
        })
    return rows


def build_audit(operation_id: str, data: dict) -> dict:
    op = manifest.find(operation_id)
    if not op or op.get("action") != "uninstall_launch":
        raise UninstallAuditError("找不到这次卸载操作")
    if not op.get("uninstall_verified_at"):
        raise UninstallAuditError("卸载登记尚未移除，请先完成卸载并复核")
    baseline = op.get("uninstall_baseline")
    if not baseline or not baseline.get("entity"):
        raise UninstallAuditError("这次卸载没有关系基线，无法自动判断深度残留")

    current_entities = data.get("software") or []
    entities = _audit_entities(baseline, current_entities)
    entity = baseline["entity"]
    entity_id = entity.get("id")
    environment_report = environment.report(entities)
    registry_report = registry_health.report(entities)
    startup_report = startup.report(entities)
    candidates = [
        *_file_candidates(entity),
        *_environment_candidates(entity_id, environment_report),
        *_registry_candidates(entity_id, registry_report),
        *_startup_candidates(entity_id, startup_report),
    ]
    current_ids = {
        "environment": {item["id"] for item in environment_report.get("items") or []
                        if any(row.get("entity_id") == entity_id
                               for row in item.get("relations") or [])},
        "registry": {item["id"] for item in registry_report.get("items") or []
                     if (item.get("entity") or {}).get("entity_id") == entity_id},
        "startup": {item["id"] for item in startup_report.get("items") or []
                    if item.get("owner_id") == entity_id},
    }
    before = {kind: len(baseline.get(kind) or [])
              for kind in ("environment", "registry", "startup")}
    removed = {
        kind: len({item["id"] for item in baseline.get(kind) or []} - current_ids[kind])
        for kind in current_ids
    }
    return {
        "operation_id": operation_id,
        "name": op.get("uninstall_name") or entity.get("name"),
        "entity": {
            "entity_id": entity_id,
            "name": entity.get("name"),
            "publisher": entity.get("publisher"),
            "icon": entity.get("icon"),
        },
        "audited_at": _now(),
        "startup_state": startup_report.get("state"),
        "candidates": candidates,
        "changes": {
            kind: {"before": before[kind], "remaining": len(current_ids[kind]),
                   "removed": removed[kind]}
            for kind in current_ids
        },
        "summary": {
            "total": len(candidates),
            "actionable": sum(bool(item.get("can_clean")) for item in candidates),
            "recommended": sum(bool(item.get("recommended")) for item in candidates),
            "files": sum(item.get("kind") == "file" for item in candidates),
            "environment": sum(item.get("kind") == "environment" for item in candidates),
            "registry": sum(item.get("kind") == "registry" for item in candidates),
            "startup": sum(item.get("kind") == "startup" for item in candidates),
            "file_size": sum((item.get("size") or 0) for item in candidates
                             if item.get("kind") == "file"),
        },
    }


def execute_cleanup(operation_id: str, candidate_ids: list[str], data: dict,
                    dry_run: bool = True) -> dict:
    audit = build_audit(operation_id, data)
    by_id = {item["id"]: item for item in audit["candidates"]}
    selected_ids = list(dict.fromkeys(candidate_ids))
    if not selected_ids:
        raise UninstallAuditError("至少选择一项可清理残留")
    missing = [item_id for item_id in selected_ids if item_id not in by_id]
    if missing:
        raise UninstallAuditError("残留清单已经变化，请重新复核后再清理")
    refused = [by_id[item_id]["name"] for item_id in selected_ids
               if not by_id[item_id].get("can_clean")]
    if refused:
        raise UninstallAuditError(f"这些项目只允许人工核对：{'、'.join(refused)}")

    results = []
    child_ids = []
    for item_id in selected_ids:
        candidate = by_id[item_id]
        try:
            if candidate["action"] == "recycle_directory":
                child = executor.execute_residue_recycle(
                    candidate, operation_id, dry_run=dry_run)
            elif candidate["action"] == "env_delete":
                child = environment.delete_value(
                    candidate["name"], candidate["fingerprint"], dry_run=dry_run)
            elif candidate["action"] == "registry_cleanup":
                child = registry_health.cleanup(
                    candidate["item_id"], dry_run=dry_run)
            elif candidate["action"] == "startup_disable":
                child = startup.disable(candidate["item_id"], dry_run=dry_run)
            else:  # 前面的 can_clean 闸门之后不应到这里
                raise UninstallAuditError("未知的残留清理动作")
        except Exception as exc:
            results.append({
                "candidate_id": item_id,
                "name": candidate.get("name"),
                "ok": False,
                "error": str(exc),
            })
            continue
        public = manifest.public_operation(child)
        results.append({
            "candidate_id": item_id,
            "name": candidate.get("name"),
            "ok": True,
            "operation": public,
        })
        if not dry_run and public.get("id"):
            child_ids.append(public["id"])

    if not dry_run and child_ids:
        parent = manifest.find(operation_id)
        runs = list(parent.get("deep_cleanup_runs") or [])
        runs.append({"at": _now(), "operation_ids": child_ids})
        manifest.mark(parent, parent.get("status", "done"), deep_cleanup_runs=runs)

    return {
        "dry_run": dry_run,
        "results": results,
        "succeeded": sum(bool(item.get("ok")) for item in results),
        "failed": sum(not item.get("ok") for item in results),
        "audit": audit if dry_run else build_audit(operation_id, data),
    }


__all__ = [
    "UninstallAuditError", "build_audit", "capture_baseline", "execute_cleanup",
]
