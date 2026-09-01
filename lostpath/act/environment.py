"""Windows 环境变量清单与当前用户变量管理。

系统级变量只读。当前用户变量的修改先写 manifest，再写注册表；每次操作都带原值与
注册表类型，可在操作历史中恢复。疑似凭据的值不进入 API，但仍可替换或删除。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from lostpath.software_identity import relate_environment_variable

from . import envvar, manifest

try:
    import winreg
except ImportError:  # pragma: no cover - Windows 应用，非 Windows 仅供静态分析
    winreg = None


USER_KEY = envvar.ENV_KEY
MACHINE_KEY = envvar.MACHINE_ENV_KEY
STRING_TYPES = {
    getattr(winreg, "REG_SZ", 1),
    getattr(winreg, "REG_EXPAND_SZ", 2),
}
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.()\-]{0,254}$")
SENSITIVE_RE = re.compile(
    r"(?:^|[_\-.])(token|secret|password|passwd|credential|private[_-]?key|"
    r"api[_-]?key|access[_-]?key|client[_-]?secret|connection[_-]?string|"
    r"database[_-]?url|cookie|session|auth|pat)(?:$|[_\-.])", re.I)


class EnvironmentActionError(RuntimeError):
    """环境变量操作被拒绝或写入失败。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(name: str, value: str | None, value_type: int | None) -> str:
    raw = f"{name.casefold()}\0{value_type}\0{value if value is not None else '<missing>'}"
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:20]


def _is_sensitive(name: str) -> bool:
    return bool(SENSITIVE_RE.search(name))


def _read_scope(root, subkey: str, scope: str) -> list[dict]:
    if winreg is None:
        return []
    out = []
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
            value_count = winreg.QueryInfoKey(key)[1]
            for index in range(value_count):
                try:
                    name, value, value_type = winreg.EnumValue(key, index)
                except OSError:
                    continue
                if not isinstance(name, str) or not isinstance(value, str):
                    continue
                if value_type not in STRING_TYPES:
                    continue
                out.append({
                    "name": name,
                    "value": value,
                    "value_type": value_type,
                    "scope": scope,
                })
    except OSError:
        pass
    return out


def _read_user_record(name: str) -> tuple[str | None, int | None]:
    if winreg is None:
        return None, None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_KEY, 0,
                            winreg.KEY_READ) as key:
            value, value_type = winreg.QueryValueEx(key, name)
    except OSError:
        return None, None
    if not isinstance(value, str) or value_type not in STRING_TYPES:
        raise EnvironmentActionError(
            f"环境变量 {name} 使用了不支持的注册表类型，拒绝覆盖")
    return value, value_type


def _write_user(name: str, value: str, value_type: int) -> None:
    if winreg is None:
        raise EnvironmentActionError("当前平台不支持 Windows 注册表")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, USER_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, value_type, value)
    envvar.broadcast_change()


def _delete_user(name: str) -> None:
    if winreg is None:
        raise EnvironmentActionError("当前平台不支持 Windows 注册表")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError as exc:
        raise EnvironmentActionError(f"环境变量已经不存在：{name}") from exc
    except OSError as exc:
        raise EnvironmentActionError(f"删除环境变量失败：{exc}") from exc
    envvar.broadcast_change()


def _validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not NAME_RE.fullmatch(cleaned):
        raise EnvironmentActionError(
            "变量名需以字母或下划线开头，只能包含字母、数字、下划线、点、括号和连字符")
    return cleaned


def _validate_value(value: str) -> str:
    if not isinstance(value, str):
        raise EnvironmentActionError("变量值必须是文本")
    if "\x00" in value:
        raise EnvironmentActionError("变量值不能包含空字符")
    if len(value) > 32767:
        raise EnvironmentActionError("变量值超过 Windows 允许的长度")
    return value


