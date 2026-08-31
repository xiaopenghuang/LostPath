"""目录扫描回归。在临时目录树上跑真实 scan_tree，不碰 C 盘。

守三件事：体积统计正确、进度回调与取消真的接通、以及 Windows 那两个特有行为
（重解析点不递归、min_report 门槛）。
"""
import os

import pytest

from lostpath.scan.scan_dirs import (MIN_REPORT, PROGRESS_EVERY, ScanCancelled,
                                     scan_tree)


@pytest.fixture
def tree(tmp_path):
    """造一棵已知体积的目录树：根 3000B，子目录 a=2000B、b=1000B。"""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "x.bin").write_bytes(b"x" * 2000)
    (tmp_path / "b" / "y.bin").write_bytes(b"y" * 1000)
    return tmp_path


def test_sizes_and_counts(tree):
    r = scan_tree(str(tree), min_report=0)
    assert r["total_bytes"] == 3000
    assert r["total_files"] == 2
    assert r["dirs"][str(tree)] == (3000, 2)
    assert r["dirs"][str(tree / "a")] == (2000, 1)
    assert r["dirs"][str(tree / "b")] == (1000, 1)


def test_min_report_filters_small_dirs(tree):
    """门槛之下的目录不上报——106 处足迹这个量级靠的就是它。"""
    r = scan_tree(str(tree), min_report=1500)
    reported = set(r["dirs"])
    assert str(tree / "a") in reported          # 2000B 过线
    assert str(tree / "b") not in reported      # 1000B 不过线


def test_default_min_report_is_20mb():
    assert MIN_REPORT == 20 * 1024 * 1024


def test_hardlinks_counted_once(tmp_path):
    r"""同一份内容有多个硬链接时只计一次体积。

    **这条曾经整个是死代码。** 原实现判 `st.st_nlink > 1`，而 `os.DirEntry.stat()` 在
    Windows 上 `st_nlink` 恒为 0，条件永远不成立：扫 70 万文件报出
    `hardlink_dedup_bytes: 0`，实际有 31237 条硬链接。本机 uv 缓存因此虚报
    1.59 GiB（真实占盘 0.31 GiB），用户照着这个数点下清理，只腾出五分之一。

    以前没有任何测试覆盖它——`tmp_path` 造的树里没有硬链接，所以死代码看不出来。
    """
    (tmp_path / "one.bin").write_bytes(b"z" * 5000)
    os.link(tmp_path / "one.bin", tmp_path / "two.bin")     # 同一份内容，两个名字
    (tmp_path / "solo.bin").write_bytes(b"q" * 700)

    r = scan_tree(str(tmp_path), min_report=0)
    assert r["total_bytes"] == 5700, (
        f"5000 的内容有两个链接，只该算一次，加上 700 应为 5700，"
        f"实得 {r['total_bytes']}（若为 10700 说明去重没生效）")
    assert r["total_files"] == 2, "去重后第二条链接不再计入文件数"
    assert r["hardlink_dedup_bytes"] == 5000, (
        f"应记下省掉的 5000 字节，实得 {r['hardlink_dedup_bytes']}"
        f"（0 说明一条硬链接都没认出来）")


def test_hardlink_dedup_survives_subdirectories(tmp_path):
    """两条链接分处不同子目录时同样只计一次，父目录不该重复累加。"""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "f.bin").write_bytes(b"w" * 4000)
    os.link(tmp_path / "a" / "f.bin", tmp_path / "b" / "f.bin")

    r = scan_tree(str(tmp_path), min_report=0)
    assert r["total_bytes"] == 4000, f"跨子目录的硬链接仍只算一次，实得 {r['total_bytes']}"


def test_denied_dirs_are_counted_not_fatal(tree, monkeypatch):
    """拒绝访问要计数不能中断——非管理员盲区约 96 个目录，这是常态。

    桩打在 `winfs.list_dir` 上：扫描器已从 `os.scandir` 换成它（`os.scandir` 在
    Windows 上拿不到 file_id，硬链接去重因此整个是死代码，见 scan_dirs 开头）。
    """
    from lostpath import winfs
    real = winfs.list_dir

    def fake(path):
        if str(path).endswith("b"):
            raise PermissionError("拒绝访问")
        return real(path)

    monkeypatch.setattr(winfs, "list_dir", fake)
    r = scan_tree(str(tree), min_report=0)
    assert r["denied_count"] >= 1
    assert r["total_bytes"] == 2000, "被拒目录的体积不该计入，其余仍应统计"


def test_progress_callback_fires_and_is_monotonic(tmp_path):
    """进度必须真回调且单调递增，否则 UI 没反馈或进度倒退。"""
    # 目录数要超过 PROGRESS_EVERY 才会触发回调
    for i in range(PROGRESS_EVERY + 50):
        (tmp_path / f"d{i}").mkdir()

    seen = []
    scan_tree(str(tmp_path), min_report=0,
              progress=lambda n, p: seen.append(n))
    assert seen, f"目录数超过 {PROGRESS_EVERY} 却没有进度回调"
    assert seen == sorted(seen), f"进度回退了：{seen}"


def test_should_cancel_aborts_scan(tmp_path):
    for i in range(PROGRESS_EVERY + 50):
        (tmp_path / f"d{i}").mkdir()

    with pytest.raises(ScanCancelled):
        scan_tree(str(tmp_path), min_report=0, should_cancel=lambda: True)


def test_recursion_limit_is_restored(tree):
    """库不该留下解释器全局状态的副作用。"""
    import sys

    before = sys.getrecursionlimit()
    scan_tree(str(tree), min_report=0)
    assert sys.getrecursionlimit() == before


def test_reparse_points_not_truncated(tmp_path, monkeypatch):
    """重解析点必须全量返回。

    原先截断到 400 条（M0 探针时期只为看分布）。归因要按 path 查 junction 目标，
    超出的查不到就是静默丢证据——本机 130 条恰好没暴露这个问题。
    """
    from lostpath import winfs

    fake_points = [f"C:\\fake{i}" for i in range(500)]
    entries = [winfs.Entry(os.path.basename(p), p, 0, i + 1,
                           winfs.FILE_ATTRIBUTE_REPARSE_POINT)
               for i, p in enumerate(fake_points)]
    monkeypatch.setattr(winfs, "list_dir", lambda p: list(entries))
    r = scan_tree(str(tmp_path), min_report=0)
    assert r["reparse_count"] == 500
    assert len(r["reparse_points"]) == 500, \
        "重解析点被截断了，归因会丢 junction 证据"
