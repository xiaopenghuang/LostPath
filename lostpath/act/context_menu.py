"""Windows Explorer 右键菜单巡检、自定义命令与可恢复管理。

右键菜单主要来自注册表的两套机制：普通 shell 命令，以及 COM 上下文菜单处理器。
普通命令通过 ``LegacyDisable`` 隐藏，COM 处理器通过当前用户级 ``Blocked`` 清单禁用。
两种操作都只增加一个标记，不删除原命令、CLSID 或程序文件；恢复时只移除 LostPath
自己写入的标记。自定义命令只写当前用户注册表，删除前完整备份；Windows 核心命令
和位于 Windows 目录内的处理器保持只读。
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from lostpath.software_identity import match_path_entity, normalize_path

from . import manifest, registry_health
from .registry_health import command_executable

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows development environments
    winreg = None


BLOCKED_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Shell Extensions\Blocked"
SURFACES = (
    ("files", "所有文件", "*"),
    ("filesystem", "文件和文件夹", "AllFilesystemObjects"),
    ("folder", "文件夹", "Directory"),
    ("folder_background", "文件夹空白处", r"Directory\Background"),
    ("drive", "磁盘", "Drive"),
    ("desktop", "桌面空白处", "DesktopBackground"),
    ("folder_object", "文件夹对象", "Folder"),
)
SURFACE_BY_ID = {surface_id: (name, relative) for surface_id, name, relative in SURFACES}
CREATABLE_SURFACE_IDS = ("files", "folder", "folder_background", "drive")
SURFACE_ARGUMENT = {
    "files": "%1",
    "folder": "%1",
    "folder_background": "%V",
    "drive": "%1",
}
PROTECTED_VERBS = {
    "open", "opennewwindow", "explore", "find", "properties", "runas",
}
_GUID_RE = re.compile(
    r"^\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}$",
    re.I,
)
_DLL_RE = re.compile(r'(?i)(?:"([^"\r\n]+?\.dll)"|([^\s,]+?\.dll))')

_cache_lock = threading.Lock()
_cache_at = 0.0
_cache_rows: list[dict] = []
CACHE_SECONDS = 15.0


class ContextMenuActionError(RuntimeError):
    """右键菜单操作被拒绝或执行失败。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sources() -> list[tuple[str, str, object, str, int]]:
    if winreg is None:
        return []
    return [
        ("HKCU", "user", winreg.HKEY_CURRENT_USER, r"Software\Classes", 0),
        ("HKLM64", "system", winreg.HKEY_LOCAL_MACHINE, r"Software\Classes",
         getattr(winreg, "KEY_WOW64_64KEY", 0)),
        ("HKLM32", "system", winreg.HKEY_LOCAL_MACHINE, r"Software\Classes",
         getattr(winreg, "KEY_WOW64_32KEY", 0)),
    ]


def _query(root, subkey: str, name: str = "", view: int = 0) -> tuple[bool, object, int | None]:
    if winreg is None:
        return False, None, None
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as key:
            value, value_type = winreg.QueryValueEx(key, name)
            return True, value, value_type
    except OSError:
        return False, None, None


def _enum_subkeys(root, subkey: str, view: int = 0) -> list[str]:
    if winreg is None:
        return []
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as key:
            count = winreg.QueryInfoKey(key)[0]
            names = []
            for index in range(count):
                try:
                    names.append(winreg.EnumKey(key, index))
                except OSError:
                    continue
            return names
    except OSError:
        return []


def _display_name(*values: object, fallback: str) -> str:
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip().replace("&&", "\0").replace("&", "").replace("\0", "&")
        if text and not text.startswith("@") and not _GUID_RE.match(text):
            return text
    return fallback


def _command_target(command: object) -> str | None:
    if not isinstance(command, str) or not command.strip():
        return None
    executable = command_executable(command)
    if executable and os.path.basename(executable).casefold() in {"rundll32", "rundll32.exe"}:
        match = _DLL_RE.search(os.path.expandvars(command))
        if match:
            return (match.group(1) or match.group(2)).strip('"')
    return os.path.expandvars(executable) if executable else None


def _server_target(value: object) -> str | None:
    """Parse an InprocServer32/LocalServer32 path without truncating spaces."""
    if not isinstance(value, str) or not value.strip():
        return None
    expanded = os.path.expandvars(value.strip())
    match = re.match(r'^"?(.+?\.(?:dll|exe))"?(?:\s|,|$)', expanded, re.I)
    return match.group(1) if match else _command_target(expanded)


def _is_windows_component(target: str | None) -> bool:
    path = normalize_path(target)
    if not path:
        return False
    tail = os.path.splitdrive(path)[1]
    return tail == r"\windows" or tail.startswith("\\windows\\")


