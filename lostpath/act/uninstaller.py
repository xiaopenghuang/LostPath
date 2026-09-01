"""调用软件登记的卸载器，并记录启动与复核结果。

这里不自行删除程序目录，也不执行静默卸载。完整命令只从注册表读入并保存在本地 manifest，
API 只返回软件名称、范围和状态。卸载本身不可回滚，因此操作明确标记为不可撤销。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone

from . import manifest, registry_health, uninstall_audit


class UninstallActionError(RuntimeError):
    """卸载器无法安全启动或复核。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_command(command: str) -> str:
    """MSI 的 ARP 登记常写 /I，卸载入口应改成 /X；其它命令保持原样。"""
    executable = registry_health.command_executable(command)
    if os.path.basename(executable or "").casefold() not in {"msiexec", "msiexec.exe"}:
        return command
    return re.sub(r"(?i)(?<!\S)/I(?=\s*\{)", "/X", command, count=1)


def _can_launch(entry: dict) -> tuple[bool, str | None]:
    command = entry.get("uninstall_string")
    if not isinstance(command, str) or not command.strip():
        return False, "软件没有登记卸载命令"
    executable = registry_health.command_executable(command)
    if not executable:
        return False, "无法解析卸载器路径"
    if entry.get("uninstaller_exists") is False:
        return False, "登记的卸载器文件已经不存在"
    return True, None


def _public_entry(entry: dict, entities: list[dict] | None = None) -> dict:
    can_launch, reason = _can_launch(entry)
    relation = registry_health.public_entry(entry, entities).get("entity")
    return {
        "id": entry["id"],
        "name": entry["name"],
        "version": entry.get("version"),
        "publisher": entry.get("publisher"),
        "scope": entry.get("scope"),
        "hive": entry.get("hive"),
        "location": entry.get("location"),
        "location_exists": entry.get("location_exists"),
        "estimated_size": entry.get("estimated_size"),
        "can_uninstall": can_launch,
        "reason": reason,
        "entity": relation,
        "icon": relation.get("icon") if relation else None,
    }


def report(entities: list[dict] | None = None) -> dict:
    entries = [entry for entry in registry_health.enumerate_entries()
               if not entry.get("system_component")]
    items = [_public_entry(entry, entities) for entry in entries]
    items.sort(key=lambda row: (not row["can_uninstall"], row["name"].casefold()))
    recent = []
    for op in manifest.list_operations():
        if op.get("action") != "uninstall_launch":
            continue
        recent.append({
            "operation_id": op.get("id"),
            "item_id": op.get("uninstall_item_id"),
            "name": op.get("uninstall_name") or "未命名软件",
            "status": op.get("status"),
            "created_at": op.get("created_at"),
            "verified_removed": bool(op.get("uninstall_verified_at")),
            "baseline_captured": bool(op.get("uninstall_baseline")),
            "deep_cleaned": bool(op.get("deep_cleanup_runs")),
            "failure": op.get("failure"),
        })
    return {
        "items": items,
        "recent": recent,
        "summary": {
            "total": len(items),
            "user": sum(row["scope"] == "user" for row in items),
            "system": sum(row["scope"] == "system" for row in items),
            "uninstallable": sum(bool(row["can_uninstall"]) for row in items),
            "needs_repair": sum(not row["can_uninstall"] for row in items),
            "associated": sum(bool(row.get("entity")) for row in items),
        },
        "read_at": _now(),
    }


def launch(item_id: str, dry_run: bool = True,
           entities: list[dict] | None = None) -> dict:
    entry = registry_health.find_entry(item_id)
    if not entry:
        raise UninstallActionError("软件登记已不存在，请刷新后重试")
    can_launch, reason = _can_launch(entry)
    if not can_launch:
        raise UninstallActionError(reason or "卸载器不可用")
    command = _normalize_command(entry["uninstall_string"])
    executable = registry_health.command_executable(command)
    if not executable:
        raise UninstallActionError("无法解析卸载器路径")
    launch_executable = (executable if os.path.isabs(executable)
                         else shutil.which(executable) or executable)
    op = manifest.new_operation("uninstall_launch", {
        "path": f"{entry['hive']}\\{entry['subkey']}",
        "name": entry["name"],
        "publisher": entry.get("publisher"),
    })
    op.update({
        "uninstall_item_id": item_id,
        "uninstall_name": entry["name"],
        "uninstall_command": command,
        "rollback_supported": False,
        "recoverable_until": None,
    })
    baseline = uninstall_audit.capture_baseline(entry, entities or []) if entities else None
    if baseline:
        op["uninstall_baseline"] = baseline
        op["uninstall_baseline_captured"] = True
    if dry_run:
        op["status"] = "dry_run"
        return manifest.public_operation(op)
    manifest.save(op)
    try:
        # shell=False：注册表里的 &、| 等字符不能被 cmd.exe 当成额外命令解释。
        process = subprocess.Popen(
            command,
            # 显式传 lpApplicationName，避免无引号的
            # C:\Program Files\... 被 Windows 按 C:\Program.exe 歧义解析。
            executable=launch_executable,
            shell=False,
            close_fds=True,
            cwd=entry["location"] if entry.get("location_exists") else None,
        )
        op["uninstall_pid"] = process.pid
        op["uninstall_launched_at"] = _now()
        manifest.add_step(op, "vendor_uninstaller_launched")
        # done 表示“卸载器已成功交给 Windows”，是否卸载完成由 verify 再确认。
        manifest.mark(op, "done")
    except Exception as exc:
        manifest.mark(op, "failed", failure=f"{type(exc).__name__}: {exc}")
        raise UninstallActionError(f"启动卸载器失败：{exc}") from exc
    return manifest.public_operation(op)


def verify(operation_id: str) -> dict:
    op = manifest.find(operation_id)
    if not op or op.get("action") != "uninstall_launch":
        raise UninstallActionError("找不到这次卸载操作")
    item_id = op.get("uninstall_item_id")
    still_installed = registry_health.find_entry(item_id) is not None
    if not still_installed and not op.get("uninstall_verified_at"):
        manifest.mark(op, op.get("status", "done"), uninstall_verified_at=_now())
    return {
        "operation_id": operation_id,
        "item_id": item_id,
        "name": op.get("uninstall_name"),
        "still_installed": still_installed,
        "verified_removed": not still_installed,
    }


__all__ = ["UninstallActionError", "launch", "report", "verify"]
