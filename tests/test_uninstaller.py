import os

import pytest

from lostpath.act import executor, manifest, registry_health, uninstall_audit, uninstaller


def _entry(command=r'"G:\Demo\uninstall.exe" /remove'):
    return {
        "id": "app-1",
        "name": "Demo App",
        "version": "1.0",
        "publisher": "Demo",
        "scope": "user",
        "hive": "HKCU",
        "subkey": r"Software\Uninstall\Demo",
        "location": r"G:\Demo",
        "location_exists": True,
        "estimated_size": 1024,
        "uninstall_string": command,
        "uninstaller_exists": True,
        "system_component": False,
    }


def test_launch_uses_vendor_uninstaller_without_exposing_command(monkeypatch):
    item = _entry()
    monkeypatch.setattr(registry_health, "find_entry", lambda _id: item)
    seen = {}

    class Process:
        pid = 4321

    def popen(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return Process()

    monkeypatch.setattr(uninstaller.subprocess, "Popen", popen)
    public = uninstaller.launch(item["id"], dry_run=False)

    assert seen["command"] == item["uninstall_string"]
    assert seen["executable"] == r"G:\Demo\uninstall.exe"
    assert seen["shell"] is False
    assert "uninstall_command" not in public
    saved = manifest.find(public["id"])
    assert saved["uninstall_command"] == item["uninstall_string"]
    assert saved["rollback_supported"] is False
    assert manifest.pending_rollback() == []
    with pytest.raises(executor.ExecutionRefused, match="不支持自动回滚"):
        executor.rollback(public["id"])


def test_msi_install_switch_is_changed_to_uninstall(monkeypatch):
    item = _entry("MsiExec.exe /I{12345678-1234-1234-1234-123456789ABC}")
    monkeypatch.setattr(registry_health, "find_entry", lambda _id: item)
    seen = []
    monkeypatch.setattr(uninstaller.subprocess, "Popen",
                        lambda command, **_kwargs: seen.append(command) or type("P", (), {"pid": 7})())
    uninstaller.launch(item["id"], dry_run=False)
    assert "/X{" in seen[0]
    assert "/I{" not in seen[0]


def test_unquoted_program_files_command_uses_explicit_executable(monkeypatch):
    item = _entry(r"G:\Demo Folder\uninstall.exe /remove")
    monkeypatch.setattr(registry_health, "find_entry", lambda _id: item)
    seen = {}
    monkeypatch.setattr(
        uninstaller.subprocess, "Popen",
        lambda command, **kwargs: seen.update(command=command, **kwargs)
        or type("P", (), {"pid": 8})(),
    )

    uninstaller.launch(item["id"], dry_run=False)

    assert seen["command"] == item["uninstall_string"]
    assert seen["executable"] == r"G:\Demo Folder\uninstall.exe"


def test_verify_marks_removed_registration(monkeypatch):
    item = _entry()
    monkeypatch.setattr(registry_health, "find_entry", lambda _id: item)
    monkeypatch.setattr(uninstaller.subprocess, "Popen",
                        lambda *_args, **_kwargs: type("P", (), {"pid": 8})())
    op = uninstaller.launch(item["id"], dry_run=False)
    monkeypatch.setattr(registry_health, "find_entry", lambda _id: None)
    result = uninstaller.verify(op["id"])
    assert result["verified_removed"] is True
    assert manifest.find(op["id"])["uninstall_verified_at"]


def test_missing_uninstaller_is_refused_before_manifest(monkeypatch):
    item = {**_entry(), "uninstaller_exists": False}
    monkeypatch.setattr(registry_health, "find_entry", lambda _id: item)
    with pytest.raises(uninstaller.UninstallActionError, match="不存在"):
        uninstaller.launch(item["id"], dry_run=False)
    assert manifest.list_operations() == []


def test_launch_persists_private_deep_uninstall_baseline(monkeypatch):
    item = _entry()
    baseline = {"entity": {"id": "r:demo:demoapp", "name": "Demo App"}}
    monkeypatch.setattr(registry_health, "find_entry", lambda _id: item)
    monkeypatch.setattr(uninstall_audit, "capture_baseline",
                        lambda _entry, _entities: baseline)
    monkeypatch.setattr(uninstaller.subprocess, "Popen",
                        lambda *_args, **_kwargs: type("P", (), {"pid": 9})())

    public = uninstaller.launch(item["id"], dry_run=False, entities=[{"id": "entity"}])
    saved = manifest.find(public["id"])

    assert saved["uninstall_baseline"] == baseline
    assert saved["uninstall_baseline_captured"] is True
    assert "uninstall_baseline" not in public
