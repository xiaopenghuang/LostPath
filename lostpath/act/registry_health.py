"""卸载注册表巡检，以及当前用户失效登记的可恢复清理。

这里只检查 Windows 的 Uninstall 清单，不提供任意注册表编辑。机器级登记保持只读；
只有卸载器和安装目录都已失效的 HKCU 登记可以清理，整棵键会先备份到 manifest。
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
from datetime import datetime, timezone

from lostpath.software_identity import match_registry_entity

from . import manifest

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None


UNINSTALL_HIVES = [
    ("HKLM64", "system", getattr(winreg, "HKEY_LOCAL_MACHINE", None),
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM32", "system", getattr(winreg, "HKEY_LOCAL_MACHINE", None),
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", "user", getattr(winreg, "HKEY_CURRENT_USER", None),
     r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
]


class RegistryActionError(RuntimeError):
    """注册表巡检操作被拒绝或失败。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _query(key, name: str):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _entry_id(label: str, key_name: str) -> str:
    return hashlib.sha1(f"{label}|{key_name}".encode("utf-8", "replace")).hexdigest()[:16]


def _clean_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return os.path.expandvars(value.strip().strip('"')).rstrip("\\/") or None


def command_executable(command: object) -> str | None:
    """从卸载命令中提取可执行文件，仅用于存在性检查，不执行。"""
    if not isinstance(command, str) or not command.strip():
        return None
    text = os.path.expandvars(command.strip())
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 1 else None
    match = re.match(r"(.+?\.exe)(?:\s|$)", text, re.I)
    if match:
        return match.group(1).strip()
    return text.split(None, 1)[0].strip('"') if text.split(None, 1) else None


def _executable_exists(command: object) -> bool | None:
    executable = command_executable(command)
    if not executable:
        return None
    base = os.path.basename(executable).casefold()
    if base in {"msiexec", "msiexec.exe"}:
        return True
    if os.path.isabs(executable):
        return os.path.isfile(executable)
    return shutil.which(executable) is not None


def classify(entry: dict) -> dict:
    """保守判定登记健康度。只有双重失效才标 orphaned。"""
    location = _clean_path(entry.get("install_location"))
    location_exists = os.path.isdir(location) if location else None
    uninstall_exists = _executable_exists(entry.get("uninstall_string"))
    if uninstall_exists is True:
        status = "healthy" if location_exists is not False else "location_missing"
        reason = ("卸载器可用" if location_exists is not False else
                  "登记的安装目录不存在，但卸载器仍可用")
    elif location_exists is True:
        status = "uninstaller_missing"
        reason = "安装目录仍存在，但卸载器缺失或无法定位"
    elif uninstall_exists is False or (uninstall_exists is None and location_exists is False):
        status = "orphaned"
        reason = "安装目录和卸载器均不存在，属于失效卸载登记"
    else:
        status = "incomplete"
        reason = "登记缺少足够路径信息，无法确认是否失效"
    return {
        **entry,
        "status": status,
        "reason": reason,
        "location": location,
        "location_exists": location_exists,
        "uninstaller_exists": uninstall_exists,
        "can_clean": status == "orphaned" and entry.get("scope") == "user",
    }


def enumerate_entries() -> list[dict]:
    if winreg is None:
        return []
    out = []
    for label, scope, root, base in UNINSTALL_HIVES:
        try:
            with winreg.OpenKey(root, base, 0, winreg.KEY_READ) as parent:
                count = winreg.QueryInfoKey(parent)[0]
                names = []
                for index in range(count):
                    try:
                        names.append(winreg.EnumKey(parent, index))
                    except OSError:
                        continue
        except OSError:
            continue
        for key_name in names:
            subkey = base + "\\" + key_name
            try:
                with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
                    display = _query(key, "DisplayName")
                    if not isinstance(display, str) or not display.strip():
                        continue
                    estimated_kb = _query(key, "EstimatedSize")
                    entry = {
                        "id": _entry_id(label, key_name),
                        "key_name": key_name,
                        "hive": label,
                        "scope": scope,
                        "subkey": subkey,
                        "name": display.strip(),
                        "version": _query(key, "DisplayVersion"),
                        "publisher": _query(key, "Publisher"),
                        "install_location": _query(key, "InstallLocation"),
                        "uninstall_string": _query(key, "UninstallString"),
                        "quiet_uninstall_string": _query(key, "QuietUninstallString"),
                        "estimated_size": estimated_kb * 1024
                        if isinstance(estimated_kb, int) and estimated_kb > 0 else None,
                        "system_component": _query(key, "SystemComponent") == 1,
                        "root": root,
                    }
            except OSError:
                continue
            out.append(classify(entry))
    return out