def _item_id(kind: str, locator: str) -> str:
    raw = f"{kind}|{locator.casefold()}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _scan_registrations() -> list[dict]:
    rows: list[dict] = []
    for hive, scope, root, classes, view in _sources():
        for surface_id, surface_name, relative in SURFACES:
            shell = f"{classes}\\{relative}\\shell"
            for verb in _enum_subkeys(root, shell, view):
                subkey = f"{shell}\\{verb}"
                _default_ok, default, _default_type = _query(root, subkey, "", view)
                _mui_ok, mui_verb, _mui_type = _query(root, subkey, "MUIVerb", view)
                command_ok, command, _command_type = _query(
                    root, subkey + r"\command", "", view)
                explorer_ok, explorer_handler, _explorer_type = _query(
                    root, subkey, "ExplorerCommandHandler", view)
                legacy_disabled, _legacy, _legacy_type = _query(
                    root, subkey, "LegacyDisable", view)
                managed_ok, managed, _managed_type = _query(
                    root, subkey, "LostPathManaged", view)
                custom_id_ok, custom_id, _custom_id_type = _query(
                    root, subkey, "LostPathCommandId", view)
                if not command_ok and not explorer_ok and not _enum_subkeys(root, subkey, view):
                    continue
                relative_key = f"{relative}\\shell\\{verb}"
                rows.append({
                    "kind": "command",
                    "name": _display_name(mui_verb, default, fallback=verb),
                    "verb": verb,
                    "hive": hive,
                    "scope": scope,
                    "surface_id": surface_id,
                    "surface_name": surface_name,
                    "relative_key": relative_key,
                    "registry_path": f"{hive}\\{subkey}",
                    "target": _command_target(command),
                    "command": command if command_ok else None,
                    "handler_clsid": explorer_handler if explorer_ok else None,
                    "source_disabled": legacy_disabled,
                    "custom": bool(
                        hive == "HKCU" and managed_ok and managed == 1
                        and custom_id_ok and isinstance(custom_id, str) and custom_id
                    ),
                    "custom_id": custom_id if custom_id_ok else None,
                    "subkey": subkey,
                    "source_rank": 0 if hive == "HKCU" else 1 if hive == "HKLM64" else 2,
                })

            handlers = f"{classes}\\{relative}\\shellex\\ContextMenuHandlers"
            for handler_name in _enum_subkeys(root, handlers, view):
                subkey = f"{handlers}\\{handler_name}"
                value_ok, value, _value_type = _query(root, subkey, "", view)
                clsid = str(value or handler_name).strip() if value_ok else handler_name.strip()
                if not _GUID_RE.match(clsid):
                    continue
                rows.append({
                    "kind": "handler",
                    "name": handler_name,
                    "hive": hive,
                    "scope": scope,
                    "surface_id": surface_id,
                    "surface_name": surface_name,
                    "registry_path": f"{hive}\\{subkey}",
                    "handler_clsid": clsid.upper(),
                    "source_root": root,
                    "source_classes": classes,
                    "source_view": view,
                    "source_rank": 0 if hive == "HKCU" else 1 if hive == "HKLM64" else 2,
                })
    return rows


def _registrations(force: bool = False) -> list[dict]:
    global _cache_at, _cache_rows
    now = time.monotonic()
    with _cache_lock:
        if not force and _cache_rows and now - _cache_at < CACHE_SECONDS:
            return [dict(row) for row in _cache_rows]
    rows = _scan_registrations()
    with _cache_lock:
        _cache_rows = [dict(row) for row in rows]
        _cache_at = now
    return rows


def invalidate_cache() -> None:
    global _cache_at, _cache_rows
    with _cache_lock:
        _cache_at = 0.0
        _cache_rows = []


def _resolve_clsid(clsid: str, registration: dict) -> tuple[str | None, str | None]:
    candidates = [
        (registration.get("source_root"), registration.get("source_classes"),
         registration.get("source_view", 0)),
    ]
    candidates.extend((root, classes, view) for _hive, _scope, root, classes, view in _sources())
    seen = set()
    for root, classes, view in candidates:
        if root is None or not classes:
            continue
        marker = (str(root), classes, view)
        if marker in seen:
            continue
        seen.add(marker)
        class_key = f"{classes}\\CLSID\\{clsid}"
        _name_ok, class_name, _name_type = _query(root, class_key, "", view)
        server_ok, server, _server_type = _query(root, class_key + r"\InprocServer32", "", view)
        if not server_ok:
            server_ok, server, _server_type = _query(
                root, class_key + r"\LocalServer32", "", view)
        if class_name or server_ok:
            return (str(class_name).strip() if isinstance(class_name, str) else None,
                    _server_target(server) if server_ok else None)
    return None, None


def _action_history() -> dict[str, dict]:
    actions: dict[str, dict] = {}
    try:
        operations = manifest.list_operations()
    except Exception:
        return actions
    for op in operations:
        if op.get("action") != "context_menu_disable":
            continue
        item_id = op.get("context_menu_id")
        if item_id:
            actions.setdefault(item_id, op)
    return actions


def _marker_specs(item: dict) -> list[tuple[str, str]]:
    if item["kind"] == "handler":
        return [(BLOCKED_SUBKEY, item["handler_clsid"])]
    relative_keys = item.get("relative_keys") or [item["relative_key"]]
    return [(rf"Software\Classes\{relative_key}", "LegacyDisable")
            for relative_key in relative_keys]


def _marker_spec(item: dict) -> tuple[str, str]:
    """兼容单登记调用方；新操作统一使用 `_marker_specs`。"""
    return _marker_specs(item)[0]


