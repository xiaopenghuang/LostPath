"""快照读写回归。

重点守两条被文档反复强调、却最容易被"优化"掉的约束：
1. **缺快照是正常状态，不是错误**——用户首次启动、换新机器都没有快照，此时服务
   必须能起来并让 UI 引导扫描，而不是崩在启动阶段。
2. 写入必须原子，中断不得留下半个文件。
"""
import json

import pytest

from lostpath.storage import paths, snapshots


# --------------------------------------------- 缺快照 / 坏快照都不能抛
def test_missing_snapshot_is_normal_not_error():
    items, meta = snapshots.load_latest()
    assert items == []
    assert meta["present"] is False
    assert meta["reason"], "应给出原因供 UI 显示引导"


def test_corrupt_snapshot_degrades_instead_of_raising():
    paths.ensure_dirs()
    paths.latest_snapshot().write_text("{ 这不是 JSON", encoding="utf-8")
    items, meta = snapshots.load_latest()
    assert items == []
    assert meta["present"] is False
    assert "损坏" in meta["reason"]


@pytest.mark.parametrize("raw, fragment", [
    ('"valid json but not an object"', "顶层必须是对象"),
    (json.dumps({"schema_version": 3, "items": {"path": "C:\\x"}}),
     "items 必须是数组"),
    (json.dumps({"schema_version": 3, "items": [None]}),
     "items 包含无效条目"),
    (json.dumps({"schema_version": "3", "items": []}),
     "schema_version 必须是整数"),
])
def test_structurally_invalid_snapshot_degrades_without_startup_crash(raw, fragment):
    """JSON 可解析不等于快照可消费，结构错误也必须走正常降级。"""
    paths.ensure_dirs()
    paths.latest_snapshot().write_text(raw, encoding="utf-8")

    items, meta = snapshots.load_latest()

    assert items == []
    assert meta["present"] is False
    assert fragment in meta["reason"]


# ------------------------------------------------------- 信封与版本兼容
def test_save_then_load_roundtrip():
    recs = [{"path": "C:\\x", "size": 10, "owner": "甲"},
            {"path": "C:\\y", "size": 20, "owner": None}]
    snapshots.save_latest(recs, machine="TESTBOX",
                          scan_stats={"total_dirs": 5, "denied_count": 2})
    items, meta = snapshots.load_latest()
    assert items == recs
    assert meta["machine"] == "TESTBOX"
    assert meta["schema_version"] == snapshots.SCHEMA_VERSION
    assert meta["scanned_at"], "缺时间戳，UI 无法显示上次扫描时间"
    assert meta["scan_stats"]["total_dirs"] == 5


def test_scan_stats_optional_and_absent_is_none():
    """v1 快照没有 scan_stats，取不到必须是 None 而不是 KeyError。"""
    snapshots.save_latest([{"path": "C:\\x", "size": 1}])
    _items, meta = snapshots.load_latest()
    assert meta["scan_stats"] is None


def test_v1_envelope_still_readable():
    """老快照要能读。schema 升级不能让用户的既有数据变成不可用。"""
    paths.ensure_dirs()
    paths.latest_snapshot().write_text(json.dumps({
        "schema_version": 1,
        "scanned_at": "2026-08-01T00:00:00+00:00",
        "machine": "OLDBOX",
        "items": [{"path": "C:\\old", "size": 7}],
    }, ensure_ascii=False), encoding="utf-8")

    items, meta = snapshots.load_latest()
    assert len(items) == 1
    assert meta["present"] is True
    assert meta["schema_version"] == 1
    assert meta["scan_stats"] is None
    assert "reason" not in meta, "v1 是受支持的版本，不该报警告"


def test_m0_bare_array_still_readable():
    """M0 探针产物是裸数组（无信封）。兼容分支存在就得有测试盯着。"""
    paths.ensure_dirs()
    paths.latest_snapshot().write_text(
        json.dumps([{"path": "C:\\bare", "size": 3}]), encoding="utf-8")
    items, meta = snapshots.load_latest()
    assert len(items) == 1
    assert meta["present"] is True
    assert meta["schema_version"] == 0


def test_future_schema_warns_but_still_loads():
    """比本程序新的快照要提示重扫，但不能直接拒绝——数据还能用。"""
    paths.ensure_dirs()
    paths.latest_snapshot().write_text(json.dumps({
        "schema_version": snapshots.SCHEMA_VERSION + 5,
        "items": [{"path": "C:\\future", "size": 1}],
    }), encoding="utf-8")
    items, meta = snapshots.load_latest()
    assert len(items) == 1
    assert meta["present"] is True
    assert "建议重扫" in meta["reason"]


