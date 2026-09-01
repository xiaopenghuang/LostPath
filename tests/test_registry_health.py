import os

import pytest

from lostpath.act import manifest, registry_health


def _entry(**changes):
    base = {
        "id": "entry-1",
        "key_name": "Demo",
        "hive": "HKCU",
        "scope": "user",
        "subkey": r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Demo",
        "name": "Demo App",
        "version": "1.0",
        "publisher": "Demo",
        "install_location": r"Z:\missing\Demo",
        "uninstall_string": r'"Z:\missing\Demo\uninstall.exe"',
        "quiet_uninstall_string": None,
        "estimated_size": 1024,
        "system_component": False,
        "root": object(),
    }
    base.update(changes)
    return registry_health.classify(base)


def test_only_double_missing_user_entry_is_manageable(monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda _path: False)
    monkeypatch.setattr(os.path, "isfile", lambda _path: False)
    item = _entry()
    assert item["status"] == "orphaned"
    assert item["can_clean"] is True

    system = _entry(scope="system", hive="HKLM64")
    assert system["status"] == "orphaned"
    assert system["can_clean"] is False


def test_msi_uninstaller_prevents_false_orphan(monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda _path: False)
    item = _entry(uninstall_string="MsiExec.exe /I{12345678-1234-1234-1234-123456789ABC}")
    assert item["status"] == "location_missing"
    assert item["can_clean"] is False


def test_cleanup_backs_up_before_delete_and_restores(monkeypatch):
    item = {**_entry(), "status": "orphaned", "can_clean": True}
    state = {"exists": True, "deleted_after_backup": False, "restored": False}
    backup = {"values": [{"name": "DisplayName", "type": 1,
                           "data": {"encoding": "json", "value": "Demo App"}}],
              "children": []}
    monkeypatch.setattr(registry_health, "find_entry", lambda _id: item)
    monkeypatch.setattr(registry_health, "_snapshot_key", lambda _root, _key: backup)

    def delete(_root, _key):
        saved = manifest.list_operations()
        assert saved and saved[0]["registry_backup"] == backup
        state["deleted_after_backup"] = True
        state["exists"] = False

    monkeypatch.setattr(registry_health, "_delete_tree", delete)
    monkeypatch.setattr(registry_health, "_key_exists",
                        lambda _root, _key: state["exists"])

    def restore(_root, _key, tree):
        assert tree == backup
        state["restored"] = True
        state["exists"] = True

    monkeypatch.setattr(registry_health, "_restore_key", restore)

    public = registry_health.cleanup(item["id"], dry_run=False)
    assert state["deleted_after_backup"] is True
    assert "registry_backup" not in public
    registry_health.restore(public["id"])
    assert state["restored"] is True
    assert manifest.find(public["id"])["status"] == "rolled_back"


def test_planned_cleanup_can_restore_after_key_was_deleted(monkeypatch):
    item = {**_entry(), "status": "orphaned", "can_clean": True}
    state = {"exists": True}
    backup = {"values": [], "children": []}
    monkeypatch.setattr(registry_health, "find_entry", lambda _id: item)
    monkeypatch.setattr(registry_health, "_snapshot_key", lambda _root, _key: backup)
    monkeypatch.setattr(
        registry_health, "_delete_tree",
        lambda _root, _key: state.update(exists=False),
    )
    monkeypatch.setattr(
        registry_health, "_key_exists", lambda _root, _key: state["exists"])
    monkeypatch.setattr(
        registry_health, "_restore_key",
        lambda _root, _key, _tree: state.update(exists=True),
    )
    public = registry_health.cleanup(item["id"], dry_run=False)
    saved = manifest.find(public["id"])
    saved["status"] = "planned"
    manifest.save(saved)

    assert registry_health.recovery_state(saved)[0] is True
    registry_health.restore(public["id"])

    assert state["exists"] is True


def test_cleanup_refuses_non_orphan_without_writing(monkeypatch):
    item = {**_entry(), "status": "uninstaller_missing", "can_clean": False}
    monkeypatch.setattr(registry_health, "find_entry", lambda _id: item)
    with pytest.raises(registry_health.RegistryActionError, match="只允许"):
        registry_health.cleanup(item["id"], dry_run=False)
    assert manifest.list_operations() == []
