r"""硬链接感知的测量与搬运。

这一层存在的理由是一次真实事故：清理 `C:\Users\<user>\AppData\Local\uv` 时，计划说能腾出
1.63 GiB，实际只能腾出 0.31 GiB——那个目录里 69979 个文件有 54340 个是硬链接，逐文件
累加把同一份内容重复计数了 5 次。而搬运用的 `shutil.copytree` 不认硬链接，把 0.31 GiB
复制成 1.63 GiB，于是"腾空间"变成"多占 3.22 GiB"。

所以守两件事：**测量要认硬链接**，**搬运不能把硬链接拆开**。
"""
import os

import pytest

from lostpath import fsdedup


def test_measure_dedups_hardlinks(tmp_path):
    """同一份内容多个链接：logical 重复计数，dedup 只计一次。"""
    (tmp_path / "one.bin").write_bytes(b"z" * 5000)
    os.link(tmp_path / "one.bin", tmp_path / "two.bin")
    os.link(tmp_path / "one.bin", tmp_path / "three.bin")
    (tmp_path / "solo.bin").write_bytes(b"q" * 700)

    m = fsdedup.measure(str(tmp_path))
    assert m.files == 4
    assert m.logical == 5000 * 3 + 700, "logical 是逐文件累加，与扫描器口径一致"
    assert m.dedup == 5700, f"三条链接指同一份内容，只该算一次，实得 {m.dedup}"
    assert m.linked_files == 3, "三个路径都属于有多链接的那份内容"
    assert m.has_hardlinks


def test_measure_without_hardlinks_is_identical(tmp_path):
    """没有硬链接时三个口径必须完全相等，否则普通目录会被误报。"""
    (tmp_path / "a.bin").write_bytes(b"a" * 1200)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"b" * 800)

    m = fsdedup.measure(str(tmp_path))
    assert m.logical == m.dedup == m.freeable == 2000
    assert m.linked_files == 0
    assert not m.has_hardlinks


def test_freeable_excludes_content_linked_from_outside(tmp_path):
    r"""**能腾出多少 ≠ 去重后的体积。**

    链接有一条在树外时，删掉树里这一份并不释放任何空间——内容还被树外那个路径引用
    着。实测 `C:\Program Files`：dedup 16.850 GiB，而按此定义只有 16.488 GiB，差的
    0.362 GiB 是与 WinSxS 共用的部分。把这两个口径混为一谈就会高报可腾空间。
    """
    inside = tmp_path / "tree"
    inside.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    (inside / "shared.bin").write_bytes(b"s" * 3000)
    os.link(inside / "shared.bin", outside / "shared.bin")   # 链接跑到树外
    (inside / "own.bin").write_bytes(b"o" * 400)

    m = fsdedup.measure(str(inside))
    assert m.dedup == 3400, "去重口径：树内两份不同内容"
    assert m.freeable == 400, (
        f"shared.bin 还被树外引用着，删这棵树腾不出它的 3000 字节，"
        f"可腾出只有 400，实得 {m.freeable}")


def test_freeable_counts_content_fully_inside(tmp_path):
    """全部链接都在树内时，可腾出等于去重体积——否则会低报，用户以为清不出空间。"""
    (tmp_path / "a.bin").write_bytes(b"a" * 2500)
    os.link(tmp_path / "a.bin", tmp_path / "b.bin")

    m = fsdedup.measure(str(tmp_path))
    assert m.dedup == 2500
    assert m.freeable == 2500, "两条链接都在树内，删掉整棵树就能释放这 2500 字节"


def test_copytree_keep_links_rebuilds_links(tmp_path):
    """复制后目标端仍是硬链接，不是两份独立文件。

    `shutil.copytree` 在这里会把 5000 字节复制成 10000。那次事故正是这样把 0.31 GiB
    变成 1.63 GiB。
    """
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "one.bin").write_bytes(b"z" * 5000)
    os.link(src / "one.bin", src / "sub" / "two.bin")        # 跨子目录的链接
    (src / "solo.bin").write_bytes(b"q" * 300)

    dst = tmp_path / "dst"
    made = fsdedup.copytree_keep_links(str(src), str(dst))

    assert made == 1, f"应重建 1 条链接，实得 {made}"
    assert (dst / "one.bin").read_bytes() == b"z" * 5000, "内容必须一字不差"
    assert (dst / "sub" / "two.bin").read_bytes() == b"z" * 5000
    assert (dst / "solo.bin").read_bytes() == b"q" * 300

    a = os.stat(dst / "one.bin")
    b = os.stat(dst / "sub" / "two.bin")
    assert a.st_ino == b.st_ino, "目标端两个路径必须仍指同一份内容"
    assert a.st_nlink >= 2

    m = fsdedup.measure(str(dst))
    assert m.dedup == 5300, f"目标端去重体积应与源一致，实得 {m.dedup}"


def test_copytree_keep_links_preserves_file_count_and_bytes(tmp_path):
    """文件数与逐文件字节数都要与源一致——执行器靠这两个数做搬迁后校验。"""
    src = tmp_path / "src"
    src.mkdir()
    for i in range(3):
        (src / f"f{i}.bin").write_bytes(bytes([i]) * (100 + i))
    os.link(src / "f0.bin", src / "link0.bin")

    dst = tmp_path / "dst"
    fsdedup.copytree_keep_links(str(src), str(dst))

    def stats(p):
        n = total = 0
        for dirpath, _d, names in os.walk(p):
            for x in names:
                n += 1
                total += os.path.getsize(os.path.join(dirpath, x))
        return n, total

    assert stats(str(dst)) == stats(str(src))


def test_copytree_keep_links_skips_reparse_points(tmp_path):
    """重解析点不跟进：跟进会把链接目标的内容整份复制过来，体积凭空翻倍。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.bin").write_bytes(b"r" * 500)
    target = tmp_path / "outside"
    target.mkdir()
    (target / "big.bin").write_bytes(b"b" * 9000)

    try:
        os.symlink(target, src / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("建符号链接需要权限，本机不允许")

    dst = tmp_path / "dst"
    fsdedup.copytree_keep_links(str(src), str(dst))
    assert (dst / "real.bin").exists()
    assert not (dst / "link" / "big.bin").exists(), "不该把链接目标的内容复制进来"


def test_measure_does_not_follow_reparse_directories(tmp_path):
    """测量也不能把目录 junction/符号链接的外部内容算进来。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.bin").write_bytes(b"r" * 500)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"b" * 9000)
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("建符号链接需要权限，本机不允许")

    m = fsdedup.measure(str(root))

    assert m.logical == m.dedup == m.freeable == 500
