import pytest

from lostpath.act import context_menu, manifest


def _command_row(**changes):
    row = {
        "kind": "command",
        "name": "Open with Demo",
        "verb": "demo.open",
        "hive": "HKCU",
        "scope": "user",
        "surface_id": "files",
        "surface_name": "所有文件",
        "relative_key": r"*\shell\demo.open",
        "registry_path": r"HKCU\Software\Classes\*\shell\demo.open",
        "target": r"G:\Apps\Demo\demo.exe",
        "handler_clsid": None,
        "source_disabled": False,
        "source_rank": 0,
    }
    row.update(changes)
    return row


def _handler_row(surface_id="files", surface_name="所有文件", **changes):
    row = {
        "kind": "handler",
        "name": "DemoShell",
        "hive": "HKLM64",
        "scope": "system",
        "surface_id": surface_id,
        "surface_name": surface_name,
        "registry_path": rf"HKLM64\Software\Classes\{surface_id}\shellex\DemoShell",
        "handler_clsid": "{12345678-1234-1234-1234-123456789ABC}",
        "source_root": object(),
        "source_classes": r"Software\Classes",
        "source_view": 0,
        "source_rank": 1,
    }
    row.update(changes)
    return row


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(context_menu, "_read_marker", lambda _key, _name: (False, None, None))
    monkeypatch.setattr(context_menu, "_action_history", lambda: {})
    monkeypatch.setattr(context_menu, "_resolve_clsid", lambda _clsid, _row: (
        "Demo Shell Extension", r"G:\Apps\Demo\demo-shell.dll"))
    context_menu.invalidate_cache()


def test_user_registration_wins_over_machine_merged_view():
    machine = _command_row(
        name="Machine Demo", hive="HKLM64", scope="system", source_rank=1,
        registry_path=r"HKLM64\Software\Classes\*\shell\demo.open",
    )
    items = context_menu.build_items([machine, _command_row(name="User Demo")])

    assert len(items) == 1
    assert items[0]["name"] == "User Demo"
    assert items[0]["hive"] == "HKCU"


def test_handler_surfaces_are_merged_and_related_by_dll_path():
    entities = [{
        "id": "r:demo:demo", "name": "Demo", "publisher": "Demo",
        "location": r"G:\Apps\Demo", "exe_path": r"G:\Apps\Demo\demo.exe",
        "icon": "/icons/demo.png",
    }]
    rows = [
        _handler_row(),
        _handler_row("folder", "文件夹"),
        _handler_row("folder_background", "文件夹空白处"),
    ]

    item = context_menu.build_items(rows, entities)[0]

    assert item["kind"] == "handler"
    assert [surface["name"] for surface in item["surfaces"]] == [
        "所有文件", "文件夹", "文件夹空白处",
    ]
    assert item["entity"]["entity_id"] == "r:demo:demo"
    assert item["entity"]["confidence"] == 0.98


def test_custom_command_surfaces_are_grouped_as_one_logical_menu():
    rows = [
        _command_row(
            custom=True, custom_id="custom-1",
            subkey=r"Software\Classes\Directory\shell\LostPath.custom-1",
        ),
        _command_row(
            custom=True, custom_id="custom-1",
            surface_id="folder_background", surface_name="文件夹空白处",
            relative_key=r"Directory\Background\shell\LostPath.custom-1",
            registry_path=(r"HKCU\Software\Classes\Directory\Background\shell"
                           r"\LostPath.custom-1"),
            subkey=r"Software\Classes\Directory\Background\shell\LostPath.custom-1",
        ),
    ]

    items = context_menu.build_items(rows)

    assert len(items) == 1
    assert items[0]["custom"] is True
    assert [surface["name"] for surface in items[0]["surfaces"]] == [
        "所有文件", "文件夹空白处",
    ]


@pytest.mark.parametrize("verb,target", [
    ("open", r"G:\Apps\Demo\demo.exe"),
    ("demo.open", r"D:\Windows\System32\shell32.dll"),
])
def test_windows_core_entries_are_read_only(verb, target):
    item = context_menu.build_items([_command_row(verb=verb, target=target)])[0]

    assert item["system_component"] is True
    assert item["manage"]["can_disable"] is False
    assert item["manage"]["reason"] == "Windows 核心菜单保持只读"


