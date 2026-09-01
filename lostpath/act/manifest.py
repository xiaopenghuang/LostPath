r"""回滚记录。**写操作之前先落盘，这是红线要求（DESIGN.md §1）。**

顺序是刻意的：先写记录，再动文件。反过来的话，程序在两步之间崩掉就会留下"文件已经
动了但没有任何记录"的状态——用户既不知道东西去哪了，工具也无从恢复。多写一次记录的
代价，远小于丢失可恢复性。

记录形态：`%LOCALAPPDATA%\LostPath\operations\<时间戳>-<id>.json`。一次操作一个文件，
不用单一大文件——并发或崩溃时不会互相污染，也方便用户直接翻看某一次都做了什么。

状态机（status 字段）：
    planned    记录已落盘，尚未动手。**看到这个状态说明中途崩了**，需人工检查
    done       操作完成且已校验
    rolled_back 已回滚，系统回到操作前
    failed     操作失败，failure 字段说明原因；文件状态由 steps_done 判断
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..storage import paths

SCHEMA_VERSION = 1
# 回收期：过了才允许真删。给用户留出"咦，那个软件怎么了"的反应时间。
RECOVERABLE_DAYS = 30


def operations_dir() -> Path:
    return paths.data_root() / "operations"


def recycle_dir() -> Path:
    """回收区。删除一律先移到这里，不直接 rmtree。"""
    return paths.data_root() / "recycle"


def new_operation(action: str, plan: dict) -> dict:
    """建一条操作记录（尚未落盘）。plan 是 planner 出的计划 dict。"""
    now = datetime.now(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "id": uuid.uuid4().hex[:12],
        "created_at": now.isoformat(timespec="seconds"),
        "recoverable_until": (now + timedelta(days=RECOVERABLE_DAYS))
        .isoformat(timespec="seconds"),
        "action": action,
        "status": "planned",
        "machine": os.environ.get("COMPUTERNAME"),
        # 计划原文存进来：出问题时要能还原当时的判断依据，而不是只看到结果
        "plan": plan,
        "source_path": plan.get("path"),
        "size": plan.get("size"),
        "files": plan.get("files"),
        # 逐步记录，崩溃后据此判断做到哪了
        "steps_done": [],
        "recycled_to": None,
        "env_var": plan.get("env_var"),
        "env_previous": None,      # 原值；None 表示原本不存在该变量
        "env_new": None,
        "failure": None,
    }


def path_for(op: dict) -> Path:
    stamp = op["created_at"].replace(":", "-").replace("+00-00", "Z")
    return operations_dir() / f"{stamp}-{op['id']}.json"


def save(op: dict) -> Path:
    """原子写入操作记录。每次状态变化都要调，落盘失败就该中止操作。"""
    operations_dir().mkdir(parents=True, exist_ok=True)
    target = path_for(op)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(op, f, ensure_ascii=False, indent=1)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def mark(op: dict, status: str, **fields) -> Path:
    """更新状态并立刻落盘。"""
    op["status"] = status
    op.update(fields)
    op["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return save(op)


def add_step(op: dict, step: str) -> Path:
    op["steps_done"].append({
        "step": step,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return save(op)


def load(path: str | Path) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def list_operations() -> list[dict]:
    """按时间倒序列出全部操作记录。坏文件跳过但要能看出来。"""
    d = operations_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            op = load(p)
        except (OSError, json.JSONDecodeError) as e:
            out.append({"id": p.stem, "status": "unreadable",
                        "failure": f"{type(e).__name__}: {e}",
                        "manifest_path": str(p)})
            continue
        op["manifest_path"] = str(p)
        out.append(op)
    return out


def find(op_id: str) -> dict | None:
    for op in list_operations():
        if op.get("id") == op_id:
            return op
    return None


def is_expired(op: dict) -> bool:
    """回收期是否已过。过期项才允许真删。"""
    until = op.get("recoverable_until")
    if not until:
        return False
    try:
        return datetime.now(timezone.utc) > datetime.fromisoformat(until)
    except ValueError:
        return False


def is_purged(op: dict) -> bool:
    """回收区数据是否已被永久删除。

    不能只看 `recycled_to is None`：清空时会把它置空，而"从未搬过东西"的操作那里
    本来也是空的。两者的后果完全不同——前者不可回滚，后者可以。所以用 purged_at
    这个只在真删时才写的字段来区分。
    """
    return bool(op.get("purged_at"))


def pending_rollback() -> list[dict]:
    """可回滚的操作：已完成、数据未被永久删除、且回收区内容还在。"""
    out = []
    for op in list_operations():
        if op.get("status") != "done" or is_purged(op):
            continue
        if op.get("rollback_supported") is False:
            continue
        dst = op.get("recycled_to")
        if dst is None or os.path.exists(dst):
            out.append(op)
    return out


def public_operation(op: dict) -> dict:
    """返回可安全交给 UI 的操作记录，不泄露变量原值、卸载命令或注册表备份。"""
    public = dict(op)
    for field in (
        "env_previous", "env_new", "registry_backup", "uninstall_command",
        "uninstall_baseline", "uninstall_audit", "context_menu_executable",
        "context_menu_created_keys", "context_menu_deleted_command",
        "context_menu_backup", "context_menu_markers", "context_menu_deleted_entries",
    ):
        public.pop(field, None)
    return public


def days_left(op: dict) -> int | None:
    """回收期还剩几天。已过期返回 0，无该字段返回 None。"""
    until = op.get("recoverable_until")
    if not until:
        return None
    try:
        delta = datetime.fromisoformat(until) - datetime.now(timezone.utc)
    except ValueError:
        return None
    return max(0, delta.days)


def dir_size(path: str) -> tuple[int, int]:
    """返回 (字节数, 文件数)。取不到的项跳过而非中断。"""
    total = files = 0
    for dirpath, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(dirpath, n))
                files += 1
            except OSError:
                pass
    return total, files


def recycle_entries() -> list[dict]:
    r"""回收区逐条清单。

    体积按磁盘实测而非沿用 manifest 里记的 size：那个是归因时的快照值，可能与实际
    移进来的内容有差（比如执行前软件又写了些文件）。用户要据此决定删不删，得给实数。

    **也认领"台账没记全"的数据。** 曾经出过这样的状态：回收区里实存 3.22 GiB，而界面
    显示"回收区是空的、0 项"——因为那两次操作在移动中途失败，`recycled_to` 还是 None，
    而本函数与 `purge_expired()` 都只认这个字段。结果那些数据界面看不见、30 天后也不会
    被自动清掉，只有翻文件系统才能发现。所以这里额外看两处：

    1. `recycle_intent`——执行前就落盘的"打算搬到哪"（见 executor._recycle_dst）。
       它存在而 `recycled_to` 为空，说明搬运没走完，数据可能已经在那儿了。
    2. 回收区里以操作 id 命名、但任何台账记录都不认领的目录。台账文件本身丢了或写坏
       时只剩这条路能发现它们。

    这两类都标 `unconfirmed=True`，让界面能说清"这份数据的搬运没有完成"，而不是假装
    它是一次正常的回收。
    """
    out = []
    claimed: set[str] = set()
    for op in list_operations():
        dst = op.get("recycled_to")
        unconfirmed = False
        if not dst:
            dst = op.get("recycle_intent")
            unconfirmed = True
        if not dst or not os.path.exists(dst):
            continue
        claimed.add(os.path.normcase(os.path.dirname(dst)))
        size, files = dir_size(dst)
        out.append({
            "id": op["id"],
            "action": op.get("action"),
            "source_path": op.get("source_path"),
            "recycled_to": dst,
            "size": size,
            "files": files,
            "created_at": op.get("created_at"),
            "recoverable_until": op.get("recoverable_until"),
            "days_left": days_left(op),
            "expired": is_expired(op),
            "status": op.get("status"),
            "env_var": op.get("env_var"),
            "unconfirmed": unconfirmed,
            "freed": op.get("freed"),
        })
    out.extend(_unclaimed_recycle_dirs(claimed))
    out.sort(key=lambda e: -e["size"])
    return out


def _unclaimed_recycle_dirs(claimed: set[str]) -> list[dict]:
    """回收区里没有任何台账记录认领的目录。台账写坏或丢失时的最后一道发现手段。"""
    root = recycle_dir()
    out = []
    try:
        shells = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return out
    for shell in shells:
        if os.path.normcase(str(shell)) in claimed:
            continue
        size, files = dir_size(str(shell))
        if not files:
            continue
        out.append({
            "id": shell.name,
            "action": None,
            "source_path": None,
            "recycled_to": str(shell),
            "size": size,
            "files": files,
            "created_at": None,
            "recoverable_until": None,
            "days_left": None,
            "expired": False,
            "status": "orphan",
            "env_var": None,
            "unconfirmed": True,
            "freed": None,
        })
    return out
