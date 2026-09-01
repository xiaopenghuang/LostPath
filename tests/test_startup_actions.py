import pytest

import lostpath.startup as startup


class FakeKey:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_WRITE = 2

    def __init__(self, run_key, backup_key=None, fail_delete=False):
        self.run_key = run_key
        self.backup_key = backup_key or FakeKey()
        self.fail_delete = fail_delete

    def OpenKey(self, root, subkey, _reserved, _access):
        assert root is self.HKEY_CURRENT_USER
        assert subkey == startup.USER_RUN_SUBKEY
        return self.run_key

    def CreateKeyEx(self, root, subkey, _reserved, _access):
        assert root is self.HKEY_CURRENT_USER
        assert subkey == startup.DISABLED_SUBKEY
        return self.backup_key

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise OSError("value not found")
        return key.values[name]

    def SetValueEx(self, key, name, _reserved, value_type, value):
        key.values[name] = (value, value_type)

    def DeleteValue(self, key, name):
        if self.fail_delete and key is self.run_key:
            raise OSError("simulated registry failure")
        if name not in key.values:
            raise OSError("value not found")
        del key.values[name]


@pytest.fixture(autouse=True)
def clean_startup_state():
    with startup._lock:
        startup._state.update(state="idle", items=[], scanned_at=None, error=None)
        startup._worker = None
    yield
    with startup._lock:
        startup._state.update(state="idle", items=[], scanned_at=None, error=None)
        startup._worker = None


def _item():
    return startup.normalize_inventory({
        "startup": [{
            "name": "DemoEditor",
            "key": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
            "exe": r"G:\Apps\Demo Editor\demo.exe",
            "raw": r'"G:\Apps\Demo Editor\demo.exe" --token=secret',
        }],
    })[0]


def _install_item(item):
    with startup._lock:
        startup._state.update(state="ready", items=[item], scanned_at="now", error=None)


def test_disable_backups_registry_value_and_restore(monkeypatch):
    item = _item()
    _install_item(item)
    key = FakeKey({"DemoEditor": (r'"G:\Apps\Demo Editor\demo.exe" --token=secret', 1)})
    backup_key = FakeKey()
    monkeypatch.setattr(startup, "winreg", FakeWinreg(key, backup_key))

    op = startup.disable(item["id"], dry_run=False)

    assert op["status"] == "done"
    assert "DemoEditor" not in key.values
    backup = op["startup_backup"]
    assert backup not in key.values, "备份放在 Run 键里仍会被 Windows 执行"
    assert backup_key.values[backup][0].endswith("--token=secret")
    saved = startup.manifest.find(op["id"])
    assert saved["status"] == "done"
    assert "token" not in str(saved["plan"])

    restored = startup.restore(op["id"])
    assert restored["status"] == "rolled_back"
    assert key.values["DemoEditor"][0].endswith("--token=secret")
    assert backup not in backup_key.values


def test_hklm_startup_item_is_read_only(monkeypatch):
    raw = {"startup": [{
        "name": "MachineApp",
        "key": r"HKLM:\Software\Microsoft\Windows\CurrentVersion\Run",
        "exe": r"G:\Apps\machine.exe",
    }]}
    item = startup.normalize_inventory(raw)[0]
    _install_item(item)
    key = FakeKey({"MachineApp": (r"G:\Apps\machine.exe", 1)})
    monkeypatch.setattr(startup, "winreg", FakeWinreg(key))

    with pytest.raises(startup.StartupActionError, match="仅支持当前用户"):
        startup.disable(item["id"], dry_run=False)
    assert "MachineApp" in key.values


def test_registry_failure_is_recorded_after_backup(monkeypatch):
    item = _item()
    _install_item(item)
    key = FakeKey({"DemoEditor": (r"G:\Apps\Demo Editor\demo.exe", 1)})
    backup_key = FakeKey()
    monkeypatch.setattr(startup, "winreg", FakeWinreg(key, backup_key, fail_delete=True))

    with pytest.raises(startup.StartupActionError, match="simulated registry failure"):
        startup.disable(item["id"], dry_run=False)

    failed = [op for op in startup.manifest.list_operations()
              if op.get("action") == "startup_disable"]
    assert failed and failed[0]["status"] == "failed"
    assert failed[0]["startup_backup"] not in backup_key.values
    assert "DemoEditor" in key.values


def test_disabled_item_is_merged_back_into_report(monkeypatch):
    item = _item()
    _install_item(item)
    key = FakeKey({"DemoEditor": (r"G:\Apps\Demo Editor\demo.exe", 1)})
    monkeypatch.setattr(startup, "winreg", FakeWinreg(key))
    op = startup.disable(item["id"], dry_run=False)

    merged = startup._merge_disabled_items([], [])
    assert len(merged) == 1
    assert merged[0]["id"] == item["id"]
    assert merged[0]["manage"]["disabled"] is True
    assert merged[0]["manage"]["action_id"] == op["id"]


def test_restore_refuses_to_overwrite_a_new_same_name_value(monkeypatch):
    item = _item()
    _install_item(item)
    key = FakeKey({"DemoEditor": (r"G:\Apps\Demo Editor\demo.exe", 1)})
    backup_key = FakeKey()
    monkeypatch.setattr(startup, "winreg", FakeWinreg(key, backup_key))
    op = startup.disable(item["id"], dry_run=False)
    key.values["DemoEditor"] = (r"G:\New\replacement.exe", 1)

    with pytest.raises(startup.StartupActionError, match="拒绝覆盖"):
        startup.restore(op["id"])

    assert key.values["DemoEditor"][0] == r"G:\New\replacement.exe"
    assert op["startup_backup"] in backup_key.values
    assert startup.manifest.find(op["id"])["status"] == "done"


def test_planned_disable_can_restore_from_private_backup(monkeypatch):
    item = _item()
    _install_item(item)
    key = FakeKey({"DemoEditor": (r"G:\Apps\Demo Editor\demo.exe", 1)})
    backup_key = FakeKey()
    monkeypatch.setattr(startup, "winreg", FakeWinreg(key, backup_key))
    op = startup.disable(item["id"], dry_run=False)
    saved = startup.manifest.find(op["id"])
    saved["status"] = "planned"
    startup.manifest.save(saved)

    assert startup.recovery_state(saved)[0] is True
    startup.restore(op["id"])

    assert "DemoEditor" in key.values
    assert op["startup_backup"] not in backup_key.values