def _public_item(row: dict, overridden: bool,
                 entities: list[dict] | None = None) -> dict:
    sensitive = _is_sensitive(row["name"])
    value = row["value"]
    relations = relate_environment_variable(
        row["name"], value, entities or [], sensitive=sensitive)
    return {
        "id": hashlib.sha1(
            f"{row['scope']}|{row['name'].casefold()}".encode()).hexdigest()[:16],
        "name": row["name"],
        "scope": row["scope"],
        "value": None if sensitive else value,
        "masked": sensitive,
        "preview": "已隐藏敏感值" if sensitive else value,
        "expandable": row["value_type"] == getattr(winreg, "REG_EXPAND_SZ", 2),
        "editable": row["scope"] == "user",
        "overridden": overridden,
        "fingerprint": _fingerprint(row["name"], value, row["value_type"]),
        "relations": relations,
    }


def report(entities: list[dict] | None = None) -> dict:
    """返回用户级与系统级环境变量，敏感值只返回占位。"""
    changes = [manifest.public_operation(op) for op in manifest.list_operations()
               if op.get("action") in {"env_set", "env_delete"}]
    if winreg is None:
        return {"items": [], "summary": {"total": 0, "user": 0, "machine": 0,
                                           "masked": 0, "overrides": 0,
                                           "associated": 0, "software": 0},
                "changes": changes, "read_at": _now()}
    user = _read_scope(winreg.HKEY_CURRENT_USER, USER_KEY, "user")
    machine = _read_scope(winreg.HKEY_LOCAL_MACHINE, MACHINE_KEY, "machine")
    user_names = {row["name"].casefold() for row in user}
    items = [_public_item(row, False, entities) for row in user]
    items.extend(_public_item(row, row["name"].casefold() in user_names, entities)
                 for row in machine)
    items.sort(key=lambda row: (row["name"].casefold(), row["scope"] != "user"))
    return {
        "items": items,
        "summary": {
            "total": len(items),
            "user": len(user),
            "machine": len(machine),
            "masked": sum(1 for row in items if row["masked"]),
            "overrides": sum(1 for row in items if row["overridden"]),
            "associated": sum(1 for row in items if row["relations"]),
            "software": len({relation["entity_id"] for row in items
                              for relation in row["relations"]}),
        },
        "changes": changes,
        "read_at": _now(),
    }


def _new_operation(action: str, name: str, previous: str | None,
                   previous_type: int | None, new_value: str | None) -> dict:
    op = manifest.new_operation(action, {
        "path": f"HKCU\\Environment\\{name}",
        "env_var": name,
    })
    op.update({
        "env_var": name,
        "env_previous": previous,
        "env_previous_type": previous_type,
        "env_new": new_value,
        "rollback_supported": True,
    })
    return op


def set_value(name: str, value: str, expected_fingerprint: str | None,
              dry_run: bool = True) -> dict:
    """新增或修改当前用户变量。expected_fingerprint 防止覆盖页面读取后的外部改动。"""
    name = _validate_name(name)
    value = _validate_value(value)
    previous, previous_type = _read_user_record(name)
    actual = _fingerprint(name, previous, previous_type)
    if expected_fingerprint is None and previous is not None:
        raise EnvironmentActionError(f"环境变量已经存在：{name}，请刷新后再编辑")
    if expected_fingerprint is not None and expected_fingerprint != actual:
        raise EnvironmentActionError("环境变量已被其它程序修改，请刷新后重试")
    value_type = (previous_type if previous_type in STRING_TYPES else
                  getattr(winreg, "REG_EXPAND_SZ", 2) if "%" in value else
                  getattr(winreg, "REG_SZ", 1))
    op = _new_operation("env_set", name, previous, previous_type, value)
    op["env_new_type"] = value_type
    if dry_run:
        op["status"] = "dry_run"
        return manifest.public_operation(op)
    manifest.save(op)
    try:
        _write_user(name, value, value_type)
        current, current_type = _read_user_record(name)
        if (current, current_type) != (value, value_type):
            raise EnvironmentActionError("写入后读回不一致")
        manifest.add_step(op, f"env_set:{name}")
        manifest.mark(op, "done")
    except Exception as exc:
        manifest.mark(op, "failed", failure=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, EnvironmentActionError):
            raise
        raise EnvironmentActionError(f"写入环境变量失败：{exc}") from exc
    return manifest.public_operation(op)


