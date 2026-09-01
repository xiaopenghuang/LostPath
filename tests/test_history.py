"""历史快照与用户规则的回归。"""
from lostpath.act import planner, rules
from lostpath.storage import paths, snapshots


def test_archive_keeps_two_scans_created_in_one_second():
    snapshots.save_latest([{"path": "C:\\one", "size": 1}])
    first = snapshots.archive_latest()
    second = snapshots.archive_latest()
    assert first and second and first != second
    assert first.read_bytes() == second.read_bytes()


def test_history_reports_delta_and_directory_changes():
    snapshots.save_latest([
        {"path": "C:\\cache", "name": "cache", "size": 100},
        {"path": "C:\\old", "name": "old", "size": 80},
    ])
    snapshots.archive_latest()
    snapshots.save_latest([
        {"path": "C:\\cache", "name": "cache", "size": 180},
        {"path": "C:\\new", "name": "new", "size": 40},
    ])

    report = snapshots.history_report()
    assert report["current"]["total_size"] == 220
    assert report["previous"]["total_size"] == 180
    assert report["delta"]["bytes"] == 40
    assert report["gainers"][0]["path"] == "C:\\cache"
    assert report["gainers"][0]["delta"] == 80
    assert report["shrinkers"][0]["path"] == "C:\\old"
    assert report["history_count"] == 1


def test_history_skips_corrupt_archive():
    paths.ensure_dirs()
    (paths.snapshots_dir() / "broken.json").write_text("not json", encoding="utf-8")
    snapshots.save_latest([{"path": "C:\\x", "size": 1}])
    report = snapshots.history_report()
    assert report["history_count"] == 0
    assert report["previous"] is None


def test_ignore_rule_matches_path_and_descendants():
    entry = rules.add_ignored(r"C:\Users\dev\Cache", "用户保留")
    assert entry["path"] == r"C:\Users\dev\Cache"
    assert rules.ignored_rule(r"c:\users\DEV\cache") is not None
    assert rules.ignored_rule(r"C:\Users\dev\Cache\nested") is not None
    assert rules.ignored_rule(r"C:\Users\dev\Cache2") is None


def test_ignore_rule_blocks_plan_without_touching_files():
    rules.add_ignored(r"C:\Cache")
    plan = planner.plan_for({
        "path": r"C:\Cache", "size": 200 * 1024 * 1024,
        "owner": "Demo", "owner_kind": "app", "cat": "可清理", "conf": 0.99,
    })
    assert plan.action == "none"
    assert [b.code for b in plan.blockers] == ["user_ignored"]


def test_remove_ignore_rule_is_idempotent():
    rules.add_ignored(r"C:\Cache")
    assert rules.remove_ignored(r"c:\cache") is True
    assert rules.remove_ignored(r"C:\Cache") is False