def test_bom_tolerated():
    """PowerShell 有的写法会带 BOM，两种都得能读。"""
    paths.ensure_dirs()
    paths.latest_snapshot().write_text(json.dumps({
        "schema_version": 2, "items": [{"path": "C:\\bom", "size": 1}],
    }), encoding="utf-8-sig")
    items, meta = snapshots.load_latest()
    assert len(items) == 1 and meta["present"] is True


# ------------------------------------------------------------- 原子写入
def test_write_is_atomic_no_partial_file_on_failure(monkeypatch):
    """写入中途失败不得留下半个快照，也不得损坏既有快照。"""
    snapshots.save_latest([{"path": "C:\\good", "size": 1}])
    good = paths.latest_snapshot().read_bytes()

    real_dump = json.dump

    def exploding_dump(*a, **kw):
        real_dump(*a, **kw)
        raise OSError("磁盘满了")

    monkeypatch.setattr(json, "dump", exploding_dump)
    with pytest.raises(OSError):
        snapshots.save_latest([{"path": "C:\\bad", "size": 2}])

    assert paths.latest_snapshot().read_bytes() == good, "既有快照被写坏了"
    leftovers = list(paths.snapshots_dir().glob("*.tmp"))
    assert not leftovers, f"失败后留下临时文件：{leftovers}"


# --------------------------------------------------------------- 归档
def test_archive_creates_timestamped_copy():
    snapshots.save_latest([{"path": "C:\\v1", "size": 1}])
    original = paths.latest_snapshot().read_bytes()

    dst = snapshots.archive_latest()
    assert dst is not None and dst.is_file()
    assert dst.read_bytes() == original
    assert dst.name != "latest.json"


def test_archive_without_existing_snapshot_returns_none():
    assert snapshots.archive_latest() is None


# ----------------------------------------------------------- 路径来源唯一
def test_data_root_honors_env_override(isolated_data_dir):
    """LOSTPATH_DATA_DIR 是测试与便携模式的唯一开关，它失效则测试会污染用户数据。"""
    assert str(paths.data_root()).startswith(str(isolated_data_dir))
    assert paths.latest_snapshot().parent == paths.snapshots_dir()
    d = paths.describe()
    assert d["override_active"] == "LOSTPATH_DATA_DIR"


def test_ensure_dirs_is_idempotent():
    paths.ensure_dirs()
    paths.ensure_dirs()
    for p in (paths.snapshots_dir(), paths.icons_dir(),
              paths.config_dir(), paths.logs_dir()):
        assert p.is_dir()


# --------------------------------------------- v3：体积语义变更后的过时标记
def test_pre_v3_snapshot_is_flagged_as_size_inflated():
    r"""v3 以下的快照必须标出"体积虚高"，不能静默读入。

    v3 之前扫描器里的硬链接去重整个是死代码（`os.DirEntry.stat()` 在 Windows 上
    `st_nlink` 恒为 0，条件永不成立），uv 这类去重缓存被虚报数倍——实测逻辑 1.59 GiB
    而真实占盘 0.31 GiB。而 `load_latest` 原先只在"快照比程序新"时才给提示，旧快照悄悄
    读进来，界面就拿虚高的数字算"能腾出多少"。**修好代码却对着旧数据，结论照样是错的。**
    """
    paths.ensure_dirs()
    paths.latest_snapshot().write_text(json.dumps({
        "schema_version": 2,
        "scanned_at": "2026-08-01T00:00:00+00:00",
        "items": [{"path": "C:\\x", "size": 1}],
    }, ensure_ascii=False), encoding="utf-8")

    _items, meta = snapshots.load_latest()
    assert meta["sizes_inflated"] is True, "v2 的体积是硬链接虚高的，必须标出来"
    assert "硬链接" in meta["sizes_reason"], "要给一句用户看得懂的说明，光有标志位界面没法讲"
    # 与 reason 分开：reason 的含义是"这份快照可能不可用"，而这里是"数据可用但数字过时"。
    # 混用一个字段会让界面分不清该引导重扫还是该报错。
    assert "reason" not in meta, "体积过时不是快照损坏，不该占用 reason"


def test_current_schema_snapshot_not_flagged():
    """当前版本扫的快照不该被误标，否则每次都提示重扫，用户会学会忽略提示。"""
    snapshots.save_latest([{"path": "C:\\x", "size": 1}])
    _items, meta = snapshots.load_latest()
    assert meta["sizes_inflated"] is False
    assert "sizes_reason" not in meta


def test_bare_array_also_flagged_as_inflated():
    """裸数组分支提前 return，容易漏标——它同样是 v3 之前的产物。"""
    paths.ensure_dirs()
    paths.latest_snapshot().write_text(
        json.dumps([{"path": "C:\\bare", "size": 3}]), encoding="utf-8")
    _items, meta = snapshots.load_latest()
    assert meta["schema_version"] == 0
    assert meta["sizes_inflated"] is True, "v0 也是去重生效之前的数据"
    assert meta.get("sizes_reason")