def _key_exists(subkey: str) -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ):
            return True
    except OSError:
        return False


def _read_marker(subkey: str, name: str) -> tuple[bool, object, int | None]:
    if winreg is None:
        return False, None, None
    return _query(winreg.HKEY_CURRENT_USER, subkey, name)


def _write_marker(subkey: str, name: str, value: str, value_type: int) -> None:
    if winreg is None:
        raise ContextMenuActionError("当前系统不支持修改 Windows 右键菜单")
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ | winreg.KEY_WRITE,
    ) as key:
        try:
            winreg.QueryValueEx(key, name)
        except OSError:
            pass
        else:
            raise ContextMenuActionError("禁用标记已存在，请刷新后重试")
        winreg.SetValueEx(key, name, 0, value_type, value)


def _delete_marker(subkey: str, name: str) -> None:
    if winreg is None:
        raise ContextMenuActionError("当前系统不支持修改 Windows 右键菜单")
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ | winreg.KEY_WRITE,
    ) as key:
        winreg.DeleteValue(key, name)


def _delete_key_if_empty(subkey: str) -> None:
    if winreg is None:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ) as key:
            children, values, _stamp = winreg.QueryInfoKey(key)
        if children == 0 and values == 0:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
    except OSError:
        pass


def _write_custom_key(
    subkey: str,
    *,
    name: str,
    command_id: str,
    executable: str,
    command: str,
) -> None:
    if winreg is None:
        raise ContextMenuActionError("当前系统不支持创建 Windows 右键菜单")
    if _key_exists(subkey):
        raise ContextMenuActionError("待创建的注册表路径已经存在")
    string_type = getattr(winreg, "REG_SZ", 1)
    dword_type = getattr(winreg, "REG_DWORD", 4)
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ | winreg.KEY_WRITE,
    ) as key:
        winreg.SetValueEx(key, "", 0, string_type, name)
        winreg.SetValueEx(key, "MUIVerb", 0, string_type, name)
        winreg.SetValueEx(key, "Icon", 0, string_type, executable + ",0")
        winreg.SetValueEx(key, "LostPathManaged", 0, dword_type, 1)
        winreg.SetValueEx(key, "LostPathCommandId", 0, string_type, command_id)
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, subkey + r"\command", 0,
        winreg.KEY_READ | winreg.KEY_WRITE,
    ) as key:
        winreg.SetValueEx(key, "", 0, string_type, command)


def _custom_key_matches(subkey: str, command_id: str, command: str) -> bool:
    if winreg is None:
        return False
    managed = _query(winreg.HKEY_CURRENT_USER, subkey, "LostPathManaged")
    identifier = _query(winreg.HKEY_CURRENT_USER, subkey, "LostPathCommandId")
    current_command = _query(winreg.HKEY_CURRENT_USER, subkey + r"\command", "")
    return (
        managed[0] and managed[1] == 1
        and identifier[0] and identifier[1] == command_id
        and current_command[0] and current_command[1] == command
    )


def _snapshot_user_tree(subkey: str) -> dict:
    if winreg is None:
        raise ContextMenuActionError("当前系统不支持读取 Windows 右键菜单")
    return registry_health._snapshot_key(winreg.HKEY_CURRENT_USER, subkey)


def _delete_user_tree(subkey: str) -> None:
    if winreg is None:
        raise ContextMenuActionError("当前系统不支持修改 Windows 右键菜单")
    try:
        registry_health._delete_tree(winreg.HKEY_CURRENT_USER, subkey)
    except Exception as exc:
        raise ContextMenuActionError(f"删除自定义右键菜单失败：{exc}") from exc


def _restore_user_tree(subkey: str, tree: dict) -> None:
    if winreg is None:
        raise ContextMenuActionError("当前系统不支持修改 Windows 右键菜单")
    registry_health._restore_key(winreg.HKEY_CURRENT_USER, subkey, tree)


def _validate_create(name: str, executable: str, surface_ids: list[str]) -> tuple[str, list[str]]:
    label = (name or "").strip()
    if not label or len(label) > 80 or any(ord(char) < 32 for char in label):
        raise ContextMenuActionError("菜单名称必须是 1 到 80 个可见字符")
    expanded = os.path.expandvars((executable or "").strip().strip('"'))
    if '"' in expanded or not os.path.isabs(expanded) or not expanded.lower().endswith(".exe"):
        raise ContextMenuActionError("请选择本机绝对路径下的 exe 程序")
    expanded = os.path.abspath(expanded)
    if not os.path.isfile(expanded):
        raise ContextMenuActionError("程序文件不存在，请重新选择")
    surfaces = list(dict.fromkeys(surface_ids or []))
    if not surfaces or any(surface not in CREATABLE_SURFACE_IDS for surface in surfaces):
        raise ContextMenuActionError("请选择至少一个支持的右键菜单位置")
    return label, surfaces


