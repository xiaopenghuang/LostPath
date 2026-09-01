import pytest

from lostpath.act import environment, manifest


def _install_fake_store(monkeypatch, initial):
    store = dict(initial)

    def read(name):
        return store.get(name, (None, None))

    def write(name, value, value_type):
        store[name] = (value, value_type)

    def delete(name):
        if name not in store:
            raise environment.EnvironmentActionError("missing")
        del store[name]

    monkeypatch.setattr(environment, "_read_user_record", read)
    monkeypatch.setattr(environment, "_write_user", write)
    monkeypatch.setattr(environment, "_delete_user", delete)
    return store


def test_set_records_original_value_and_can_restore(monkeypatch):
    store = _install_fake_store(monkeypatch, {"DEMO_HOME": (r"C:\old", 1)})
    fingerprint = environment._fingerprint("DEMO_HOME", r"C:\old", 1)

    public = environment.set_value(
        "DEMO_HOME", r"D:\new", fingerprint, dry_run=False)

    assert store["DEMO_HOME"] == (r"D:\new", 1)
    assert "env_previous" not in public
    assert "env_new" not in public
    saved = manifest.find(public["id"])
    assert saved["env_previous"] == r"C:\old"
    assert saved["env_new"] == r"D:\new"

    environment.restore(public["id"])
    assert store["DEMO_HOME"] == (r"C:\old", 1)
    assert manifest.find(public["id"])["status"] == "rolled_back"


def test_planned_set_can_restore_when_written_value_is_still_present(monkeypatch):
    store = _install_fake_store(monkeypatch, {"DEMO_HOME": (r"C:\old", 1)})
    fingerprint = environment._fingerprint("DEMO_HOME", r"C:\old", 1)
    public = environment.set_value(
        "DEMO_HOME", r"D:\new", fingerprint, dry_run=False)
    saved = manifest.find(public["id"])
    saved["status"] = "planned"
    manifest.save(saved)

    can_recover, _reason = environment.recovery_state(saved)
    assert can_recover is True
    environment.restore(public["id"])

    assert store["DEMO_HOME"] == (r"C:\old", 1)


def test_set_refuses_stale_fingerprint(monkeypatch):
    store = _install_fake_store(monkeypatch, {"DEMO_HOME": (r"C:\changed", 1)})

    with pytest.raises(environment.EnvironmentActionError, match="其它程序修改"):
        environment.set_value("DEMO_HOME", r"D:\new", "stale", dry_run=False)

    assert store["DEMO_HOME"] == (r"C:\changed", 1)
    assert manifest.list_operations() == []


def test_existing_unsupported_registry_type_is_never_overwritten(monkeypatch):
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeWinreg:
        HKEY_CURRENT_USER = object()
        KEY_READ = 1

        @staticmethod
        def OpenKey(*_args):
            return Key()

        @staticmethod
        def QueryValueEx(_key, _name):
            return ["unexpected", "value"], 7

    monkeypatch.setattr(environment, "winreg", FakeWinreg())

    with pytest.raises(environment.EnvironmentActionError, match="不支持的注册表类型"):
        environment.set_value("DEMO", "new", None, dry_run=False)

    assert manifest.list_operations() == []


def test_delete_requires_fingerprint_and_restore_refuses_overwrite(monkeypatch):
    store = _install_fake_store(monkeypatch, {"DEMO": ("old", 2)})
    fingerprint = environment._fingerprint("DEMO", "old", 2)
    op = environment.delete_value("DEMO", fingerprint, dry_run=False)
    assert "DEMO" not in store

    store["DEMO"] = ("created-by-other-program", 1)
    with pytest.raises(environment.EnvironmentActionError, match="拒绝覆盖"):
        environment.restore(op["id"])
    assert store["DEMO"][0] == "created-by-other-program"


def test_report_masks_secret_values(monkeypatch):
    def read_scope(_root, _subkey, scope):
        if scope == "user":
            return [
                {"name": "SERVICE_TOKEN", "value": "top-secret", "value_type": 1,
                 "scope": "user"},
                {"name": "TOOLS_HOME", "value": r"G:\Tools", "value_type": 1,
                 "scope": "user"},
            ]
        return []

    monkeypatch.setattr(environment, "_read_scope", read_scope)
    result = environment.report()
    secret = next(row for row in result["items"] if row["name"] == "SERVICE_TOKEN")
    assert secret["masked"] is True
    assert secret["value"] is None
    assert "top-secret" not in str(result)


@pytest.mark.parametrize("name", [
    "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "SERVICE_PAT",
    "DATABASE_URL", "CLIENT_SECRET",
])
def test_common_credential_names_are_sensitive(name):
    assert environment._is_sensitive(name) is True