def delete_value(name: str, expected_fingerprint: str,
                 dry_run: bool = True) -> dict:
    """删除当前用户变量。删除前必须带上刚读取到的指纹。"""
    name = _validate_name(name)
    previous, previous_type = _read_user_record(name)
    if previous is None:
        raise EnvironmentActionError(f"环境变量不存在：{name}")
    if expected_fingerprint != _fingerprint(name, previous, previous_type):
        raise EnvironmentActionError("环境变量已被其它程序修改，请刷新后重试")
    op = _new_operation("env_delete", name, previous, previous_type, None)
    if dry_run:
        op["status"] = "dry_run"
        return manifest.public_operation(op)
    manifest.save(op)
    try:
        _delete_user(name)
        if _read_user_record(name)[0] is not None:
            raise EnvironmentActionError("删除后变量仍然存在")
        manifest.add_step(op, f"env_deleted:{name}")
        manifest.mark(op, "done")
    except Exception as exc:
        manifest.mark(op, "failed", failure=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, EnvironmentActionError):
            raise
        raise EnvironmentActionError(f"删除环境变量失败：{exc}") from exc
    return manifest.public_operation(op)


def recovery_state(op: dict) -> tuple[bool, str]:
    """Check whether the post-operation value is still present and safe to undo."""
    if op.get("action") not in {"env_set", "env_delete"}:
        return False, "不是环境变量操作"
    if op.get("status") not in {"done", "planned", "failed"}:
        return False, f"状态为 {op.get('status')}，无需恢复"
    name = op.get("env_var")
    if not name:
        return False, "操作记录缺少变量名"
    current, _current_type = _read_user_record(name)
    if op["action"] == "env_set":
        if current != op.get("env_new"):
            return False, "变量已被其它程序修改，拒绝覆盖当前值"
        return True, "变量仍是本次写入的值，可恢复原值"
    if current is not None:
        return False, "同名变量已经重新出现，拒绝覆盖"
    return True, "变量仍处于删除状态，可恢复原值"


def restore(operation_id: str) -> dict:
    op = manifest.find(operation_id)
    if not op or op.get("action") not in {"env_set", "env_delete"}:
        raise EnvironmentActionError("找不到可恢复的环境变量操作")
    if op.get("status") == "rolled_back":
        raise EnvironmentActionError("该操作已经撤销")
    if op.get("status") not in {"done", "planned", "failed"}:
        raise EnvironmentActionError(f"状态为 {op.get('status')} 的操作不能撤销")
    name = op.get("env_var")
    current, _current_type = _read_user_record(name)
    if op["action"] == "env_set" and current != op.get("env_new"):
        raise EnvironmentActionError("变量在操作后又被修改，拒绝覆盖现在的值")
    if op["action"] == "env_delete" and current is not None:
        raise EnvironmentActionError("同名变量已经重新创建，拒绝覆盖现在的值")
    previous = op.get("env_previous")
    try:
        if previous is None:
            if current is not None:
                _delete_user(name)
        else:
            _write_user(name, previous, op.get("env_previous_type") or
                        getattr(winreg, "REG_EXPAND_SZ", 2))
        manifest.add_step(op, f"env_restored:{name}")
        manifest.mark(op, "rolled_back")
    except Exception as exc:
        raise EnvironmentActionError(f"恢复环境变量失败：{exc}") from exc
    return manifest.public_operation(op)


__all__ = [
    "EnvironmentActionError", "delete_value", "recovery_state", "report", "restore",
    "set_value",
]