def _manage_state(item: dict, actions: dict[str, dict]) -> dict:
    marker_specs = _marker_specs(item)
    source_states = item.get("source_disabled_states") or [
        bool(item.get("source_disabled")) for _spec in marker_specs
    ]
    disabled_states = []
    for index, (marker_key, marker_name) in enumerate(marker_specs):
        marker_exists, _value, _value_type = _read_marker(marker_key, marker_name)
        source_disabled = source_states[index] if index < len(source_states) else False
        disabled_states.append(bool(source_disabled) or marker_exists)
    disabled = bool(disabled_states) and all(disabled_states)
    partially_disabled = any(disabled_states) and not disabled
    action = actions.get(item["id"])
    if disabled and action and action.get("status") in {"done", "planned", "failed"}:
        interrupted = action.get("status") != "done"
        return {
            "disabled": True, "external": False, "can_disable": False,
            "can_restore": True, "action_id": action.get("id"),
            "can_delete": bool(item.get("custom")),
            "reason": ("上次操作中断，但禁用标记仍可安全恢复" if interrupted else
                       "已由 LostPath 禁用，可随时恢复"),
        }
    if disabled:
        return {
            "disabled": True, "external": True, "can_disable": False,
            "can_restore": False, "action_id": None,
            "can_delete": bool(item.get("custom")),
            "reason": "已由 Windows 或其它工具禁用，LostPath 不会接管其恢复",
        }
    if partially_disabled:
        return {
            "disabled": False, "external": True, "can_disable": False,
            "can_restore": False, "action_id": None,
            "can_delete": bool(item.get("custom")),
            "reason": "同一菜单的部分位置已由其它工具禁用，请先统一外部状态",
        }
    if item.get("system_component"):
        return {
            "disabled": False, "external": False, "can_disable": False,
            "can_restore": False, "action_id": None,
            "can_delete": False,
            "reason": "Windows 核心菜单保持只读",
        }
    return {
        "disabled": False, "external": False, "can_disable": True,
        "can_restore": False, "action_id": None,
        "can_delete": bool(item.get("custom")),
        "reason": "仅写入当前用户级禁用标记，不删除原注册表内容",
    }


def build_items(rows: list[dict], entities: list[dict] | None = None) -> list[dict]:
    """把原始登记合并成用户可理解的菜单项，处理 HKCR 的用户级优先规则。"""
    entities = entities or []
    actions = _action_history()
    items: list[dict] = []

    command_rows: dict[str, dict] = {}
    for row in sorted(
        (entry for entry in rows if entry.get("kind") == "command"),
        key=lambda entry: entry.get("source_rank", 9),
    ):
        locator = str(row.get("relative_key") or "").casefold()
        if locator:
            command_rows.setdefault(locator, row)
    custom_groups: dict[str, list[dict]] = {}
    for row in command_rows.values():
        if row.get("custom") and row.get("custom_id"):
            custom_groups.setdefault(str(row["custom_id"]), []).append(row)

    handled_custom: set[str] = set()
    for row in command_rows.values():
        custom_id = str(row.get("custom_id") or "")
        if row.get("custom") and custom_id:
            if custom_id in handled_custom:
                continue
            handled_custom.add(custom_id)
            group = custom_groups[custom_id]
        else:
            group = [row]
        representative = group[0]
        item_id = _item_id("custom" if custom_id else "command", custom_id or row["relative_key"])
        explorer_handler = str(representative.get("handler_clsid") or "").strip()
        handler_name = None
        handler_target = None
        if _GUID_RE.match(explorer_handler):
            handler_name, handler_target = _resolve_clsid(explorer_handler, representative)
        target = representative.get("target") or handler_target
        unresolved_system_command = (
            representative.get("scope") == "system" and not target
        )
        item = {
            "id": item_id,
            "kind": "command",
            "name": representative.get("name") or representative.get("verb") or "未命名命令",
            "provider": handler_name,
            "scope": representative.get("scope"),
            "hive": representative.get("hive"),
            "surfaces": [
                {"id": entry.get("surface_id"), "name": entry.get("surface_name")}
                for entry in group
            ],
            "registry_paths": [entry.get("registry_path") for entry in group],
            "target": target,
            "command": representative.get("command"),
            "commands": [entry.get("command") for entry in group],
            "relative_key": representative.get("relative_key"),
            "relative_keys": [entry.get("relative_key") for entry in group],
            "source_disabled": all(bool(entry.get("source_disabled")) for entry in group),
            "source_disabled_states": [bool(entry.get("source_disabled")) for entry in group],
            "handler_clsid": explorer_handler or None,
            "custom": bool(custom_id),
            "custom_id": custom_id or None,
            "subkey": representative.get("subkey"),
            "subkeys": [entry.get("subkey") for entry in group],
            "custom_entries": [
                {
                    "subkey": entry.get("subkey"),
                    "command": entry.get("command"),
                    "surface_id": entry.get("surface_id"),
                    "surface_name": entry.get("surface_name"),
                }
                for entry in group
            ],
            "system_component": (
                str(representative.get("verb") or "").casefold() in PROTECTED_VERBS
                or _is_windows_component(target)
                or unresolved_system_command
            ) if not custom_id else False,
            "entity": match_path_entity(target, entities),
        }
        item["manage"] = _manage_state(item, actions)
        items.append(item)

    handler_groups: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("kind") == "handler" and row.get("handler_clsid"):
            handler_groups.setdefault(str(row["handler_clsid"]).casefold(), []).append(row)
    for group in handler_groups.values():
        group.sort(key=lambda entry: entry.get("source_rank", 9))
        representative = group[0]
        clsid = str(representative["handler_clsid"]).upper()
        class_name, target = _resolve_clsid(clsid, representative)
        names = [entry.get("name") for entry in group]
        name = _display_name(class_name, *names, fallback=f"扩展处理器 {clsid[:9]}…")
        scopes = {str(entry.get("scope")) for entry in group}
        item = {
            "id": _item_id("handler", clsid),
            "kind": "handler",
            "name": name,
            "provider": class_name,
            "scope": scopes.pop() if len(scopes) == 1 else "mixed",
            "hive": representative.get("hive"),
            "surfaces": [
                {"id": surface_id, "name": surface_name}
                for surface_id, surface_name in dict.fromkeys(
                    (entry.get("surface_id"), entry.get("surface_name")) for entry in group
                )
            ],
            "registry_paths": list(dict.fromkeys(entry.get("registry_path") for entry in group)),
            "target": target,
            "relative_key": None,
            "relative_keys": [],
            "source_disabled": False,
            "source_disabled_states": [False],
            "handler_clsid": clsid,
            "custom": False,
            "custom_id": None,
            "subkey": None,
            "subkeys": [],
            "commands": [],
            "custom_entries": [],
            "system_component": _is_windows_component(target),
            "entity": match_path_entity(target, entities),
        }
        item["manage"] = _manage_state(item, actions)
        items.append(item)

    return sorted(items, key=lambda item: (
        item["manage"]["disabled"], item["system_component"],
        0 if item.get("entity") else 1, str(item["name"]).casefold(),
    ))