def test_unresolved_machine_command_is_read_only():
    item = context_menu.build_items([_command_row(
        hive="HKLM64", scope="system", target=None, handler_clsid=None,
    )])[0]

    assert item["system_component"] is True
    assert item["manage"]["can_disable"] is False


def test_explorer_command_handler_uses_resolved_target(monkeypatch):
    clsid = "{12345678-1234-1234-1234-123456789ABC}"
    monkeypatch.setattr(
        context_menu, "_resolve_clsid",
        lambda _clsid, _row: ("Demo Handler", r"G:\Apps\Demo\shell.dll"),
    )
    entities = [{
        "id": "r:demo:demo", "name": "Demo", "publisher": "Demo",
        "location": r"G:\Apps\Demo", "exe_path": r"G:\Apps\Demo\demo.exe",
    }]

    item = context_menu.build_items([_command_row(
        hive="HKLM64", scope="system", target=None, handler_clsid=clsid,
    )], entities)[0]

    assert item["target"] == r"G:\Apps\Demo\shell.dll"
    assert item["provider"] == "Demo Handler"
    assert item["system_component"] is False
    assert item["entity"]["entity_id"] == "r:demo:demo"


def test_server_path_with_spaces_is_not_truncated():
    assert context_menu._server_target(
        r"C:\Program Files\Demo\shell-extension.dll"
    ) == r"C:\Program Files\Demo\shell-extension.dll"


def test_external_disabled_marker_is_not_claimed_for_restore(monkeypatch):
    monkeypatch.setattr(context_menu, "_read_marker", lambda _key, _name: (True, "", 1))
    item = context_menu.build_items([_command_row()])[0]

    assert item["manage"]["disabled"] is True
    assert item["manage"]["external"] is True
    assert item["manage"]["can_restore"] is False


def _action_item(kind="command"):
    if kind == "handler":
        return {
            "id": "handler-id", "kind": "handler", "name": "Demo Handler",
            "handler_clsid": "{12345678-1234-1234-1234-123456789ABC}",
            "relative_key": None, "source_disabled": False, "system_component": False,
        }
    return {
        "id": "command-id", "kind": "command", "name": "Demo Command",
        "handler_clsid": None, "relative_key": r"*\shell\demo.open",
        "source_disabled": False, "system_component": False,
    }


def _install_fake_registry(monkeypatch, item):
    values = {}
    keys = set()

    def read(subkey, name):
        stored = values.get((subkey, name))
        return (True, stored[0], stored[1]) if stored else (False, None, None)

    def write(subkey, name, value, value_type):
        saved = manifest.list_operations()
        assert saved and saved[0]["status"] == "planned"
        assert subkey in {
            marker["key"] for marker in saved[0].get("context_menu_markers", [])
        } or saved[0]["context_menu_marker_key"] == subkey
        keys.add(subkey)
        values[(subkey, name)] = (value, value_type)

    def delete(subkey, name):
        if (subkey, name) not in values:
            raise OSError("missing marker")
        del values[(subkey, name)]

    monkeypatch.setattr(context_menu, "_find_item", lambda _id: dict(item))
    monkeypatch.setattr(context_menu, "_action_history", lambda: {
        op["context_menu_id"]: op
        for op in manifest.list_operations()
        if op.get("action") == "context_menu_disable"
    })
    monkeypatch.setattr(context_menu, "_read_marker", read)
    monkeypatch.setattr(context_menu, "_write_marker", write)
    monkeypatch.setattr(context_menu, "_delete_marker", delete)
    monkeypatch.setattr(context_menu, "_key_exists", lambda key: key in keys)
    monkeypatch.setattr(context_menu, "_delete_key_if_empty", lambda key: keys.discard(key))
    return values, keys


@pytest.mark.parametrize("kind", ["command", "handler"])
def test_disable_writes_manifest_before_marker_and_restore_removes_only_marker(monkeypatch, kind):
    item = _action_item(kind)
    values, keys = _install_fake_registry(monkeypatch, item)

    preview = context_menu.disable(item["id"], dry_run=True)
    assert preview["status"] == "dry_run"
    assert values == {}
    assert manifest.list_operations() == []

    operation = context_menu.disable(item["id"], dry_run=False)
    marker_key = operation["context_menu_marker_key"]
    marker_name = operation["context_menu_marker_name"]
    assert operation["status"] == "done"
    assert values[(marker_key, marker_name)] == ("", operation["context_menu_marker_type"])
    assert "target" not in str(operation).casefold()

    restored = context_menu.restore(operation["id"])
    assert restored["status"] == "rolled_back"
    assert (marker_key, marker_name) not in values
    assert marker_key not in keys