def public_entry(entry: dict, entities: list[dict] | None = None) -> dict:
    """去掉注册表句柄与完整卸载命令，保留可核对的登记路径和软件关系。"""
    public = {key: value for key, value in entry.items() if key not in {
        "root", "subkey", "uninstall_string", "quiet_uninstall_string",
        "install_location", "key_name",
    }}
    public["registry_path"] = f"{entry['hive']}\\{entry['subkey']}"
    public["entity"] = match_registry_entity(entry, entities or [])
    return public


def find_entry(item_id: str) -> dict | None:
    return next((entry for entry in enumerate_entries() if entry["id"] == item_id), None)


def report(entities: list[dict] | None = None) -> dict:
    items = [public_entry(entry, entities) for entry in enumerate_entries()]
    items.sort(key=lambda row: (
        {"orphaned": 0, "uninstaller_missing": 1, "location_missing": 2,
         "incomplete": 3, "healthy": 4}.get(row["status"], 5),
        row["name"].casefold(),
    ))
    removed = []
    for op in manifest.list_operations():
        if op.get("action") != "registry_cleanup" or op.get("status") != "done":
            continue
        removed.append({
            "operation_id": op.get("id"),
            "name": op.get("registry_name") or "未命名登记",
            "registry_path": op.get("source_path"),
            "created_at": op.get("created_at"),
            "can_restore": True,
        })
    return {
        "items": items,
        "removed": removed,
        "summary": {
            "total": len(items),
            "healthy": sum(row["status"] == "healthy" for row in items),
            "attention": sum(row["status"] != "healthy" for row in items),
            "orphaned": sum(row["status"] == "orphaned" for row in items),
            "manageable": sum(bool(row["can_clean"]) for row in items),
            "associated": sum(bool(row.get("entity")) for row in items),
            "removed": len(removed),
        },
        "read_at": _now(),
    }


def _encode_data(value) -> dict:
    if isinstance(value, bytes):
        return {"encoding": "base64", "value": base64.b64encode(value).decode("ascii")}
    return {"encoding": "json", "value": value}


def _decode_data(value: dict):
    if value.get("encoding") == "base64":
        return base64.b64decode(value.get("value") or "")
    return value.get("value")


def _snapshot_key(root, subkey: str) -> dict:
    with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
        child_count, value_count, _stamp = winreg.QueryInfoKey(key)
        values = []
        for index in range(value_count):
            name, value, value_type = winreg.EnumValue(key, index)
            values.append({"name": name, "type": value_type, "data": _encode_data(value)})
        children = []
        for index in range(child_count):
            child = winreg.EnumKey(key, index)
            children.append({"name": child,
                             "tree": _snapshot_key(root, subkey + "\\" + child)})
    return {"values": values, "children": children}


def _delete_tree(root, subkey: str) -> None:
    try:
        with winreg.OpenKey(root, subkey, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            children = []
            while True:
                try:
                    children.append(winreg.EnumKey(key, len(children)))
                except OSError:
                    break
    except OSError as exc:
        raise RegistryActionError(f"无法打开待清理登记：{exc}") from exc
    for child in children:
        _delete_tree(root, subkey + "\\" + child)
    winreg.DeleteKey(root, subkey)


def _key_exists(root, subkey: str) -> bool:
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ):
            return True
    except OSError:
        return False