def _public_item(item: dict) -> dict:
    return {key: value for key, value in item.items() if key not in {
        "relative_key", "relative_keys", "source_disabled", "source_disabled_states",
        "handler_clsid", "custom_id", "subkey", "subkeys", "command", "commands",
        "custom_entries",
    }}


def _removed_custom_commands() -> list[dict]:
    removed = []
    for op in manifest.list_operations():
        if op.get("action") != "context_menu_delete" or op.get("status") != "done":
            continue
        removed.append({
            "operation_id": op.get("id"),
            "name": op.get("context_menu_name") or "未命名自定义菜单",
            "surface": op.get("context_menu_surface"),
            "created_at": op.get("created_at"),
            "can_restore": True,
        })
    return removed


def report(entities: list[dict] | None = None, *, force: bool = False) -> dict:
    items = build_items(_registrations(force), entities)
    public = [_public_item(item) for item in items]
    return {
        "items": public,
        "removed": _removed_custom_commands(),
        "summary": {
            "total": len(public),
            "commands": sum(item["kind"] == "command" for item in public),
            "handlers": sum(item["kind"] == "handler" for item in public),
            "active": sum(not item["manage"]["disabled"] for item in public),
            "disabled": sum(item["manage"]["disabled"] for item in public),
            "associated": sum(bool(item.get("entity")) for item in public),
            "manageable": sum(item["manage"]["can_disable"] for item in public),
            "protected": sum(item["system_component"] for item in public),
            "custom": sum(bool(item.get("custom")) for item in public),
        },
        "read_at": _now(),
    }


def _find_item(item_id: str) -> dict:
    item = next((entry for entry in build_items(_registrations(True))
                 if entry.get("id") == item_id), None)
    if not item:
        raise ContextMenuActionError("右键菜单项已不存在，请刷新后重试")
    return item


