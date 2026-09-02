r"""执行器：按计划动手，每步先写回滚记录。**唯一会真写用户系统的模块。**

三条不可动摇的规矩：

1. **先写记录，再动文件。** 反过来的话，程序在两步之间崩掉就留下"文件已经动了但没有
   任何记录"的状态。多写一次记录的代价远小于丢失可恢复性。
2. **删除一律先移入回收区**，过回收期才允许真删。`shutil.rmtree` 只出现在两处：
   "清空过期回收区"（要求 manifest 已标记过期），以及"junction 回滚后清掉新盘上那份
   多余副本"（要求已回读确认原位数据完整回来、文件数与字节数与操作前一致）。两处的
   共同点是删之前已经证明数据另有完好的一份。
3. **只认计划，不自己判断。** 拦阻是 planner 的职责，执行器只做二次校验（防计划过期）
   而不新增判断——两处都能否决会让"为什么没执行"难以追查。

回滚的语义是"回到操作前"，不是"撤销上一步"：redirect 操作会同时改变量和移走旧缓存，
回滚要把两者一起还原，且顺序与执行相反（先还文件再还变量），这样中间态永远是安全的。
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .. import fsdedup, sysdirs
from . import envvar, manifest
from .planner import Plan, plan_for


class ExecutionRefused(RuntimeError):
    """执行前的安全校验没过。带上原因，不做任何改动。"""


class ExecutionFailed(RuntimeError):
    """执行中途失败。manifest 已落盘，据它判断做到哪了。"""


def _guard(path: str) -> None:
    r"""执行前的最后一道校验。planner 已经判过，这里再判一次防计划过期。

    系统目录判据在 `lostpath/sysdirs.py`，**不在本文件写死盘符**。原先这里是一张
    `("c:\\windows", "c:\\program files", ...)` 的字面量表，系统装 D 盘的机器上
    `d:\windows` 不在名单里，这道自称"最后一道闸"的校验就是空的——而同一个概念
    `lostpath_kb.high_risk()` 早已泛化过盘符并有测试钉着。同一件事两处实现只修了
    一处，是本项目反复出现的形状。
    """
    if not path:
        raise ExecutionRefused("计划里没有路径")
    p = os.path.abspath(path)
    low = p.lower().rstrip("\\")
    why = sysdirs.protected_system_dir(p)
    if why:
        raise ExecutionRefused(f"拒绝操作系统目录（{why}）：{p}")
    # 盘根不能动
    if len(low) <= 3 and low.endswith(":"):
        raise ExecutionRefused(f"拒绝操作盘根：{p}")
    if not os.path.isdir(p):
        raise ExecutionRefused(f"目录不存在（快照可能已过期）：{p}")


def _revalidate(record: dict, *, as_child: bool = False) -> Plan:
    """拿当下的磁盘实况重算一遍计划。

    计划可能是几分钟前算的，期间软件可能启动了、目录可能被删了。执行前重算比信任
    旧计划安全。重算结果不可执行就直接拒绝。
    """
    fresh = plan_for(record, as_child=as_child)
    if not fresh.executable:
        why = "；".join(b.reason for b in fresh.blockers) or "计划已不可执行"
        raise ExecutionRefused(f"计划已失效：{why}")
    return fresh


def _remove_empty_shell(recycled_path: str) -> None:
    r"""数据离开回收区后，删掉那个以操作 id 命名的空壳目录。

    回收区结构是 `recycle\<操作id>\<原目录名>`。数据无论是被还原走还是被永久删除，
    中间那层都会空下来。不删的话每操作一次攒一个空目录，用久了回收区全是空壳，
    与界面上"回收区是空的"直接矛盾。

    **只删空目录。** os.rmdir 遇到非空会抛，正是想要的行为——非空说明状态与预期不符，
    此时宁可留着让人看见，也不该替用户做删除决定（这也是不用 rmtree 的原因）。
    """
    try:
        os.rmdir(os.path.dirname(recycled_path))
    except OSError:
        pass


def _recycle_dst(src: str, op: dict) -> str:
    """算出回收区目标路径。**纯计算，不动文件**——这样能在动手前先记进台账。"""
    return str(manifest.recycle_dir() / op["id"]
               / os.path.basename(src.rstrip("\\")))


def _move_to_recycle(src: str, op: dict) -> str:
    r"""把目录移入回收区，返回目标路径。同盘时是秒级重命名。

    **只有真跨卷（EXDEV）才复制。** 原实现是 `except OSError` 一律回退到
    `shutil.move`，注释写着"跨盘"，但实测同盘失败远比跨盘常见：目录内只要有一个文件
    被打开、或有进程的工作目录在里面，`os.rename` 就以 `errno=13/winerror=5` 失败。
    那次真实故障就是这样——回收区与源目录同在 C 盘，本该瞬间改名，却因缓存里跑着的
    解释器持有句柄而落进复制分支，把 0.31 GiB 复制成 1.63 GiB，然后 rmtree 撞上被加载
    的 DLL 失败，留下三份数据。errno 13 与 18 能干净区分，那就别把它们混为一谈：被锁
    就如实报被锁，让上层拦阻或让用户去关程序，而不是默默搬 1.6 GiB。

    跨卷时用 `copytree_keep_links` 而非 `shutil.copytree`：后者把每条硬链接复制成独立
    文件，会让"腾空间"变成"多占空间"。
    """
    dst = _recycle_dst(src, op)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)          # 同盘：瞬间完成，硬链接原样保留
        return dst
    except OSError as e:
        if e.errno != fsdedup.EXDEV:
            raise ExecutionFailed(
                f"移动失败，源目录一动没动：{src}\n"
                f"系统报 errno={e.errno}/winerror={getattr(e, 'winerror', None)}：{e}\n"
                f"不是跨盘（跨盘是 errno={fsdedup.EXDEV}）。同盘改名失败通常是该目录里"
                f"有文件正被打开，或有程序的工作目录在其中——请关掉相关程序再试。") from e
    # 真跨卷：复制 + 校验 + 删源。任一步失败都不删源，宁可留副本也不丢数据。
    links = fsdedup.copytree_keep_links(src, dst)
    manifest.add_step(op, f"copied_cross_volume:{dst}:links={links}")
    src_files, src_bytes = _dir_stats(src)
    dst_files, dst_bytes = _dir_stats(dst)
    if (src_files, src_bytes) != (dst_files, dst_bytes):
        raise ExecutionFailed(
            f"跨盘复制后不一致：源 {src_files} 个/{src_bytes} 字节，"
            f"目标 {dst_files} 个/{dst_bytes} 字节。源目录未删，数据仍完整")
    shutil.rmtree(src, ignore_errors=False)
    return dst


# ------------------------------------------------------------------ 清理
def execute_cleanup(record: dict, dry_run: bool = False, *,
                    as_child: bool = False) -> dict:
    """清理一个可再生缓存目录：移入回收区 + 写回滚记录。

    dry_run=True 时只出记录不动文件，用于让调用方看清将要发生什么。
    """
    fresh = _revalidate(record, as_child=as_child)
    if fresh.action != "cleanup":
        raise ExecutionRefused(f"该目录的计划动作是 {fresh.action}，不是 cleanup")
    src = fresh.path
    _guard(src)

    op = manifest.new_operation("cleanup", fresh.to_dict())
    if dry_run:
        op["status"] = "dry_run"
        return op

    # 意图先落盘：搬运中途崩掉时，台账必须已经知道数据可能在哪。原实现是移动**之后**
    # 才填 recycled_to，于是那次失败留下 3.22 GiB 副本而台账 recycled_to=None——而
    # recycle_entries() 与 purge_expired() 都只认这个字段，副本因此界面看不见、也永远
    # 不会被自动清理。见 manifest.recycle_intent 的说明。
    op["recycle_intent"] = _recycle_dst(src, op)
    manifest.save(op)                            # 先落盘，再动文件
    try:
        dst = _move_to_recycle(src, op)
        op["recycled_to"] = dst
        manifest.add_step(op, f"moved_to_recycle:{dst}")
        if os.path.exists(src):
            raise ExecutionFailed(f"移动后源目录仍存在：{src}")
        if not os.path.exists(dst):
            raise ExecutionFailed(f"移动后目标不存在：{dst}")
        # 真正腾出的空间只有搬完才知道（逻辑体积会把硬链接重复计数）。记下实测值，
        # 让成功提示与台账给的是真数，而不是计划里那个上界。
        op["freed"] = fsdedup.measure(dst).to_dict()
        manifest.mark(op, "done")
    except ExecutionFailed as e:
        # 记原文而非笼统的"校验未通过"：失败原因里最有用的信息恰恰是**具体**是什么
        # 挡住了（比如"目录里有文件正被打开"），换成一句套话等于把它扔掉。
        manifest.mark(op, "failed", failure=str(e))
        raise
    except Exception as e:
        manifest.mark(op, "failed", failure=f"{type(e).__name__}: {e}")
        raise ExecutionFailed(str(e)) from e
    return op


def execute_residue_recycle(candidate: dict, parent_operation_id: str,
                            dry_run: bool = False) -> dict:
    """回收深度卸载审计确认过的残留目录。

    候选必须由卸载审计生成，上层还会按候选 ID 再核对一次。这里仍执行系统目录、
    盘根、目录存在性与 junction 闸门，防止页面数据过期后误动其它位置。
    """
    if candidate.get("action") != "recycle_directory" or not candidate.get("can_clean"):
        raise ExecutionRefused("该残留项未获准自动清理")
    src = candidate.get("path")
    _guard(src)
    if _is_junction(src):
        raise ExecutionRefused(f"拒绝回收 junction 残留：{src}")

    plan = {
        "path": src,
        "name": candidate.get("name") or os.path.basename(src),
        "size": candidate.get("size"),
        "files": candidate.get("files"),
        "reason": candidate.get("reason"),
        "candidate_id": candidate.get("id"),
        "parent_operation_id": parent_operation_id,
    }
    op = manifest.new_operation("uninstall_residue_cleanup", plan)
    op["rollback_supported"] = True
    op["parent_operation_id"] = parent_operation_id
    if dry_run:
        op["status"] = "dry_run"
        return op

    op["recycle_intent"] = _recycle_dst(src, op)
    manifest.save(op)
    try:
        dst = _move_to_recycle(src, op)
        op["recycled_to"] = dst
        manifest.add_step(op, f"moved_to_recycle:{dst}")
        if os.path.exists(src):
            raise ExecutionFailed(f"移动后源目录仍存在：{src}")
        if not os.path.exists(dst):
            raise ExecutionFailed(f"移动后目标不存在：{dst}")
        op["freed"] = fsdedup.measure(dst).to_dict()
        manifest.mark(op, "done")
    except ExecutionFailed as exc:
        manifest.mark(op, "failed", failure=str(exc))
        raise
    except Exception as exc:
        manifest.mark(op, "failed", failure=f"{type(exc).__name__}: {exc}")
        raise ExecutionFailed(str(exc)) from exc
    return op


# ------------------------------------------------------------------ 重定向
def execute_redirect(record: dict, target_root: str | None = None,
                     dry_run: bool = False, *, as_child: bool = False) -> dict:
    """改环境变量把缓存指到新盘，然后把旧缓存移入回收区。

    不复制旧文件：这是可再生缓存，让软件在新位置重新下载比搬运可靠，也避免搬运中损坏。
    """
    fresh = plan_for(record, target_root=target_root, as_child=as_child)
    if not fresh.executable:
        why = "；".join(b.reason for b in fresh.blockers) or "计划不可执行"
        raise ExecutionRefused(f"计划已失效：{why}")
    if fresh.action != "redirect" or not fresh.env_var:
        raise ExecutionRefused(f"该目录的计划动作是 {fresh.action}，不是 redirect")
    src, var, target = fresh.path, fresh.env_var, fresh.target
    _guard(src)

    op = manifest.new_operation("redirect", fresh.to_dict())
    op["env_var"] = var
    op["env_new"] = target
    op["env_previous"] = envvar.get_user_var(var)   # None = 原本不存在
    if dry_run:
        op["status"] = "dry_run"
        return op

    op["recycle_intent"] = _recycle_dst(src, op)   # 理由同 execute_cleanup
    manifest.save(op)
    try:
        Path(target).mkdir(parents=True, exist_ok=True)
        manifest.add_step(op, f"created_target:{target}")

        envvar.set_user_var(var, target)
        manifest.add_step(op, f"set_env:{var}")
        if envvar.get_user_var(var) != target:
            raise ExecutionFailed(f"环境变量写入后读回不一致：{var}")

        dst = _move_to_recycle(src, op)
        op["recycled_to"] = dst
        manifest.add_step(op, f"moved_to_recycle:{dst}")
        op["freed"] = fsdedup.measure(dst).to_dict()
        manifest.mark(op, "done")
    except ExecutionFailed as e:
        manifest.mark(op, "failed", failure=f"{e}\n（环境变量可能已改，请用回滚恢复）")
        raise
    except Exception as e:
        manifest.mark(op, "failed", failure=f"{type(e).__name__}: {e}")
        raise ExecutionFailed(str(e)) from e
    return op


# ------------------------------------------------------------------ junction 迁移
def _dir_stats(path: str) -> tuple[int, int]:
    """(文件数, 字节数)。用于复制前后比对，确认搬运没缺斤少两。"""
    files = total = 0
    for root, dirs, names in os.walk(path):
        # 不跟进重解析点，否则会把链接目标的内容重复计入。Windows junction
        # 不是 os.path.islink()，必须同时检查 reparse tag。
        dirs[:] = [d for d in dirs
                   if not _is_junction(os.path.join(root, d))]
        for n in names:
            p = os.path.join(root, n)
            if _is_junction(p):
                continue
            try:
                total += os.path.getsize(p)
                files += 1
            except OSError:
                # 读不到大小的（权限/占用）单独计数，仍算一个文件
                files += 1
    return files, total


def _is_junction(path: str) -> bool:
    """判断是否 junction / 符号链接。os.path.islink 在 Windows 上对 junction 返回
    False，必须看 reparse tag。"""
    if os.path.islink(path):
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return False
    tag = getattr(st, "st_reparse_tag", 0)
    return tag in (0xA0000003, 0xA000000C)   # MOUNT_POINT / SYMLINK


def execute_junction(record: dict, target_root: str | None = None,
                     dry_run: bool = False, *, as_child: bool = False) -> dict:
    r"""把目录搬到别的盘，原位留一个 junction 指过去。

    给"不能删、又没有官方重定向机制"的目录用——数据一字不少地留着，只是不再占 C 盘。

    步骤顺序是这个函数的全部要害：

        复制到新盘 → 比对文件数与字节数 → 源目录移入回收区 → 原位建 junction → 回读校验

    为什么是这个顺序：

    * **先复制后动源。** 复制失败时源目录一动没动，什么都不用还原。
    * **校验通过才动源。** 数据在两个地方都完整存在的那一刻，才是唯一可以安全动源的时机。
    * **源目录移入回收区，绝不直接删。** 这样"删源"这一步本身也是可回滚的；30 天内
      随时能把原始数据放回去。junction 建失败也只是回收区多一份数据，不丢东西。
    * **建 junction 必须在源腾空之后。** junction 要占用源那个路径名，源还在就建不上。

    用 `_winapi.CreateJunction` 而不是 `cmd /c mklink /J`：后者要把用户路径拼进命令行，
    凭空多一个命令注入面，且失败只能靠解析中文输出判断。
    """
    import _winapi

    fresh = plan_for(record, target_root=target_root, as_child=as_child)
    if not fresh.executable:
        why = "；".join(b.reason for b in fresh.blockers) or "计划不可执行"
        raise ExecutionRefused(f"计划已失效：{why}")
    if fresh.action != "junction":
        raise ExecutionRefused(f"该目录的计划动作是 {fresh.action}，不是 junction")

    src, target = fresh.path, fresh.target
    _guard(src)
    if not target:
        raise ExecutionRefused("计划里没有目标路径")
    if _is_junction(src):
        raise ExecutionRefused(f"源目录已经是 junction，不必再搬：{src}")

    # 目标不能落在源目录里面，否则复制会自己套自己
    tl, sl = os.path.abspath(target).lower(), os.path.abspath(src).lower()
    if tl == sl or tl.startswith(sl + os.sep):
        raise ExecutionRefused(f"目标目录在源目录内部，会无限递归：{target}")
    if os.path.exists(target) and os.listdir(target):
        raise ExecutionRefused(f"目标目录已存在且非空：{target}")

    src_files, src_bytes = _dir_stats(src)
    op = manifest.new_operation("junction", fresh.to_dict())
    op["junction_target"] = target
    op["source_files"] = src_files
    op["source_bytes"] = src_bytes
    if dry_run:
        op["status"] = "dry_run"
        return op

    # 理由同 execute_cleanup。junction 搬的是**不可删的真实用户数据**，这里漏记的后果
    # 比清理缓存重得多：副本会成为台账不认识的孤儿，回滚找不到、自动清理也碰不到。
    op["recycle_intent"] = _recycle_dst(src, op)
    manifest.save(op)                            # 先落盘，再动文件
    try:
        # 1. 复制到新盘。按 inode 重建硬链接，重解析点不跟进，避免把共享内容拆开
        # 或把链接目标整棵复制进来。
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        fsdedup.copytree_keep_links(src, target)
        manifest.add_step(op, f"copied_to:{target}")

        # 2. 比对。不一致就地中止——此时源目录还没动过
        dst_files, dst_bytes = _dir_stats(target)
        if (dst_files, dst_bytes) != (src_files, src_bytes):
            raise ExecutionFailed(
                f"复制后不一致：源 {src_files} 个文件/{src_bytes} 字节，"
                f"目标 {dst_files} 个文件/{dst_bytes} 字节。源目录未做任何改动")
        manifest.add_step(op, f"verified:{dst_files}files/{dst_bytes}bytes")

        # 3. 源目录移入回收区（不是删除），腾出路径名给 junction
        recycled = _move_to_recycle(src, op)
        op["recycled_to"] = recycled
        manifest.add_step(op, f"moved_to_recycle:{recycled}")
        if os.path.exists(src):
            raise ExecutionFailed(f"源目录移走后仍存在：{src}")

        # 4. 原位建 junction
        _winapi.CreateJunction(target, src)
        manifest.add_step(op, f"junction_created:{src}->{target}")

        # 5. 回读校验：既要是重解析点，也要真能读到内容
        if not _is_junction(src):
            raise ExecutionFailed(f"junction 建完但回读不是重解析点：{src}")
        if not os.path.isdir(src):
            raise ExecutionFailed(f"junction 建完但访问不到：{src}")
        through = os.listdir(src)
        direct = os.listdir(target)
        if sorted(through) != sorted(direct):
            raise ExecutionFailed(
                f"透过 junction 看到的内容与目标不一致（{len(through)} vs {len(direct)} 项）")
        manifest.mark(op, "done")
    except ExecutionFailed as e:
        manifest.mark(op, "failed", failure=str(e))
        raise
    except Exception as e:
        manifest.mark(op, "failed", failure=f"{type(e).__name__}: {e}")
        raise ExecutionFailed(str(e)) from e
    return op


# ------------------------------------------------------------------ 回滚
def recovery_state(op: dict) -> tuple[bool, str]:
    """Inspect the live system before offering rollback for an interrupted action."""
    if op.get("rollback_supported") is False:
        return False, "该操作不支持自动恢复"
    if manifest.is_purged(op):
        return False, "回收区数据已永久删除"
    if op.get("status") not in {"done", "failed", "planned"}:
        return False, f"状态为 {op.get('status')}，无需恢复"
    if op.get("action") not in {
        "cleanup", "redirect", "junction", "uninstall_residue_cleanup",
    }:
        return False, "不是文件迁移类操作"

    src = op.get("source_path")
    dst = op.get("recycled_to") or op.get("recycle_intent")
    dst_exists = bool(dst and os.path.exists(dst))
    source_exists = bool(src and os.path.exists(src))

    if dst_exists and source_exists and not (
        op.get("action") == "junction" and _is_junction(src)
    ):
        return False, "原路径已重新存在，且回收区仍有旧数据，需先人工核对"

    if op.get("action") == "redirect" and op.get("env_var"):
        current = envvar.get_user_var(op["env_var"])
        if current != op.get("env_new"):
            return False, "环境变量已被其它程序修改，拒绝覆盖当前值"
        if dst_exists:
            return True, "检测到已改环境变量和回收区数据，可恢复到操作前"
        return True, "检测到环境变量已改，可恢复原值"

    if op.get("action") == "junction":
        target = op.get("junction_target")
        target_exists = bool(target and os.path.isdir(target))
        if src and _is_junction(src) and not dst_exists:
            return False, "原位链接仍在，但原始回收副本缺失，不能自动恢复"
        if dst_exists or target_exists:
            return True, "检测到迁移副本或原位链接，可恢复到操作前"
        return False, "未发现迁移产生的磁盘变化"

    if dst_exists:
        return True, "检测到回收区数据，可移回原位置"
    return False, "未发现已搬走的数据，原路径未发生可恢复变化"


def rollback(op_id: str) -> dict:
    """把一次操作还原到操作前。顺序与执行相反：先还文件，再还变量。

    为什么这个顺序：若先还变量后还文件，中间态是"变量指回 C 盘但缓存还在回收区"——
    此时软件启动会在 C 盘重建缓存，随后把回收区的数据移回去就会撞上已存在的目录。
    先还文件则中间态是"缓存已回原位、变量还指着新盘"，软件最坏情况是去新位置重下，
    不会冲突。
    """
    op = manifest.find(op_id)
    if not op:
        raise ExecutionRefused(f"找不到操作记录：{op_id}")
    if op.get("rollback_supported") is False:
        raise ExecutionRefused("该操作由软件自己的卸载器执行，不支持自动回滚")
    if op.get("action") == "startup_disable":
        raise ExecutionRefused("启动项操作请从启动管理页面恢复")
    if op.get("status") == "rolled_back":
        raise ExecutionRefused("该操作已经回滚过了")
    can_recover, recovery_reason = recovery_state(op)
    if not can_recover:
        raise ExecutionRefused(recovery_reason)

    src = op.get("source_path")
    # recycle_intent 在移动前就已落盘。进程若恰好在移动完成、写回 recycled_to
    # 之前崩溃，恢复仍必须按这个意图寻找回收区里的数据。
    dst = op.get("recycled_to") or op.get("recycle_intent")

    # 0. junction 操作要先摘掉链接，再谈还原。
    #    不摘的话 os.path.exists(src) 为真（它指向新盘那份），下面会误判成"原路径已
    #    重新存在"而中止；更糟的是若绕过判断去移动，数据会**穿过 junction 写进目标
    #    目录**，等于把备份塞回它自己里面。
    #    摘链接用 os.rmdir：对 junction 只删链接本身，不动目标内容（rmtree 会删穿）。
    if op.get("action") == "junction" and src and _is_junction(src):
        os.rmdir(src)
        manifest.add_step(op, f"junction_removed:{src}")

    # 1. 先把数据移回原位
    if dst and os.path.exists(dst):
        if src and os.path.exists(src):
            raise ExecutionRefused(
                f"原路径已重新存在（软件可能已重建缓存）：{src}。"
                f"回滚会覆盖它，故中止；数据仍在 {dst}")
        Path(src).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(dst, src)
        except OSError:
            shutil.move(dst, src)
        manifest.add_step(op, f"restored:{src}")
        _remove_empty_shell(dst)

    # 2. 再还环境变量
    var = op.get("env_var")
    if var:
        prev = op.get("env_previous")
        if prev is None:
            envvar.delete_user_var(var)
            manifest.add_step(op, f"env_deleted:{var}")
        else:
            envvar.set_user_var(var, prev)
            manifest.add_step(op, f"env_restored:{var}")

    # 3. junction 操作：原始数据已回到原位，新盘上那份副本成了多余，清掉它。
    #
    #    这是本模块第二处 rmtree（模块开头的规矩说只在"清空过期回收区"出现），所以
    #    条件写死得很严：**必须先确认原位数据真的回来了，且文件数与字节数与操作前记录
    #    的一致**，否则宁可留着副本占空间，也不删。留下来的话界面会把路径报出来。
    #    删的是"已确认冗余的副本"，不是用户仅有的一份数据——这与规矩的用意一致。
    target = op.get("junction_target")
    leftover = None
    if op.get("action") == "junction" and target and os.path.isdir(target):
        restored_ok = False
        if src and os.path.isdir(src) and not _is_junction(src):
            files, size = _dir_stats(src)
            restored_ok = (files == op.get("source_files")
                           and size == op.get("source_bytes"))
        if restored_ok:
            shutil.rmtree(target, ignore_errors=False)
            manifest.add_step(op, f"target_copy_removed:{target}")
        else:
            leftover = target
            manifest.add_step(op, f"target_copy_kept:{target}")

    # 保留 recycle_intent 作为审计信息；落点不存在时 recycle_entries 会自然忽略它。
    manifest.mark(op, "rolled_back", recycled_to=None)
    if leftover:
        op["leftover_copy"] = leftover
    return op


# ---------------------------------------------------- 回收区：过期才真删
def purge_expired(force_ids: list[str] | None = None) -> dict:
    """清空已过回收期的回收区数据。**这是本模块唯一真删数据的地方。**

    force_ids 用于用户在界面上明确点了"立刻永久删除"的场景；其余一律要求已过期。
    """
    purged, skipped = [], []
    force = set(force_ids or [])
    for op in manifest.list_operations():
        dst = op.get("recycled_to")
        if not dst:
            # 搬运中途失败留下的数据：台账只记了意图。不认它的话这些字节永远清不掉
            # （那次故障就留下 3.22 GiB 谁也碰不到的副本）。仍受回收期保护，
            # 且只在源目录已经不完整时才谈清理——所以这里不放宽 is_expired 的要求。
            dst = op.get("recycle_intent")
        if not dst or not os.path.exists(dst):
            continue
        if op["id"] not in force and not manifest.is_expired(op):
            skipped.append({"id": op["id"], "reason": "仍在回收期内",
                            "until": op.get("recoverable_until")})
            continue
        try:
            shutil.rmtree(dst, ignore_errors=False)
        except OSError as e:
            skipped.append({"id": op["id"], "reason": f"删除失败：{e}"})
            continue
        # 与 rollback 同一处理：数据删掉后，那个以操作 id 命名的壳子就空了。
        # 只删空目录，非空说明状态与预期不符，留着让人看见。
        _remove_empty_shell(dst)
        manifest.mark(op, op.get("status", "done"), recycled_to=None,
                      purged_at=datetime.now(timezone.utc)
                      .isoformat(timespec="seconds"))
        purged.append(op["id"])
    purged_o, skipped_o = _purge_orphans(force, {op["id"] for op
                                                 in manifest.list_operations()})
    return {"purged": purged + purged_o, "skipped": skipped + skipped_o}


def _purge_orphans(force: set[str], known_ids: set[str]) -> tuple[list, list]:
    r"""删掉回收区里**没有任何台账记录认领**的目录。

    **只在用户明确点名时删**（`force_ids`），永不自动清理：既然连它是什么操作留下的都
    说不出来，就不该替用户决定它可以消失。自动清理只处理有台账、且已过回收期的。

    为什么必须有这条路：`manifest.recycle_entries()` 现在会把孤儿目录列出来（否则那些
    字节界面看不见、也永远清不掉——真实故障留下过 3.22 GiB 这样的副本）。但列出来却删不掉
    是个死胡同：界面给了「永久删除」按钮，而 `purge_expired` 遍历的是台账记录，纯孤儿
    压根不在其中，于是按钮报一句没有信息的"删除失败"。显形与可清理必须成对出现。
    """
    purged, skipped = [], []
    # 纯优化：force 为空时省掉一次目录遍历。**不是安全防线**——真正拦住"没点名就删"的是
    # 下面那句 `shell.name not in force`。变异测试验过：单独删掉这一行行为不变（测试全绿），
    # 两句一起删才有两条用例变红。别把它当保障。
    if not force:
        return purged, skipped
    root = manifest.recycle_dir()
    try:
        shells = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return purged, skipped
    for shell in shells:
        if shell.name in known_ids or shell.name not in force:
            continue
        try:
            shutil.rmtree(str(shell), ignore_errors=False)
        except OSError as e:
            skipped.append({"id": shell.name, "reason": f"删除失败：{e}"})
            continue
        purged.append(shell.name)
    return purged, skipped
