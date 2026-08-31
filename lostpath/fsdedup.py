r"""硬链接感知的体积测量与搬运。

**为什么需要这个模块。** Windows 上大量目录用硬链接做去重：uv / pnpm 的包缓存、
WinSxS 组件库都是。同一份内容被多个路径共用，逐文件累加 `st_size` 会把它重复计数。
实测本机 `C:\Users\<user>\AppData\Local\uv`：69979 个文件逻辑合计 1.59 GiB，而真实占盘
**0.31 GiB**——高报 5 倍。用户照着 1.63 GiB 这个数按下清理，实际只腾出 0.31 GiB。

**为什么不在扫描时就算准。** 判断一个文件是不是硬链接需要 `st_nlink`，而
`os.DirEntry.stat()` 在 Windows 上的数据来自 `FindFirstFile`，那个 API 不返回链接数，
于是 `st_nlink` 恒为 0（`DirEntry.inode()` 有真值，但它内部也要开文件，同样贵）。拿真
值必须走 `os.stat()`，实测慢 2.7 倍——全盘扫描 21 秒会变成 57 秒。所以扫描继续报逻辑
大小（"这里有多少数据"，本身是个诚实的量），精确值只在**动手那一刻**为**单个目录**
算一次，那时多花几秒完全付得起。

**"能腾出多少"的正确定义。** 不是"每个 inode 计一次"，而是"每个 inode 计一次，且它
的**全部**链接都在这棵树内"。有链接在树外时，删掉树里这一份并不释放任何空间——内容还
被树外那个路径引用着。实测 `C:\Program Files`：inode 去重后 16.850 GiB，而按此定义只有
16.488 GiB，差的 0.362 GiB 是与 WinSxS 共用的部分。
"""
from __future__ import annotations

import errno
import os
import shutil

__all__ = ["measure", "copytree_keep_links", "EXDEV"]

EXDEV = errno.EXDEV


class Measurement:
    """一次测量的结果。字段分开而非只给一个数，是因为界面要解释差异从何而来。"""

    __slots__ = ("logical", "dedup", "freeable", "files", "linked_files")

    def __init__(self, logical: int, dedup: int, freeable: int,
                 files: int, linked_files: int):
        self.logical = logical            # 逐文件累加，与扫描器口径一致
        self.dedup = dedup                # 每个 inode 计一次
        self.freeable = freeable          # 删掉这棵树真正释放的字节
        self.files = files
        self.linked_files = linked_files  # 其中有多个链接的文件数

    @property
    def has_hardlinks(self) -> bool:
        return self.linked_files > 0

    def to_dict(self) -> dict:
        return {"logical": self.logical, "dedup": self.dedup,
                "freeable": self.freeable, "files": self.files,
                "linked_files": self.linked_files}

    def __repr__(self) -> str:
        return (f"Measurement(logical={self.logical}, dedup={self.dedup}, "
                f"freeable={self.freeable}, files={self.files}, "
                f"linked_files={self.linked_files})")


def measure(root: str) -> Measurement:
    r"""走一遍目录，给出逻辑 / 去重 / 可释放三个口径。

    用 `os.stat()` 而非 `DirEntry.stat()`——后者在 Windows 上拿不到 `st_nlink`（恒为
    0），这正是原扫描器里那段去重代码从未生效的原因。慢 2.7 倍，但这个函数只在执行
    单个目录时调用一次。

    取不到 stat 的项跳过而非中断：目录可能正被别的程序改动，测量本身不该失败。
    `st_nlink` 取遍历期间见过的最大值——若中途有链接消失，用最大值会让 freeable 偏
    保守（少报可释放量），这个方向的偏差是安全的。
    """
    info: dict[tuple[int, int], list[int]] = {}   # key -> [size, nlink, inside]
    logical = 0
    files = 0
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                st = os.stat(p, follow_symlinks=False)
            except OSError:
                continue
            files += 1
            logical += st.st_size
            key = (st.st_dev, st.st_ino)
            cur = info.get(key)
            if cur is None:
                info[key] = [st.st_size, st.st_nlink, 1]
            else:
                cur[2] += 1
                if st.st_nlink > cur[1]:
                    cur[1] = st.st_nlink
    dedup = sum(v[0] for v in info.values())
    freeable = sum(v[0] for v in info.values() if v[2] >= v[1])
    linked = sum(v[2] for v in info.values() if v[1] > 1)
    return Measurement(logical, dedup, freeable, files, linked)


def copytree_keep_links(src: str, dst: str) -> int:
    r"""跨卷复制目录树，**树内的硬链接关系在目标端重建**。

    `shutil.copytree` 不认硬链接：同一个 inode 有 28 条链接，它就复制 28 份独立文件。
    实测那次失败正是这样把 0.31 GiB 的 uv 缓存复制成 1.63 GiB——用户想腾空间，结果
    多占了 3.22 GiB。

    做法：按 (dev, ino) 分组，每个 inode 只真复制一次，同组后续路径用 `os.link` 指
    向已复制的那份。硬链接不能跨卷，所以只能在目标端重建，这也是唯一可行的做法。

    返回重建的链接数，供调用方记账与校验。

    链接失败（目标卷不支持硬链接，比如 FAT32/exFAT）时退化为真复制并继续——宁可多
    占空间也不能让搬迁失败，那会让用户的数据卡在半路。
    """
    made = 0
    first_copy: dict[tuple[int, int], str] = {}
    for dirpath, dirs, names in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        out_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(out_dir, exist_ok=True)
        for d in list(dirs):
            # 重解析点不跟进：跟进会把链接目标的内容整份复制过来
            try:
                if os.lstat(os.path.join(dirpath, d)).st_file_attributes & 0x400:
                    dirs.remove(d)
            except (OSError, AttributeError):
                pass
        for n in names:
            s = os.path.join(dirpath, n)
            d = os.path.join(out_dir, n)
            try:
                st = os.stat(s, follow_symlinks=False)
            except OSError:
                shutil.copy2(s, d)
                continue
            if st.st_nlink <= 1:
                shutil.copy2(s, d)
                continue
            key = (st.st_dev, st.st_ino)
            done = first_copy.get(key)
            if done is None:
                shutil.copy2(s, d)
                first_copy[key] = d
            else:
                try:
                    os.link(done, d)
                    made += 1
                except OSError:
                    shutil.copy2(s, d)
    shutil.copystat(src, dst)
    return made