def create_custom(
    name: str,
    executable: str,
    surface_ids: list[str],
    dry_run: bool = True,
) -> dict:
    """创建当前用户级右键命令，参数占位符按出现位置自动生成。"""
    label, surfaces = _validate_create(name, executable, surface_ids)
    executable = os.path.abspath(os.path.expandvars(executable.strip().strip('"')))
    command_id = uuid.uuid4().hex[:16]
    verb = f"LostPath.{command_id}"
    entries = []
    for surface_id in surfaces:
        surface_name, relative = SURFACE_BY_ID[surface_id]
        subkey = rf"Software\Classes\{relative}\shell\{verb}"
        argument = SURFACE_ARGUMENT[surface_id]
        command = f'"{executable}" "{argument}"'
        entries.append({
            "surface_id": surface_id,
            "surface_name": surface_name,
            "subkey": subkey,
            "command": command,
        })
    if any(_key_exists(entry["subkey"]) for entry in entries):
        raise ContextMenuActionError("自定义菜单注册表路径发生冲突，请重试")

    op = manifest.new_operation("context_menu_create", {
        "path": f"context-menu:create:{command_id}",
        "name": label,
        "reason": "创建当前用户级自定义右键命令",
    })
    op.update({
        "context_menu_name": label,
        "context_menu_custom_id": command_id,
        "context_menu_surfaces": [entry["surface_name"] for entry in entries],
        "context_menu_executable": executable,
        "context_menu_created_keys": entries,
        "rollback_supported": True,
    })
    if dry_run:
        op["status"] = "dry_run"
        return manifest.public_operation(op)

    created: list[dict] = []
    manifest.save(op)
    try:
        for entry in entries:
            _write_custom_key(
                entry["subkey"], name=label, command_id=command_id,
                executable=executable, command=entry["command"],
            )
            created.append(entry)
            if not _custom_key_matches(entry["subkey"], command_id, entry["command"]):
                raise ContextMenuActionError(
                    f"{entry['surface_name']} 的自定义菜单写入后校验失败")
        op["steps_done"].append({"step": "context_menu_commands_created", "at": _now()})
        manifest.mark(op, "done")
    except Exception as exc:
        for entry in reversed(created):
            try:
                if _key_exists(entry["subkey"]):
                    _delete_user_tree(entry["subkey"])
            except Exception:
                pass
        try:
            manifest.mark(op, "failed", failure=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        if isinstance(exc, ContextMenuActionError):
            raise
        raise ContextMenuActionError(f"创建自定义右键菜单失败：{exc}") from exc
    invalidate_cache()
    return manifest.public_operation(op)


def remove_custom(item_id: str, dry_run: bool = True) -> dict:
    """删除一条 LostPath 创建的菜单，整键备份后才执行。"""
    item = _find_item(item_id)
    command_id = str(item.get("custom_id") or "")
    entries = item.get("custom_entries") or [{
        "subkey": item.get("subkey"),
        "command": item.get("command"),
        "surface_name": (item.get("surfaces") or [{}])[0].get("name"),
    }]
    if not item.get("custom") or item.get("hive") != "HKCU" or not entries:
        raise ContextMenuActionError("只能删除 LostPath 创建的当前用户菜单")
    for entry in entries:
        if (not command_id or not entry.get("subkey") or not entry.get("command")
                or not _custom_key_matches(
                    entry["subkey"], command_id, entry["command"])):
            raise ContextMenuActionError("自定义菜单已被其它程序修改，请刷新后重试")
    surface = "、".join(str(entry.get("surface_name") or "右键菜单") for entry in entries)
    op = manifest.new_operation("context_menu_delete", {
        "path": f"context-menu:delete:{command_id}",
        "name": item.get("name"),
        "reason": "删除 LostPath 创建的自定义右键命令",
    })
    op.update({
        "context_menu_name": item.get("name"),
        "context_menu_surface": surface,
        "context_menu_custom_id": command_id,
        "context_menu_deleted_key": entries[0]["subkey"],
        "context_menu_deleted_command": entries[0]["command"],
        "rollback_supported": True,
    })
    if dry_run:
        op["status"] = "dry_run"
        return manifest.public_operation(op)

    deleted_entries = [{**entry, "backup": _snapshot_user_tree(entry["subkey"])}
                       for entry in entries]
    op["context_menu_deleted_entries"] = deleted_entries
    op["context_menu_backup"] = deleted_entries[0]["backup"]
    manifest.save(op)
    deleted: list[dict] = []
    try:
        for entry in deleted_entries:
            if not _custom_key_matches(entry["subkey"], command_id, entry["command"]):
                raise ContextMenuActionError("自定义菜单状态已变化，请刷新后重试")
            _delete_user_tree(entry["subkey"])
            deleted.append(entry)
            if _key_exists(entry["subkey"]):
                raise ContextMenuActionError("删除后自定义菜单注册表键仍然存在")
        op["steps_done"].append({"step": "context_menu_custom_deleted", "at": _now()})
        manifest.mark(op, "done")
    except Exception as exc:
        for entry in deleted:
            try:
                if not _key_exists(entry["subkey"]):
                    _restore_user_tree(entry["subkey"], entry["backup"])
            except Exception:
                pass
        try:
            manifest.mark(op, "failed", failure=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        if isinstance(exc, ContextMenuActionError):
            raise
        raise ContextMenuActionError(f"删除自定义右键菜单失败：{exc}") from exc
    invalidate_cache()
    return manifest.public_operation(op)


def disable(item_id: str, dry_run: bool = True) -> dict:
    item = _find_item(item_id)
    actions = _action_history()
    manage = _manage_state(item, actions)
    if not manage["can_disable"]:
        raise ContextMenuActionError(manage["reason"])
    marker_specs = _marker_specs(item)
    marker_type = getattr(winreg, "REG_SZ", 1) if winreg is not None else 1
    markers = [{
        "key": marker_key,
        "name": marker_name,
        "value": "",
        "type": marker_type,
        "created_key": not _key_exists(marker_key),
    } for marker_key, marker_name in marker_specs]
    marker_key, marker_name = marker_specs[0]
    op = manifest.new_operation("context_menu_disable", {
        "path": f"context-menu:{item_id}",
        "name": item.get("name"),
        "reason": manage["reason"],
    })
    op.update({
        "context_menu_id": item_id,
        "context_menu_name": item.get("name"),
        "context_menu_kind": item.get("kind"),
        "context_menu_marker_key": marker_key,
        "context_menu_marker_name": marker_name,
        "context_menu_marker_value": "",
        "context_menu_marker_type": marker_type,
        "context_menu_created_key": not _key_exists(marker_key),
        "context_menu_markers": markers,
        "rollback_supported": True,
    })
    if dry_run:
        op["status"] = "dry_run"
        return manifest.public_operation(op)

    written: list[dict] = []
    manifest.save(op)
    try:
        for marker in markers:
            exists, _value, _value_type = _read_marker(marker["key"], marker["name"])
            if exists:
                raise ContextMenuActionError("右键菜单状态已变化，请刷新后重试")
            _write_marker(marker["key"], marker["name"], marker["value"], marker["type"])
            written.append(marker)
            exists, value, value_type = _read_marker(marker["key"], marker["name"])
            if not exists or value != marker["value"] or value_type != marker["type"]:
                raise ContextMenuActionError("禁用标记写入后校验失败")
        op["steps_done"].append({"step": "context_menu_marker_written", "at": _now()})
        manifest.mark(op, "done")
    except Exception as exc:
        for marker in reversed(written):
            try:
                _delete_marker(marker["key"], marker["name"])
                if marker.get("created_key"):
                    _delete_key_if_empty(marker["key"])
            except Exception:
                pass
        try:
            manifest.mark(op, "failed", failure=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        if isinstance(exc, ContextMenuActionError):
            raise
        raise ContextMenuActionError(f"禁用右键菜单失败：{exc}") from exc
    invalidate_cache()
    return manifest.public_operation(op)


def _rollback_created(op: dict) -> dict:
    if op.get("status") == "rolled_back":
        raise ContextMenuActionError("该自定义菜单已经撤销")
    if op.get("status") not in {"done", "planned", "failed"}:
        raise ContextMenuActionError(f"状态为 {op.get('status')} 的操作不能撤销")
    entries = op.get("context_menu_created_keys") or []
    command_id = str(op.get("context_menu_custom_id") or "")
    executable = str(op.get("context_menu_executable") or "")
    label = str(op.get("context_menu_name") or "")
    if not entries or not command_id or not executable or not label:
        raise ContextMenuActionError("操作记录缺少自定义菜单恢复信息")
    existing = []
    for entry in entries:
        subkey = str(entry.get("subkey") or "")
        command = str(entry.get("command") or "")
        if not subkey or not command:
            raise ContextMenuActionError("操作记录中的自定义菜单信息不完整")
        if _key_exists(subkey):
            if not _custom_key_matches(subkey, command_id, command):
                raise ContextMenuActionError("自定义菜单已被其它程序修改，拒绝删除")
            existing.append(entry)

    op["restore_requested_at"] = _now()
    manifest.save(op)
    deleted = []
    try:
        for entry in existing:
            _delete_user_tree(entry["subkey"])
            deleted.append(entry)
        op["steps_done"].append({"step": "context_menu_creation_rolled_back", "at": _now()})
        manifest.mark(op, "rolled_back")
    except Exception as exc:
        for entry in deleted:
            try:
                if not _key_exists(entry["subkey"]):
                    _write_custom_key(
                        entry["subkey"], name=label, command_id=command_id,
                        executable=executable, command=entry["command"],
                    )
            except Exception:
                pass
        if isinstance(exc, ContextMenuActionError):
            raise
        raise ContextMenuActionError(f"撤销自定义右键菜单失败：{exc}") from exc
    invalidate_cache()
    return manifest.public_operation(op)


def _restore_deleted(op: dict) -> dict:
    if op.get("status") == "rolled_back":
        raise ContextMenuActionError("该自定义菜单已经恢复")
    if op.get("status") not in {"done", "planned", "failed"}:
        raise ContextMenuActionError(f"状态为 {op.get('status')} 的删除操作不能恢复")
    command_id = str(op.get("context_menu_custom_id") or "")
    entries = op.get("context_menu_deleted_entries") or [{
        "subkey": op.get("context_menu_deleted_key"),
        "command": op.get("context_menu_deleted_command"),
        "backup": op.get("context_menu_backup"),
    }]
    if (not command_id or not entries or any(
        not entry.get("subkey") or not entry.get("command")
        or not isinstance(entry.get("backup"), dict)
        for entry in entries
    )):
        raise ContextMenuActionError("操作记录缺少自定义菜单备份")
    missing = []
    for entry in entries:
        if _key_exists(entry["subkey"]):
            if not _custom_key_matches(entry["subkey"], command_id, entry["command"]):
                raise ContextMenuActionError("原注册表路径已被其它程序占用，拒绝覆盖")
        else:
            missing.append(entry)

    op["restore_requested_at"] = _now()
    manifest.save(op)
    restored: list[dict] = []
    try:
        for entry in missing:
            _restore_user_tree(entry["subkey"], entry["backup"])
            restored.append(entry)
        if any(not _custom_key_matches(entry["subkey"], command_id, entry["command"])
               for entry in entries):
            raise ContextMenuActionError("恢复后自定义菜单校验失败")
        op["steps_done"].append({"step": "context_menu_custom_restored", "at": _now()})
        manifest.mark(op, "rolled_back")
    except Exception as exc:
        for entry in restored:
            try:
                _delete_user_tree(entry["subkey"])
            except Exception:
                pass
        if isinstance(exc, ContextMenuActionError):
            raise
        raise ContextMenuActionError(f"恢复自定义右键菜单失败：{exc}") from exc
    invalidate_cache()
    return manifest.public_operation(op)


def recovery_state(op: dict) -> tuple[bool, str]:
    action = op.get("action")
    if action not in {
        "context_menu_disable", "context_menu_create", "context_menu_delete",
    }:
        return False, "不是右键菜单操作"
    if op.get("status") not in {"done", "planned", "failed"}:
        return False, f"状态为 {op.get('status')}，无需恢复"
    if action == "context_menu_create":
        entries = op.get("context_menu_created_keys") or []
        command_id = str(op.get("context_menu_custom_id") or "")
        existing = [entry for entry in entries if entry.get("subkey")
                    and _key_exists(entry["subkey"])]
        if not existing:
            return False, "未发现仍存在的自定义菜单键"
        if any(not _custom_key_matches(
            entry["subkey"], command_id, str(entry.get("command") or ""),
        ) for entry in existing):
            return False, "自定义菜单已被其它程序修改"
        return True, "检测到本次创建的菜单键，可以撤销"
    if action == "context_menu_delete":
        entries = op.get("context_menu_deleted_entries") or []
        if not entries or any(not isinstance(entry.get("backup"), dict) for entry in entries):
            return False, "自定义菜单备份不完整"
        command_id = str(op.get("context_menu_custom_id") or "")
        for entry in entries:
            if (_key_exists(entry["subkey"])
                    and not _custom_key_matches(
                        entry["subkey"], command_id, entry["command"]
                    )):
                return False, "原注册表路径已被其它程序占用"
        if all(_key_exists(entry["subkey"]) for entry in entries):
            return False, "菜单键已经完整存在，无需恢复"
        return True, "检测到缺失的菜单键且备份完整，可以恢复"

    markers = op.get("context_menu_markers") or [{
        "key": op.get("context_menu_marker_key"),
        "name": op.get("context_menu_marker_name"),
        "value": op.get("context_menu_marker_value"),
        "type": op.get("context_menu_marker_type"),
    }]
    existing = []
    for marker in markers:
        if not marker.get("key") or not marker.get("name"):
            return False, "操作记录缺少禁用标记信息"
        found, value, value_type = _read_marker(marker["key"], marker["name"])
        if found and (value != marker.get("value") or value_type != marker.get("type")):
            return False, "禁用标记已被其它程序修改"
        if found:
            existing.append(marker)
    if not existing:
        return False, "禁用标记已经不存在，无需恢复"
    return True, "检测到 LostPath 写入的禁用标记，可以恢复"


def restore(operation_id: str) -> dict:
    op = manifest.find(operation_id)
    if not op:
        raise ContextMenuActionError("找不到可恢复的右键菜单操作")
    if op.get("action") == "context_menu_create":
        return _rollback_created(op)
    if op.get("action") == "context_menu_delete":
        return _restore_deleted(op)
    if op.get("action") != "context_menu_disable":
        raise ContextMenuActionError("找不到可恢复的右键菜单操作")
    if op.get("status") == "rolled_back":
        raise ContextMenuActionError("该右键菜单已经恢复")
    if op.get("status") not in {"done", "planned", "failed"}:
        raise ContextMenuActionError(f"状态为 {op.get('status')} 的操作不能恢复")
    markers = op.get("context_menu_markers") or [{
        "key": op.get("context_menu_marker_key"),
        "name": op.get("context_menu_marker_name"),
        "value": op.get("context_menu_marker_value"),
        "type": op.get("context_menu_marker_type"),
        "created_key": op.get("context_menu_created_key"),
    }]
    if not markers or any(
        not marker.get("key") or not marker.get("name")
        or not isinstance(marker.get("type"), int)
        for marker in markers
    ):
        raise ContextMenuActionError("操作记录缺少恢复信息")
    existing_markers = []
    for marker in markers:
        exists, value, value_type = _read_marker(marker["key"], marker["name"])
        if exists and (value != marker.get("value") or value_type != marker.get("type")):
            raise ContextMenuActionError("禁用标记已被其它程序修改，拒绝覆盖")
        if exists:
            existing_markers.append(marker)

    op["restore_requested_at"] = _now()
    manifest.save(op)
    deleted: list[dict] = []
    try:
        for marker in existing_markers:
            _delete_marker(marker["key"], marker["name"])
            deleted.append(marker)
            if _read_marker(marker["key"], marker["name"])[0]:
                raise ContextMenuActionError("恢复后禁用标记仍然存在")
        op["steps_done"].append({"step": "context_menu_marker_removed", "at": _now()})
        manifest.mark(op, "rolled_back")
        for marker in markers:
            if marker.get("created_key"):
                _delete_key_if_empty(marker["key"])
    except Exception as exc:
        for marker in deleted:
            try:
                _write_marker(
                    marker["key"], marker["name"], marker.get("value"), marker["type"])
            except Exception:
                pass
        if isinstance(exc, ContextMenuActionError):
            raise
        raise ContextMenuActionError(f"恢复右键菜单失败：{exc}") from exc
    invalidate_cache()
    return manifest.public_operation(op)


__all__ = [
    "ContextMenuActionError", "build_items", "create_custom", "disable",
    "invalidate_cache", "recovery_state", "remove_custom", "report", "restore",
]