def test_restore_refuses_marker_changed_by_other_program(monkeypatch):
    item = _action_item()
    values, _keys = _install_fake_registry(monkeypatch, item)
    operation = context_menu.disable(item["id"], dry_run=False)
    marker = (operation["context_menu_marker_key"], operation["context_menu_marker_name"])
    values[marker] = ("changed", operation["context_menu_marker_type"])

    with pytest.raises(context_menu.ContextMenuActionError, match="其它程序修改"):
        context_menu.restore(operation["id"])

    assert values[marker][0] == "changed"
    assert manifest.find(operation["id"])["status"] == "done"


def test_grouped_custom_menu_disables_and_restores_every_surface(monkeypatch):
    item = {
        **_action_item(),
        "custom": True,
        "relative_keys": [
            r"Directory\shell\LostPath.custom-1",
            r"Directory\Background\shell\LostPath.custom-1",
        ],
        "source_disabled_states": [False, False],
    }
    values, _keys = _install_fake_registry(monkeypatch, item)

    operation = context_menu.disable(item["id"], dry_run=False)
    saved = manifest.find(operation["id"])
    assert len(saved["context_menu_markers"]) == 2
    assert len(values) == 2

    context_menu.restore(operation["id"])
    assert values == {}


def test_interrupted_operation_with_live_marker_remains_restorable(monkeypatch):
    item = _action_item()
    values, _keys = _install_fake_registry(monkeypatch, item)
    marker_key, marker_name = context_menu._marker_spec(item)
    marker_type = getattr(context_menu.winreg, "REG_SZ", 1)
    operation = manifest.new_operation("context_menu_disable", {
        "path": f"context-menu:{item['id']}", "name": item["name"],
    })
    operation.update({
        "context_menu_id": item["id"],
        "context_menu_marker_key": marker_key,
        "context_menu_marker_name": marker_name,
        "context_menu_marker_value": "",
        "context_menu_marker_type": marker_type,
        "context_menu_created_key": False,
    })
    manifest.save(operation)
    values[(marker_key, marker_name)] = ("", marker_type)

    state = context_menu._manage_state(item, context_menu._action_history())
    assert state["can_restore"] is True
    assert "中断" in state["reason"]

    restored = context_menu.restore(operation["id"])
    assert restored["status"] == "rolled_back"
    assert (marker_key, marker_name) not in values


def test_report_counts_manageable_protected_disabled_and_associated(monkeypatch):
    rows = [
        _command_row(),
        _command_row(relative_key=r"Directory\shell\open", verb="open", name="Open"),
        _command_row(
            relative_key=r"Drive\shell\external", verb="external", name="External",
            source_disabled=True,
        ),
    ]
    monkeypatch.setattr(context_menu, "_registrations", lambda _force=False: rows)

    result = context_menu.report()

    assert result["summary"] == {
        "total": 3, "commands": 3, "handlers": 0, "active": 2, "disabled": 1,
        "associated": 0, "manageable": 1, "protected": 1, "custom": 0,
    }


def test_create_custom_menu_uses_surface_arguments_and_rolls_back(monkeypatch, tmp_path):
    executable = tmp_path / "Code.exe"
    executable.write_bytes(b"test")
    keys = {}

    monkeypatch.setattr(context_menu, "_key_exists", lambda subkey: subkey in keys)

    def write(subkey, *, name, command_id, executable, command):
        saved = manifest.list_operations()
        assert saved and saved[0]["status"] == "planned"
        keys[subkey] = {
            "name": name, "command_id": command_id,
            "executable": executable, "command": command,
        }

    monkeypatch.setattr(context_menu, "_write_custom_key", write)
    monkeypatch.setattr(
        context_menu, "_custom_key_matches",
        lambda subkey, command_id, command: (
            subkey in keys and keys[subkey]["command_id"] == command_id
            and keys[subkey]["command"] == command
        ),
    )
    monkeypatch.setattr(context_menu, "_delete_user_tree", lambda subkey: keys.pop(subkey))

    preview = context_menu.create_custom(
        "使用 VS Code 打开", str(executable), ["folder", "folder_background"], True)
    assert preview["status"] == "dry_run"
    assert keys == {}
    assert manifest.list_operations() == []

    operation = context_menu.create_custom(
        "使用 VS Code 打开", str(executable), ["folder", "folder_background"], False)
    assert operation["status"] == "done"
    assert len(keys) == 2
    assert sorted(value["command"].rsplit('"', 2)[1] for value in keys.values()) == ["%1", "%V"]
    assert "context_menu_created_keys" not in operation
    assert "context_menu_executable" not in operation

    restored = context_menu.restore(operation["id"])
    assert restored["status"] == "rolled_back"
    assert keys == {}


