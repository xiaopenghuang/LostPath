"""快照读写。快照 = 某次扫描产出的 C 盘归因结果。

关键约束：**缺快照是正常状态**，不是错误。用户首次启动、或换了新机器时都没有
快照，此时 API 必须能起来并让 UI 引导用户扫描，而不是崩在启动阶段。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import paths

# v2 起信封多带 scan_stats（目录/文件数、耗时、拒绝访问数）。纯新增字段，v1
# 快照仍能正常读；它的用处见 runner 的进度估算与 UI 的盲区明示。
#
# v3 是**语义变更，不是加字段**：`size` 从"逐文件累加"改成"硬链接只计一次"。此前扫描器
# 里那段去重代码整个是死的（`os.DirEntry.stat()` 在 Windows 上 `st_nlink` 恒为 0，条件永
# 不成立），于是 uv / pnpm 这类去重缓存被虚报数倍——本机实测 uv 逻辑 1.59 GiB、真实占盘
# 0.31 GiB，虚高 412%。所以 v3 以下的快照**数字不能直接信**，必须显式提示重扫：修好的
# 代码对着旧数据照样会算出"能腾出 1.63 GiB"，而用户是照着那个数按下执行的。
SCHEMA_VERSION = 3
# 低于此版本的快照，其 size 是硬链接虚高的
SIZES_DEDUPED_SINCE = 3


def load_latest() -> tuple[list[dict], dict]:
    """读当前快照，返回 (痕迹列表, 元信息)。

    没有快照时返回 ([], {"present": False})——调用方据此让 UI 走引导页。
    """
    p = paths.latest_snapshot()
    if not p.is_file():
        return [], {"present": False, "reason": "尚未扫描本机"}
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        return [], {"present": False, "reason": f"快照损坏：{type(e).__name__}"}

    # 兼容两种形态：裸数组（M0 probe 产物）与带信封的新格式
    if isinstance(raw, list):
        return raw, {
            "present": True, "schema_version": 0, "scanned_at": None,
            "note": "M0 裸数组格式，重扫后升级为带信封格式",
            # 裸数组是 v3 之前的产物，体积一定是硬链接虚高的。这条分支提前 return，
            # 不会走到下面那段判断，所以要在这里自己标上。
            "sizes_inflated": True,
            "sizes_reason": "这份快照是早期格式，体积没有排除硬链接。重新扫描一次即可。",
        }

    if not isinstance(raw, dict):
        return [], {"present": False, "reason": "快照结构损坏：顶层必须是对象"}
    items = raw.get("items")
    if not isinstance(items, list):
        return [], {"present": False, "reason": "快照结构损坏：items 必须是数组"}
    if not all(isinstance(item, dict) for item in items):
        return [], {"present": False, "reason": "快照结构损坏：items 包含无效条目"}
    ver = raw.get("schema_version")
    if ver is not None and (isinstance(ver, bool) or not isinstance(ver, int)):
        return [], {"present": False, "reason": "快照结构损坏：schema_version 必须是整数"}
    meta = {
        "present": True,
        "schema_version": ver,
        "scanned_at": raw.get("scanned_at"),
        "machine": raw.get("machine"),
        # v1 快照没有这一项，取不到就是 None，调用方须容忍
        "scan_stats": raw.get("scan_stats"),
    }
    if ver is not None and ver > SCHEMA_VERSION:
        meta["reason"] = f"快照 schema v{ver} 高于本程序支持的 v{SCHEMA_VERSION}，建议重扫"
    # 反向也得提示。原先只警告"比程序新"，旧快照静默读入——而 v3 以下的 size 是硬链接
    # 虚高的，不说的话界面会拿虚高数倍的数字算"能腾出多少"，用户照着它下决定。
    meta["sizes_inflated"] = (ver or 0) < SIZES_DEDUPED_SINCE
    if meta["sizes_inflated"]:
        meta["sizes_reason"] = (
            f"这份快照（v{ver or 0}）的体积没有排除硬链接，像 uv / pnpm 这类共用内容的"
            f"缓存会被高报数倍。重新扫描一次即可得到准确数字。")
    return items, meta


def save_latest(items: list[dict], machine: str | None = None,
                scan_stats: dict | None = None) -> Path:
    """原子写入快照。先写临时文件再 replace，避免扫描中断留下半个文件。

    scan_stats 记这次扫了多少目录/文件、耗时、多少目录拒绝访问。两个用处：
    下次扫描据此估进度（目录总数每台机器差一个量级，写死常数必然不准），
    以及让 UI 能明示非管理员盲区。
    """
    paths.ensure_dirs()
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": machine or os.environ.get("COMPUTERNAME"),
        "scan_stats": scan_stats,
        "items": items,
    }
    target = paths.latest_snapshot()
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(envelope, f, ensure_ascii=False)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def archive_latest() -> Path | None:
    """把当前快照另存一份带时间戳的历史副本，供重扫前留底。"""
    src = paths.latest_snapshot()
    if not src.is_file():
        return None
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M")
    dst = paths.snapshots_dir() / f"{stamp}.json"
    dst.write_bytes(src.read_bytes())
    return dst
