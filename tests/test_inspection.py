import json

from lostpath import inspection, residues
from lostpath.storage import paths, snapshots


def test_inspection_config_roundtrip_and_due_state():
    assert inspection.load_config()["enabled"] is False
    inspection.save_config(True, 6)
    cfg = inspection.status()
    assert cfg["enabled"] is True
    assert cfg["interval_hours"] == 6
    assert cfg["due"] is True

    snapshots.save_latest([], scan_stats={"total_dirs": 1})
    cfg = inspection.status()
    assert cfg["due"] is False
    assert cfg["last_scanned_at"]


def test_inspection_rejects_unsupported_interval():
    try:
        inspection.save_config(True, 7)
    except ValueError as e:
        assert "6、12、24 或 72" in str(e)
    else:
        raise AssertionError("不支持的巡检间隔没有被拒绝")


def test_corrupt_inspection_config_falls_back_to_disabled():
    paths.inspection_config().parent.mkdir(parents=True, exist_ok=True)
    paths.inspection_config().write_text("{bad", encoding="utf-8")
    assert inspection.load_config() == {"enabled": False, "interval_hours": 24}


def test_detect_residue_requires_missing_current_install():
    data = {
        "snapshot": {"scanned_at": "2026-08-31T00:00:00+00:00"},
        "software": [{"name": "Still Installed", "source": "registry"}],
        "items": [
            {"path": r"C:\Still", "owner": "Still Installed", "owner_kind": "app", "size": 90_000_000},
            {"path": r"C:\Removed", "owner": "Removed App", "owner_kind": "app", "size": 80_000_000},
            {"path": r"C:\Tool", "owner": "Removed Tool", "owner_kind": "toolchain", "size": 90_000_000},
        ],
    }
    report = residues.detect(data, min_size=1, check_exists=False)
    assert report["summary"] == {"count": 1, "total_size": 80_000_000}
    assert report["candidates"][0]["owner"] == "Removed App"
    assert report["candidates"][0]["evidence"][0]["source"] == "registry_absence"


def test_detect_residue_ignores_missing_directories(tmp_path):
    existing = tmp_path / "removed"
    existing.mkdir()
    data = {
        "software": [],
        "items": [
            {"path": str(existing), "owner": "Removed", "owner_kind": "app", "size": 60_000_000},
            {"path": str(tmp_path / "gone"), "owner": "Gone", "owner_kind": "app", "size": 60_000_000},
        ],
    }
    report = residues.detect(data)
    assert [x["owner"] for x in report["candidates"]] == ["Removed"]
