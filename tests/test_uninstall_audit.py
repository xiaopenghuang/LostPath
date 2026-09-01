import json

import pytest

from lostpath import startup
from lostpath.act import executor, manifest, uninstall_audit


def _reports(monkeypatch, *, environment_items=None, registry_items=None,
             startup_items=None):
    monkeypatch.setattr(
        uninstall_audit.environment, "report",
        lambda _entities: {"items": environment_items or []},
    )
    monkeypatch.setattr(
        uninstall_audit.registry_health, "report",
        lambda _entities: {"items": registry_items or []},
    )
    monkeypatch.setattr(
        startup, "report",
        lambda _entities: {"state": "ready", "items": startup_items or []},
    )


def _parent(path, *, category="可再生缓存"):
    op = manifest.new_operation("uninstall_launch", {
        "path": r"HKCU\Software\Uninstall\Demo",
        "name": "Demo App",
    })
    op.update({
        "status": "done",
        "rollback_supported": False,
        "uninstall_name": "Demo App",
        "uninstall_verified_at": "2026-09-01T00:00:00+00:00",
        "uninstall_baseline": {
            "entity": {
                "id": "r:demo:demoapp",
                "name": "Demo App",
                "publisher": "Demo",
                "source": "registry",
                "location": None,
                "estimated_size": None,
                "shared_vendor": False,
                "redirects": [],
                "traces": [{
                    "path": str(path),
                    "name": path.name,
                    "role": "Demo 缓存",
                    "cat": category,
                    "owner": "Demo App",
                    "size": 7,
                    "files": 1,
                    "conf": 0.98,
                }],
            },
            "environment": [],
            "registry": [],
            "startup": [],
        },
    })
    manifest.save(op)
    return op


def test_audit_finds_existing_trace_and_marks_only_cache_recommended(
    tmp_path, monkeypatch,
):
    cache = tmp_path / "demo-cache"
    cache.mkdir()
    (cache / "data.bin").write_bytes(b"content")
    parent = _parent(cache)
    _reports(monkeypatch)

    audit = uninstall_audit.build_audit(parent["id"], {"software": []})

    assert audit["summary"]["files"] == 1
    assert audit["summary"]["actionable"] == 1
    assert audit["summary"]["recommended"] == 1
    candidate = audit["candidates"][0]
    assert candidate["path"] == str(cache)
    assert candidate["action"] == "recycle_directory"