def _restore_key(root, subkey: str, tree: dict) -> None:
    with winreg.CreateKeyEx(root, subkey, 0, winreg.KEY_WRITE) as key:
        for row in tree.get("values") or []:
            winreg.SetValueEx(key, row["name"], 0, row["type"],
                              _decode_data(row["data"]))
    for child in tree.get("children") or []:
        _restore_key(root, subkey + "\\" + child["name"], child["tree"])


def cleanup(item_id: str, dry_run: bool = True) -> dict:
    entry = find_entry(item_id)
    if not entry:
        raise RegistryActionError("登记已不存在，请刷新后重试")
    if not entry.get("can_clean"):
        raise RegistryActionError("只允许清理当前用户中安装目录和卸载器均失效的登记")
    op = manifest.new_operation("registry_cleanup", {
        "path": f"{entry['hive']}\\{entry['subkey']}",
        "name": entry["name"],
        "reason": entry["reason"],
    })
    op.update({
        "registry_name": entry["name"],
        "registry_hive": entry["hive"],
        "registry_subkey": entry["subkey"],
        "rollback_supported": True,
    })
    if dry_run:
        op["status"] = "dry_run"
        return manifest.public_operation(op)
    try:
        op["registry_backup"] = _snapshot_key(entry["root"], entry["subkey"])
        manifest.save(op)
        _delete_tree(entry["root"], entry["subkey"])
        if _key_exists(entry["root"], entry["subkey"]):
            raise RegistryActionError("清理后登记仍然存在")
        manifest.add_step(op, "registry_key_deleted")
        manifest.mark(op, "done")
    except Exception as exc:
        if op.get("registry_backup") is not None:
            manifest.mark(op, "failed", failure=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, RegistryActionError):
            raise
        raise RegistryActionError(f"清理注册表登记失败：{exc}") from exc
    return manifest.public_operation(op)


def recovery_state(op: dict) -> tuple[bool, str]:
    if op.get("action") != "registry_cleanup":
        return False, "不是注册表清理操作"
    if op.get("status") not in {"done", "planned", "failed"}:
        return False, f"状态为 {op.get('status')}，无需恢复"
    subkey = op.get("registry_subkey")
    if (op.get("registry_hive") != "HKCU" or not subkey
            or not isinstance(op.get("registry_backup"), dict)):
        return False, "操作记录没有完整的当前用户注册表备份"
    root = getattr(winreg, "HKEY_CURRENT_USER", None)
    if _key_exists(root, subkey):
        return False, "原注册表路径已经存在，拒绝覆盖"
    return True, "原登记仍缺失且备份完整，可以恢复"


def restore(operation_id: str) -> dict:
    op = manifest.find(operation_id)
    if not op or op.get("action") != "registry_cleanup":
        raise RegistryActionError("找不到可恢复的注册表操作")
    if op.get("status") == "rolled_back":
        raise RegistryActionError("该登记已经恢复")
    if (op.get("status") not in {"done", "planned", "failed"}
            or not op.get("registry_backup")):
        raise RegistryActionError("该操作没有完整备份，不能自动恢复")
    root = getattr(winreg, "HKEY_CURRENT_USER", None)
    subkey = op.get("registry_subkey")
    if op.get("registry_hive") != "HKCU" or not subkey:
        raise RegistryActionError("只支持恢复当前用户的卸载登记")
    if _key_exists(root, subkey):
        raise RegistryActionError("原注册表路径已经重新出现，拒绝覆盖")
    try:
        _restore_key(root, subkey, op["registry_backup"])
        manifest.add_step(op, "registry_key_restored")
        manifest.mark(op, "rolled_back")
    except Exception as exc:
        raise RegistryActionError(f"恢复注册表登记失败：{exc}") from exc
    return manifest.public_operation(op)


__all__ = [
    "RegistryActionError", "cleanup", "command_executable", "enumerate_entries",
    "find_entry", "public_entry", "recovery_state", "report", "restore",
]
