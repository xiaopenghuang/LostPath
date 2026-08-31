r"""目录体积递归扫描（只读）。产出 scan_c.json 形态，供归因消费。

实测结论（M0，见 MEMORY.md）：`os.scandir` 递归扫 C 盘 64.7 万文件 17.7s，
所以 MFT 直读只是可选优化，不是必需。两个 Windows 特有的坑已处理：
- **硬链接去重曾经整个是死代码。** 原实现判 `st.st_nlink > 1`，而
  `os.DirEntry.stat()` 在 Windows 上的 `st_nlink` **恒为 0**，那个条件永远不成立——
  扫 70 万文件报出 `hardlink_dedup_bytes: 0`，而同一批文件用 `os.stat()` 实测有 31237
  条硬链接。后果是逐文件累加导致体积虚高：本机 uv 缓存逻辑 1.59 GiB / 真实占盘
  0.31 GiB，**虚高 412%**（旧注释写的"WinSxS 仅虚高 1.2%，是正确性细节"是错的，那个数
  据本身就是在去重从未生效的情况下量出来的）。现改用 `winfs.list_dir`——
  `GetFileInformationByHandleEx` 在枚举时一并给出 `FileId`，只贵 14.2%，见 winfs.py；
- 重解析点（junction/符号链接）必须跳过不递归，否则跨盘目标会被计进 C 盘，
  且可能成环。它们单独收集，归因侧用 `os.readlink` 解目标。

已知未修：路径超过 260 字符的深层目录，`os.scandir` 与本模块用的 API 都需要 `\\?\`
前缀才能打开，两者行为一致（实测），所以这不是本次改动引入的。它们目前落在 denied
里——路径可见，但字节数不计入总量。

P2 把它从一次性脚本改成可带进度与取消的函数：全盘扫描要十几秒，UI 需要进度，
用户需要能中断。walk 的判定逻辑未改。
"""
import argparse
import json
import os
import sys
import time

from .. import sysdirs, winfs

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MIN_REPORT = 20 * 1024 * 1024  # 只上报 >=20MB 的目录
PROGRESS_EVERY = 3000          # 每扫这么多目录回报一次进度


class ScanCancelled(Exception):
    """用户中断扫描。由 should_cancel 回调触发，用异常展开递归。"""


def scan_tree(root=None, min_report=MIN_REPORT, progress=None,
              should_cancel=None):
    r"""递归扫 root，返回报告 dict。

    root=None 表示系统盘根。**默认值刻意不写字面量**：函数默认值在 def 时求值，写
    `root="C:\\"` 就把它冻死了，系统装 D 盘的机器上扫的是数据盘。

    progress(dirs_done, current_path)  每 PROGRESS_EVERY 个目录调一次
    should_cancel() -> bool            为真则抛 ScanCancelled
    """
    root = root or sysdirs.system_drive_root()
    # 深目录树会超默认递归限制。放在函数内而不是模块顶层：库不该在 import
    # 时改解释器全局状态。
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(20000)

    dir_sizes = {}
    seen_ids = set()
    hardlink_saved = [0]
    reparse_points = []
    denied = []
    stats = {"files": 0, "dirs": 0}

    def walk(path):
        total = 0
        count = 0
        try:
            entries = winfs.list_dir(path)
        except OSError:
            denied.append(path)
            return 0, 0
        stats["dirs"] += 1
        if stats["dirs"] % PROGRESS_EVERY == 0:
            if should_cancel and should_cancel():
                raise ScanCancelled()
            if progress:
                progress(stats["dirs"], path)
        for e in entries:
            if e.is_reparse:
                reparse_points.append(e.path)
                continue
            if e.is_dir:
                t, c = walk(e.path)
                total += t
                count += c
            else:
                # 同一份内容被多个路径共用时只计一次。file_id 由枚举时一并取得，
                # 与 os.stat().st_ino 实测一致；为 None 表示该文件系统没给真 ID，
                # 此时不去重（宁可高报也不低报——低报会让用户以为清不出空间）。
                if e.file_id is not None:
                    if e.file_id in seen_ids:
                        hardlink_saved[0] += e.size
                        continue
                    seen_ids.add(e.file_id)
                total += e.size
                count += 1
                stats["files"] += 1
        dir_sizes[path] = (total, count)
        return total, count

    t0 = time.time()
    try:
        walk(root)
    finally:
        sys.setrecursionlimit(old_limit)
    elapsed = time.time() - t0

    return {
        "root": root,
        "elapsed_sec": round(elapsed, 1),
        "total_bytes": dir_sizes.get(root, (0, 0))[0],
        "total_files": stats["files"],
        "total_dirs": stats["dirs"],
        "hardlink_dedup_bytes": hardlink_saved[0],
        "hardlink_unique_tracked": len(seen_ids),
        "reparse_count": len(reparse_points),
        # 归因要按 path 查 junction 目标，全量保留；上限 400 是 M0 探针时期的
        # 报告截断，当年只为看分布，现在会漏掉真实重解析点。
        "reparse_points": reparse_points,
        "denied_count": len(denied),
        "denied_sample": denied[:120],
        "dirs": {p: s for p, s in dir_sizes.items() if s[0] >= min_report},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="目录体积递归扫描（只读）")
    ap.add_argument("root", nargs="?", default=None,
                    help="要扫的根，默认系统盘根")
    ap.add_argument("out", nargs="?", default="scan_c.json")
    args = ap.parse_args(argv)

    last = [0.0]

    def show(dirs_done, path):
        now = time.time()
        if now - last[0] < 1.0:
            return
        last[0] = now
        print(f"  {dirs_done} 个目录… {path[:70]}")

    report = scan_tree(args.root, progress=show)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False)

    print(f"done in {report['elapsed_sec']}s")
    print(f"total={report['total_bytes'] / 2**30:.1f}GiB "
          f"files={report['total_files']} dirs={report['total_dirs']}")
    print(f"hardlink_dedup={report['hardlink_dedup_bytes'] / 2**20:.1f}MiB "
          f"reparse={report['reparse_count']} denied={report['denied_count']}")
    print(f"reported_dirs={len(report['dirs'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