def test_cross_volume_install_residue_is_not_copied_into_app_recycle(monkeypatch):
    monkeypatch.setattr(uninstall_audit.os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(uninstall_audit, "_is_junction", lambda _path: False)
    monkeypatch.setattr(manifest, "recycle_dir", lambda: uninstall_audit.Path(r"C:\Recycle"))

    candidates = uninstall_audit._file_candidates({
        "location": r"G:\Large App",
        "estimated_size": 20 * 1024 ** 3,
        "shared_vendor": False,
        "traces": [],
    })

    assert len(candidates) == 1
    assert candidates[0]["can_clean"] is False
    assert candidates[0]["action"] == "manual"
    assert "跨盘" in candidates[0]["reason"]


def test_deep_cleanup_moves_to_recycle_and_child_operation_rolls_back(
    tmp_path, monkeypatch,
):
    cache = tmp_path / "demo-cache"
    cache.mkdir()
    (cache / "data.bin").write_bytes(b"content")
    parent = _parent(cache)
    _reports(monkeypatch)
    audit = uninstall_audit.build_audit(parent["id"], {"software": []})
    candidate_id = audit["candidates"][0]["id"]

    preview = uninstall_audit.execute_cleanup(
        parent["id"], [candidate_id], {"software": []}, dry_run=True)
    assert cache.is_dir()
    assert preview["succeeded"] == 1

    result = uninstall_audit.execute_cleanup(
        parent["id"], [candidate_id], {"software": []}, dry_run=False)
    assert not cache.exists()
    child = result["results"][0]["operation"]
    assert child["action"] == "uninstall_residue_cleanup"
    assert child["rollback_supported"] is True
    assert manifest.find(parent["id"])["deep_cleanup_runs"][0]["operation_ids"] == [child["id"]]

    executor.rollback(child["id"])
    assert (cache / "data.bin").read_bytes() == b"content"


def test_residue_manifest_knows_destination_before_directory_moves(
    tmp_path, monkeypatch,
):
    cache = tmp_path / "demo-cache"
    cache.mkdir()
    (cache / "data.bin").write_bytes(b"content")
    parent = _parent(cache)
    _reports(monkeypatch)
    candidate = uninstall_audit.build_audit(
        parent["id"], {"software": []})["candidates"][0]
    real_rename = executor.os.rename

    def inspect_manifest_before_move(source, destination):
        if str(source) == str(cache):
            child = next(
                op for op in manifest.list_operations()
                if op.get("action") == "uninstall_residue_cleanup"
            )
            assert child["source_path"] == str(cache)
            assert child["recycle_intent"] == str(destination)
            assert child["recycled_to"] is None
        return real_rename(source, destination)

    monkeypatch.setattr(executor.os, "rename", inspect_manifest_before_move)
    executor.execute_residue_recycle(candidate, parent["id"], dry_run=False)

    assert not cache.exists()


def test_environment_cleanup_refuses_shared_path_but_allows_exclusive_variable(
    tmp_path, monkeypatch,
):
    trace = tmp_path / "gone"
    parent = _parent(trace)
    relation = {
        "entity_id": "r:demo:demoapp",
        "name": "Demo App",
        "reason": "变量值指向该软件的安装目录",
        "confidence": 0.98,
    }
    _reports(monkeypatch, environment_items=[
        {
            "id": "path", "name": "PATH", "scope": "user", "masked": False,
            "fingerprint": "one", "relations": [relation],
        },
        {
            "id": "home", "name": "DEMO_HOME", "scope": "user", "masked": False,
            "fingerprint": "two", "relations": [relation],
        },
    ])

    audit = uninstall_audit.build_audit(parent["id"], {"software": []})
    by_name = {item["name"]: item for item in audit["candidates"]}

    assert by_name["PATH"]["can_clean"] is False
    assert by_name["DEMO_HOME"]["can_clean"] is True
    assert by_name["DEMO_HOME"]["action"] == "env_delete"


def test_environment_candidate_expires_if_value_changes_after_preview(
    tmp_path, monkeypatch,
):
    parent = _parent(tmp_path / "gone")
    relation = {
        "entity_id": "r:demo:demoapp",
        "name": "Demo App",
        "reason": "变量值指向该软件的安装目录",
        "confidence": 0.98,
    }
    item = {
        "id": "home", "name": "DEMO_HOME", "scope": "user", "masked": False,
        "fingerprint": "before", "relations": [relation],
    }
    _reports(monkeypatch, environment_items=[item])
    audit = uninstall_audit.build_audit(parent["id"], {"software": []})
    candidate_id = next(row["id"] for row in audit["candidates"]
                        if row["name"] == "DEMO_HOME")

    item["fingerprint"] = "after"

    with pytest.raises(uninstall_audit.UninstallAuditError, match="已经变化"):
        uninstall_audit.execute_cleanup(
            parent["id"], [candidate_id], {"software": []}, dry_run=False)


def test_capture_baseline_never_persists_environment_values(monkeypatch):
    entity = {
        "id": "r:demo:demoapp", "name": "Demo App", "publisher": "Demo",
        "source": "registry", "location": r"G:\Demo", "traces": [],
    }
    entry = {"name": "Demo App", "publisher": "Demo", "location": r"G:\Demo"}
    monkeypatch.setattr(
        uninstall_audit.environment, "report",
        lambda _entities: {"items": [{
            "id": "secret", "name": "DEMO_TOKEN", "scope": "user",
            "value": "top-secret", "preview": "top-secret",
            "relations": [{"entity_id": entity["id"]}],
        }]},
    )
    monkeypatch.setattr(
        uninstall_audit.registry_health, "report", lambda _entities: {"items": []})
    monkeypatch.setattr(
        startup, "wait_for_report",
        lambda _entities: {"state": "ready", "items": []},
    )

    baseline = uninstall_audit.capture_baseline(entry, [entity])

    assert baseline["environment"] == [
        {"id": "secret", "name": "DEMO_TOKEN", "scope": "user"},
    ]
    assert "top-secret" not in json.dumps(baseline)


def test_capture_baseline_waits_for_startup_snapshot(monkeypatch):
    entity = {
        "id": "r:demo:demoapp", "name": "Demo App", "publisher": "Demo",
        "source": "registry", "location": r"G:\Demo", "traces": [],
    }
    entry = {"name": "Demo App", "publisher": "Demo", "location": r"G:\Demo"}
    monkeypatch.setattr(
        uninstall_audit.environment, "report", lambda _entities: {"items": []})
    monkeypatch.setattr(
        uninstall_audit.registry_health, "report", lambda _entities: {"items": []})
    monkeypatch.setattr(
        startup, "request_scan",
        lambda _entities: pytest.fail("不能把 loading 的即时结果当作卸载前基线"),
    )
    monkeypatch.setattr(
        startup, "wait_for_report",
        lambda _entities: {"state": "ready", "items": [{
            "id": "startup-1", "kind": "startup", "owner_id": entity["id"],
        }]},
    )

    baseline = uninstall_audit.capture_baseline(entry, [entity])

    assert baseline["startup"] == [{"id": "startup-1", "kind": "startup"}]
    assert baseline["startup_state"] == "ready"


def test_public_operation_hides_uninstall_baseline():
    public = manifest.public_operation({
        "id": "op", "uninstall_baseline": {"environment": ["secret"]},
    })
    assert "uninstall_baseline" not in public