def test_create_rejects_missing_executable_and_unknown_surface(tmp_path):
    executable = tmp_path / "missing.exe"
    with pytest.raises(context_menu.ContextMenuActionError, match="不存在"):
        context_menu.create_custom("Demo", str(executable), ["folder"], False)

    executable.write_bytes(b"test")
    with pytest.raises(context_menu.ContextMenuActionError, match="支持的"):
        context_menu.create_custom("Demo", str(executable), ["registry_injection"], False)

    assert manifest.list_operations() == []


def test_multi_surface_create_failure_removes_completed_keys(monkeypatch, tmp_path):
    executable = tmp_path / "Code.exe"
    executable.write_bytes(b"test")
    keys = set()
    calls = {"count": 0}
    monkeypatch.setattr(context_menu, "_key_exists", lambda subkey: subkey in keys)

    def write(subkey, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated write failure")
        keys.add(subkey)

    monkeypatch.setattr(context_menu, "_write_custom_key", write)
    monkeypatch.setattr(context_menu, "_custom_key_matches", lambda subkey, *_args: subkey in keys)
    monkeypatch.setattr(context_menu, "_delete_user_tree", lambda subkey: keys.remove(subkey))

    with pytest.raises(context_menu.ContextMenuActionError, match="simulated write failure"):
        context_menu.create_custom(
            "使用 VS Code 打开", str(executable), ["folder", "folder_background"], False)

    assert keys == set()
    failed = manifest.list_operations()[0]
    assert failed["action"] == "context_menu_create"
    assert failed["status"] == "failed"


def test_delete_custom_menu_backs_up_and_restores(monkeypatch):
    item = {
        **_action_item(),
        "custom": True,
        "custom_id": "custom-1",
        "subkey": r"Software\Classes\Directory\shell\LostPath.custom-1",
        "command": r'"G:\Apps\Demo\demo.exe" "%1"',
        "hive": "HKCU",
        "surfaces": [{"id": "folder", "name": "文件夹"}],
    }
    state = {"exists": True}
    backup = {"values": [{"name": "LostPathManaged", "type": 4,
                           "data": {"encoding": "json", "value": 1}}], "children": []}
    monkeypatch.setattr(context_menu, "_find_item", lambda _id: dict(item))
    monkeypatch.setattr(context_menu, "_key_exists", lambda _subkey: state["exists"])
    monkeypatch.setattr(
        context_menu, "_custom_key_matches",
        lambda _subkey, _command_id, _command: state["exists"],
    )
    monkeypatch.setattr(context_menu, "_snapshot_user_tree", lambda _subkey: backup)

    def delete(_subkey):
        saved = manifest.list_operations()
        assert saved and saved[0]["context_menu_backup"] == backup
        state["exists"] = False

    monkeypatch.setattr(context_menu, "_delete_user_tree", delete)
    monkeypatch.setattr(
        context_menu, "_restore_user_tree",
        lambda _subkey, tree: state.update(exists=tree == backup),
    )

    preview = context_menu.remove_custom(item["id"], True)
    assert preview["status"] == "dry_run"
    assert state["exists"] is True

    operation = context_menu.remove_custom(item["id"], False)
    assert operation["status"] == "done"
    assert state["exists"] is False
    assert "context_menu_backup" not in operation
    assert context_menu._removed_custom_commands()[0]["operation_id"] == operation["id"]

    restored = context_menu.restore(operation["id"])
    assert restored["status"] == "rolled_back"
    assert state["exists"] is True
