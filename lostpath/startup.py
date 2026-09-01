"""Windows 启动项、服务与计划任务分析，以及当前用户启动项管理。

采集脚本已经导出这些系统集成点，但它们不属于软件台账本身。这里提供一个
按需、单实例的后台缓存，避免每次打开页面都阻塞 UI 或重复启动 PowerShell。
只返回执行文件和结构化状态，不返回登录启动项的完整 raw 命令，避免把参数里的
令牌、路径或其它敏感内容带进接口。只有 HKCU Run 项支持禁用和恢复；服务、计划
任务与机器级启动项保持只读。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import sysdirs
from .software_identity import entity_reference
from .act import manifest
from .scan.inventory_export import export_inventory

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows development environments
    winreg = None


USER_RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
USER_RUN_PREFIX = r"HKCU:\software\microsoft\windows\currentversion\run"
DISABLED_SUBKEY = r"Software\LostPath\DisabledStartup"


_lock = threading.Lock()
_state = {
    "state": "idle",  # idle/loading/ready/error
    "items": [],
    "scanned_at": None,
    "error": None,
}
_worker: threading.Thread | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.strip().strip('"')
    if not path:
        return None
    # PowerShell may return the NT device prefix for a task action.
    if path.lower().startswith("\\\\?\\"):
        path = path[4:]
    return path


def _exe_name(path: str | None) -> str:
    if not path:
        return ""
    return os.path.basename(path.replace("/", "\\")).lower()


def _path_key(path: str | None) -> str:
    return (path or "").replace("/", "\\").rstrip("\\").casefold()


def _is_user_run_key(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    key = value.strip().replace("/", "\\").rstrip("\\").casefold()
    return key == USER_RUN_PREFIX.casefold()


def _startup_actions() -> dict[str, dict]:
    """返回最近一次启动项操作，供报告标记已禁用项目。"""
    out: dict[str, dict] = {}
    try:
        operations = manifest.list_operations()
    except Exception:
        return out
    for op in operations:
        if op.get("action") != "startup_disable":
            continue
        item_id = op.get("startup_id")
        if item_id and item_id not in out:
            out[item_id] = op
    return out


def _manage_state(item: dict, actions: dict[str, dict] | None = None) -> dict:
    """给启动项附上可操作范围，不把服务或计划任务伪装成可管理。"""
    if item.get("kind") != "startup":
        return {
            "can_disable": False,
            "can_restore": False,
            "disabled": False,
            "action_id": None,
            "reason": "系统服务和计划任务暂不支持直接修改",
        }
    if not _is_user_run_key(item.get("detail")):
        return {
            "can_disable": False,
            "can_restore": False,
            "disabled": False,
            "action_id": None,
            "reason": "仅支持当前用户的登录启动项",
        }
    if actions is None:
        actions = _startup_actions()
    action = actions.get(item.get("id"))
    if action and action.get("status") == "done":
        return {
            "can_disable": False,
            "can_restore": True,
            "disabled": True,
            "action_id": action.get("id"),
            "reason": "已由 LostPath 禁用，可随时恢复",
        }
    return {
        "can_disable": True,
        "can_restore": False,
        "disabled": False,
        "action_id": None,
        "reason": "当前用户登录时运行，可安全记录并禁用",
    }


def _entity_for_exe(exe: str | None, entities: list[dict]) -> dict | None:
    """用已定位的本体目录关联启动项，匹配不到就保持未知。"""
    if not exe:
        return None
    exe_key = _path_key(exe)
    exe_name = _exe_name(exe)
    best: tuple[int, dict, str, float] | None = None
    for entity in entities:
        location = _path_key(entity.get("location"))
        main_exe = _path_key(entity.get("exe_path"))
        if main_exe and exe_key == main_exe:
            return entity_reference(
                entity, reason="执行文件与软件主程序一致", confidence=1.0)
        if location and (exe_key == location or exe_key.startswith(location + "\\")):
            score = len(location)
            if best is None or score > best[0]:
                best = (score, entity, "执行文件位于软件安装目录", 0.98)
        # 对 Windows 系统服务的 svchost.exe 不做名称猜测，避免把一百多个服务
        # 都错误挂到某个软件上。
        if exe_name and exe_name == _exe_name(entity.get("exe_path")) and exe_name != "svchost.exe":
            if best is None:
                best = (1, entity, "执行文件名与软件主程序一致", 0.78)
    if not best:
        return None
    return entity_reference(best[1], reason=best[2], confidence=best[3])


def _owner_for(exe: str | None, entities: list[dict]) -> str | None:
    relation = _entity_for_exe(exe, entities)
    return relation.get("name") if relation else None


def _risk(kind: str, exe: str | None, state: str | None, start: str | None) -> tuple[str, int, str]:
    """返回 (等级, 分数, 依据)。等级是提示，不是安全结论。"""
    score = 28 if kind == "startup" else 22 if kind == "service" else 24
    reasons: list[str] = []
    systemish = False
    lower = (exe or "").lower().replace("/", "\\")
    if not exe:
        score += 32
        reasons.append("没有解析到执行文件")
    elif any(part in lower for part in ("\\temp\\", "\\tmp\\", "\\downloads\\")):
        score += 28
        reasons.append("执行文件位于临时或下载目录")
    elif sysdirs.protected_system_dir(exe):
        score -= 12
        systemish = True
        reasons.append("执行文件位于常见系统/程序目录")
    else:
        reasons.append("来源目录需要用户自行确认")
    if kind == "service" and (start or "").lower() not in {"auto", "automatic"}:
        score -= 8
        reasons.append(f"启动类型为 {start or '未知'}")
    if state and state.lower() == "stopped":
        score -= 4
        reasons.append("当前未运行")
    score = max(0, min(100, score))
    level = "attention" if score >= 55 else "system" if systemish else "normal"
    return level, score, "；".join(reasons)


def _item(kind: str, name: str, exe: str | None, *, source: str,
          detail: str | None = None, state: str | None = None,
          start: str | None = None, task_path: str | None = None,
          entities: list[dict], actions: dict[str, dict] | None = None) -> dict:
    level, score, reason = _risk(kind, exe, state, start)
    identity = "|".join((kind, name, exe or "", detail or "", task_path or ""))
    relation = _entity_for_exe(exe, entities)
    item = {
        "id": hashlib.sha1(identity.encode("utf-8", "replace")).hexdigest()[:16],
        "kind": kind,
        "name": name or "未命名项",
        "exe": exe,
        "source": source,
        "detail": detail,
        "state": state,
        "start": start,
        "task_path": task_path,
        "owner": relation.get("name") if relation else None,
        "owner_id": relation.get("entity_id") if relation else None,
        "owner_icon": relation.get("icon") if relation else None,
        "owner_reason": relation.get("reason") if relation else None,
        "owner_confidence": relation.get("confidence") if relation else None,
        "risk": level,
        "risk_score": score,
        "risk_reason": reason,
    }
    item["manage"] = _manage_state(item, actions)
    return item


def normalize_inventory(raw: dict, entities: list[dict] | None = None) -> list[dict]:
    """将 export_inventory 的结果变成 UI 稳定消费的统一记录。"""
    entities = entities or []
    action_history = _startup_actions()
    items: list[dict] = []
    for row in raw.get("startup") or []:
        if not isinstance(row, dict):
            continue
        items.append(_item(
            "startup", str(row.get("name") or "未命名启动项"), _clean_path(row.get("exe")),
            source="登录启动", detail=str(row.get("key") or ""), entities=entities,
            actions=action_history,
        ))
    for row in raw.get("services") or []:
        if not isinstance(row, dict):
            continue
        items.append(_item(
            "service", str(row.get("display") or row.get("name") or "未命名服务"),
            _clean_path(row.get("exe")), source="系统服务",
            detail=str(row.get("name") or ""), state=str(row.get("state") or ""),
            start=str(row.get("start") or ""), entities=entities,
            actions=action_history,
        ))
    for row in raw.get("tasks") or []:
        if not isinstance(row, dict):
            continue
        task_actions = row.get("actions")
        exe = None
        if isinstance(task_actions, list):
            exe = next((_clean_path(x) for x in task_actions if _clean_path(x)), None)
        elif isinstance(task_actions, str):
            exe = _clean_path(task_actions)
        items.append(_item(
            "task", str(row.get("name") or "未命名任务"), exe,
            source="计划任务",
            detail=f"{len(task_actions)} 个动作" if isinstance(task_actions, list) else None,
            task_path=str(row.get("path") or ""), entities=entities,
            actions=action_history,
        ))
    # 同一条集成点偶尔会被 32/64 位视图各导出一次，ID 去重后保留最完整的一条。
    unique: dict[str, dict] = {}
    for item in items:
        unique.setdefault(item["id"], item)
    return sorted(unique.values(), key=lambda x: (-x["risk_score"], x["name"].casefold()))


def _summary(items: list[dict]) -> dict:
    return {
        "total": len(items),
        "startup": sum(1 for x in items if x["kind"] == "startup"),
        "services": sum(1 for x in items if x["kind"] == "service"),
        "tasks": sum(1 for x in items if x["kind"] == "task"),
        "attention": sum(1 for x in items if x["risk"] == "attention"),
        "unknown_exe": sum(1 for x in items if not x.get("exe")),
        "associated": sum(1 for x in items if x.get("owner_id")),
        "manageable": sum(1 for x in items if x.get("manage", {}).get("can_disable")),
        "disabled": sum(1 for x in items if x.get("manage", {}).get("disabled")),
    }


def status() -> dict:
    with _lock:
        return _status_unlocked()


def _status_unlocked() -> dict:
    actions = _startup_actions()
    items = []
    for original in _state["items"]:
        item = dict(original)
        item["manage"] = _manage_state(item, actions)
        items.append(item)
    return {
        "state": _state["state"],
        "items": items,
        "summary": _summary(items),
        "scanned_at": _state["scanned_at"],
        "error": _state["error"],
    }


def _run(entities: list[dict]) -> None:
    global _worker
    temp = Path(tempfile.gettempdir()) / f"lostpath_startup_{uuid.uuid4().hex}.json"
    try:
        raw = export_inventory(temp)
        items = normalize_inventory(raw, entities)
        items = _merge_disabled_items(items, entities)
    except Exception as exc:
        with _lock:
            _state.update(state="error", error=f"{type(exc).__name__}: {exc}")
    else:
        with _lock:
            _state.update(state="ready", items=items, scanned_at=_now(), error=None)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        with _lock:
            _worker = None


def request_scan(entities: list[dict] | None = None, force: bool = False) -> dict:
    """启动一次后台采集并返回当前状态。"""
    global _worker
    with _lock:
        # 以 worker 是否存在作为并发闸门。状态先翻成 ready/error、worker 后清理
        # 之间有一个很短的窗口，单看 state 会让强制刷新再开一条线程并覆盖引用。
        if _worker is not None:
            return {**_status_unlocked(), "queued": False}
        if not force and _state["state"] == "ready":
            return {**_status_unlocked(), "queued": False}
        _state.update(state="loading", error=None)
        _worker = threading.Thread(target=_run, args=(list(entities or []),),
                                    name="lostpath-startup", daemon=True)
        _worker.start()
        return {**_status_unlocked(), "queued": True}


def report(entities: list[dict] | None = None) -> dict:
    """首次访问自动触发，后续访问只读缓存。"""
    with _lock:
        state = _state["state"]
    if state == "idle":
        return request_scan(entities)
    return status()


def wait_for_report(entities: list[dict] | None = None, timeout: float = 10.0) -> dict:
    """Return a complete startup snapshot, waiting only for the shared background scan."""
    current = request_scan(entities)
    if current.get("state") in {"ready", "error"}:
        return current
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        time.sleep(0.05)
        current = status()
        if current.get("state") in {"ready", "error"}:
            return current
    return status()


def _merge_disabled_items(items: list[dict], entities: list[dict]) -> list[dict]:
    """把已禁用且不再出现在 Run 键里的项目保留在界面，确保恢复入口不消失。"""
    known = {item.get("id") for item in items}
    action_history = _startup_actions()
    for op in action_history.values():
        if op.get("status") != "done":
            continue
        item_id = op.get("startup_id")
        if not item_id or item_id in known:
            continue
        item = _item(
            "startup", str(op.get("startup_name") or "已禁用启动项"),
            _clean_path(op.get("startup_exe")), source="登录启动",
            detail=str(op.get("startup_key") or USER_RUN_PREFIX), entities=entities,
            actions=action_history,
        )
        if item["id"] != item_id:
            # 原命令可能已被用户手工改过，恢复仍以台账里的 ID 为准。
            item["id"] = item_id
        items.append(item)
        known.add(item_id)
    return sorted(items, key=lambda x: (-x["risk_score"], x["name"].casefold()))


class StartupActionError(RuntimeError):
    """启动项操作被拒绝或执行失败。"""


def _open_user_run():
    if winreg is None:
        raise StartupActionError("当前系统不支持修改 Windows 启动项")
    try:
        return winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, USER_RUN_SUBKEY, 0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        )
    except OSError as exc:
        raise StartupActionError(f"无法打开当前用户启动项：{exc}") from exc


def _open_disabled_store():
    if winreg is None:
        raise StartupActionError("当前系统不支持修改 Windows 启动项")
    try:
        return winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, DISABLED_SUBKEY, 0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        )
    except OSError as exc:
        raise StartupActionError(f"无法打开 LostPath 启动项备份区：{exc}") from exc


def _find_item(item_id: str) -> dict:
    with _lock:
        for item in _state["items"]:
            if item.get("id") == item_id:
                return dict(item)
    raise StartupActionError("启动项已不在当前清单中，请先刷新")


def disable(item_id: str, dry_run: bool = True) -> dict:
    """禁用当前用户 Run 启动项，默认只生成 dry-run 记录。"""
    item = _find_item(item_id)
    manage = _manage_state(item)
    if not manage["can_disable"]:
        raise StartupActionError(manage["reason"])
    key_path = str(item.get("detail") or "")
    op = manifest.new_operation("startup_disable", {
        "path": f"startup:{item_id}",
        "name": item.get("name"),
        "kind": "startup",
        "registry_key": key_path,
    })
    op.update({
        "startup_id": item_id,
        "startup_name": item.get("name"),
        "startup_exe": item.get("exe"),
        "startup_key": key_path,
        "startup_backup": op["id"],
        "startup_backup_key": rf"HKCU:\{DISABLED_SUBKEY}",
    })
    if dry_run:
        op["status"] = "dry_run"
        return op

    manifest.save(op)
    try:
        with _open_user_run() as key, _open_disabled_store() as backup_key:
            try:
                value, value_type = winreg.QueryValueEx(key, item["name"])
            except OSError as exc:
                raise StartupActionError(f"启动项已不存在：{item['name']}") from exc
            backup = op["startup_backup"]
            try:
                winreg.QueryValueEx(backup_key, backup)
            except OSError:
                pass
            else:
                raise StartupActionError(f"备份名称已存在：{backup}")
            winreg.SetValueEx(backup_key, backup, 0, value_type, value)
            original_deleted = False
            try:
                manifest.add_step(op, f"registry_backup:{backup}")
                winreg.DeleteValue(key, item["name"])
                original_deleted = True
                manifest.add_step(op, f"registry_disabled:{item['name']}")
                manifest.mark(op, "done")
            except Exception:
                # 禁用的任何一步没能完整记入台账，就补偿回操作前状态。
                if original_deleted:
                    winreg.SetValueEx(key, item["name"], 0, value_type, value)
                try:
                    winreg.DeleteValue(backup_key, backup)
                except OSError:
                    pass
                raise
    except StartupActionError as exc:
        manifest.mark(op, "failed", failure=str(exc))
        raise
    except Exception as exc:
        manifest.mark(op, "failed", failure=f"{type(exc).__name__}: {exc}")
        raise StartupActionError(str(exc)) from exc
    return op


def recovery_state(op: dict) -> tuple[bool, str]:
    if op.get("action") != "startup_disable":
        return False, "不是登录启动项操作"
    if op.get("status") not in {"done", "planned", "failed"}:
        return False, f"状态为 {op.get('status')}，无需恢复"
    key_path = str(op.get("startup_key") or "")
    name = str(op.get("startup_name") or "")
    backup = str(op.get("startup_backup") or "")
    if not _is_user_run_key(key_path) or not name or not backup:
        return False, "操作记录缺少启动项恢复信息"
    try:
        with _open_user_run() as key, _open_disabled_store() as backup_key:
            try:
                winreg.QueryValueEx(key, name)
            except OSError:
                pass
            else:
                return False, "原启动项已经存在，拒绝覆盖"
            try:
                winreg.QueryValueEx(backup_key, backup)
            except OSError:
                return False, "禁用备份不存在，不能自动恢复"
    except OSError as exc:
        return False, f"读取启动项恢复状态失败：{exc}"
    return True, "原启动项仍缺失且备份完整，可以恢复"


def restore(op_id: str) -> dict:
    """恢复一条由 LostPath 禁用的当前用户启动项。"""
    op = manifest.find(op_id)
    if not op or op.get("action") != "startup_disable":
        raise StartupActionError("找不到启动项操作记录")
    if op.get("status") == "rolled_back":
        raise StartupActionError("该启动项已经恢复")
    if op.get("status") not in {"done", "planned", "failed"}:
        raise StartupActionError(f"状态为 {op.get('status')} 的启动项不能恢复")
    key_path = str(op.get("startup_key") or "")
    if not _is_user_run_key(key_path):
        raise StartupActionError("操作记录不是当前用户启动项")
    name = str(op.get("startup_name") or "")
    backup = str(op.get("startup_backup") or "")
    if not name or not backup:
        raise StartupActionError("操作记录缺少恢复信息")

    try:
        with _open_user_run() as key, _open_disabled_store() as backup_key:
            try:
                winreg.QueryValueEx(key, name)
            except OSError:
                pass
            else:
                raise StartupActionError(f"原启动项已存在，拒绝覆盖：{name}")
            try:
                value, value_type = winreg.QueryValueEx(backup_key, backup)
            except OSError as exc:
                raise StartupActionError("找不到备份值，无法恢复") from exc
            # 恢复意图先落盘。此时原值仍不存在、备份仍完整，写盘失败不会改变系统。
            op["restore_requested_at"] = _now()
            manifest.save(op)
            winreg.SetValueEx(key, name, 0, value_type, value)
            try:
                manifest.add_step(op, f"registry_restored:{name}")
            except Exception:
                # 台账写不下来就撤销刚才的恢复，保持“原值缺失 + 备份完整”的可重试状态。
                winreg.DeleteValue(key, name)
                raise
            warning = None
            try:
                winreg.DeleteValue(backup_key, backup)
            except OSError as exc:
                # 原值已经完整恢复。备份清理失败只会多留一份副本，不能谎称恢复失败。
                warning = f"启动项已恢复，但备份值未能清理：{exc}"
            else:
                manifest.add_step(op, f"registry_backup_removed:{backup}")
        manifest.mark(op, "rolled_back", warning=warning)
    except StartupActionError:
        # 原启动项已存在或备份缺失时没有做任何修改，保留 done 才能排障后重试。
        raise
    except Exception as exc:
        raise StartupActionError(str(exc)) from exc
    return op
