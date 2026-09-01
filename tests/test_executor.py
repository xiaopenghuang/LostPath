r"""执行器回归。**这是唯一会真写系统的模块，所以测试最厚。**

全部在 tmp_path 造的合成目录上跑，数据目录由 conftest 的 autouse fixture 指到临时
目录。环境变量测试用专用前缀 `LOSTPATH_TEST_` 的变量名，绝不碰真实变量（真实变量
一旦被测试写坏，用户的工具链会莫名找不到缓存）。

守的核心是三条：先写记录再动文件、删除一律先入回收区、回滚真能把系统还原。
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lostpath.act import executor, manifest, planner
from lostpath.act import envvar

TEST_VAR = "LOSTPATH_TEST_CACHE_DIR"


@pytest.fixture
def cache_dir(tmp_path):
    """造一个"可再生缓存"目录，带真实文件以便校验数据没丢。"""
    d = tmp_path / "toolcache"
    (d / "sub").mkdir(parents=True)
    (d / "a.bin").write_bytes(b"a" * 1024)
    (d / "sub" / "b.bin").write_bytes(b"b" * 2048)
    return d


def record_for(path, **kw):
    base = {
        "path": str(path), "name": os.path.basename(str(path)),
        "size": 200 * 2**20, "files": 2, "cat": "可再生缓存",
        "owner_kind": "toolchain", "conf": 0.9, "redirect": None,
        "owner": "某工具", "why": "测试",
    }
    base.update(kw)
    return base


def tree(path):
    """目录内容指纹，用于证明数据在移动/回滚后没被改动。"""
    out = {}
    for root, _dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            out[os.path.relpath(p, path)] = open(p, "rb").read()
    return out


# ============================================================ 清理
def test_cleanup_moves_to_recycle_not_delete(cache_dir):
    """删除一律先移入回收区。原地 rmtree 是不可接受的。"""
    before = tree(cache_dir)
    op = executor.execute_cleanup(record_for(cache_dir))

    assert op["status"] == "done"
    assert not cache_dir.exists(), "源目录应已移走"
    assert op["recycled_to"] and os.path.isdir(op["recycled_to"])
    assert tree(op["recycled_to"]) == before, "回收区里的数据与原数据不一致"


def test_cleanup_writes_manifest_before_touching_files(cache_dir, monkeypatch):
    """先写记录再动文件。反序的话崩在中间就是"文件动了但无记录"。"""
    order = []
    real_save = manifest.save
    real_rename = os.rename

    def spy_save(op):
        order.append(("save", op.get("status")))
        return real_save(op)

    def spy_rename(a, b):
        order.append(("rename", None))
        return real_rename(a, b)

    monkeypatch.setattr(manifest, "save", spy_save)
    monkeypatch.setattr(os, "rename", spy_rename)
    executor.execute_cleanup(record_for(cache_dir))

    kinds = [k for k, _ in order]
    assert kinds[0] == "save", f"第一个动作不是写记录：{order}"
    assert "rename" in kinds
    assert kinds.index("save") < kinds.index("rename")


def test_manifest_on_disk_knows_destination_before_move(cache_dir, monkeypatch):
    r"""动文件那一刻，**盘上的**台账必须已经写明数据要去哪。

    上一条只验了 save 早于 rename，而原实现确实满足它——但 `recycled_to` 是移动**之后**
    才填的，所以盘上那份记录只说"打算清理某目录"，没说数据会出现在哪。真实事故正是踩
    在这个缝里：搬运中途失败，回收区实存 3.22 GiB 而台账 `recycled_to=None`，而
    `recycle_entries()` 与 `purge_expired()` 都只认这个字段，那些字节因此界面看不见、
    30 天后也不会被清掉，只有翻文件系统才能发现。

    所以这条在 rename 发生的瞬间去读盘上的 json，而不是看内存里的 op。
    """
    seen = {}
    real_rename = os.rename

    def spy_rename(a, b):
        # 此刻磁盘上的台账是什么样子？这才是崩溃后唯一还在的东西
        ops = manifest.list_operations()
        seen["count"] = len(ops)
        if ops:
            seen["intent"] = ops[0].get("recycle_intent")
            seen["recycled_to"] = ops[0].get("recycled_to")
        return real_rename(a, b)

    monkeypatch.setattr(os, "rename", spy_rename)
    op = executor.execute_cleanup(record_for(cache_dir))

    assert seen.get("count") == 1, "动文件时台账应已落盘"
    assert seen.get("intent"), (
        "动文件时盘上的记录没有 recycle_intent——崩在这一步就无从知道数据在哪")
    assert os.path.normcase(seen["intent"]) == os.path.normcase(op["recycled_to"]), (
        f"预先记的目标 {seen['intent']} 与实际落点 {op['recycled_to']} 不一致，"
        f"那这个字段就是误导")


def test_interrupted_move_is_still_discoverable(cache_dir, monkeypatch):
    """搬运中途失败后，回收区里的数据仍要能被清单发现，并标明"没搬完"。

    否则就回到那次事故的状态：盘上有数据、界面说回收区是空的。
    """
    real_rename = os.rename

    def half_done(a, b):
        # 模拟"数据已经到了目标，但随后崩了"——真实事故里 copytree 已完成、rmtree 失败
        real_rename(a, b)
        raise OSError("模拟搬运后崩溃")

    monkeypatch.setattr(os, "rename", half_done)
    with pytest.raises(executor.ExecutionFailed):
        executor.execute_cleanup(record_for(cache_dir))

    entries = manifest.recycle_entries()
    assert len(entries) == 1, (
        f"回收区里有数据却没被清单认领（实得 {len(entries)} 条）——这正是那 3.22 GiB "
        f"隐形的原因")
    e = entries[0]
    assert e["unconfirmed"] is True, "必须标明这次搬运没有完成，不能假装是正常回收"
    assert e["files"] == 2 and e["size"] == 1024 + 2048, "体积要按磁盘实测给出"
    # 必须是**台账认领**的，不是靠兜底扫盘捡回来的：认领的能带上原路径与回收期，
    # 扫出来的只有一个目录名。只断言 unconfirmed 的话两者都满足，分不出来。
    op_id = manifest.list_operations()[0]["id"]
    assert e["id"] == op_id, f"该由台账记录认领，实得 id={e['id']}（期望 {op_id}）"
    assert e["status"] != "orphan", "有台账记录时不该退化成孤儿条目"
    assert e["source_path"], "认领的条目必须带原路径，否则用户不知道这是哪来的数据"


def test_rollback_uses_recycle_intent_when_recycled_to_was_not_written(cache_dir):
    """移动完成后若进程在写 recycled_to 前崩溃，回滚仍能按意图找回数据。"""
    before = tree(cache_dir)
    op = executor.execute_cleanup(record_for(cache_dir))
    saved = manifest.find(op["id"])
    saved["recycle_intent"] = saved["recycled_to"]
    saved["recycled_to"] = None
    saved["status"] = "failed"
    manifest.save(saved)

    restored = executor.rollback(op["id"])

    assert cache_dir.is_dir() and tree(cache_dir) == before
    assert restored["status"] == "rolled_back"
    assert manifest.find(op["id"])["recycle_intent"]


def test_planned_operation_can_recover_when_move_finished_before_crash(cache_dir):
    """A crash after rename but before the final status write must remain recoverable."""
    before = tree(cache_dir)
    op = executor.execute_cleanup(record_for(cache_dir))
    saved = manifest.find(op["id"])
    saved["status"] = "planned"
    saved["recycled_to"] = None
    manifest.save(saved)

    can_recover, _reason = executor.recovery_state(saved)
    assert can_recover is True
    executor.rollback(op["id"])

    assert cache_dir.is_dir() and tree(cache_dir) == before


def test_locked_dir_is_not_silently_copied(cache_dir, monkeypatch):
    r"""同盘改名失败（被占用）时**不许**退化成复制。

    原实现是 `except OSError` 一律回退 `shutil.move`，注释写着"跨盘"。但实测同盘失败
    远比跨盘常见：目录里只要有一个文件被打开、或有进程的工作目录在其中，`os.rename`
    就以 errno=13/winerror=5 失败。那次事故就是这样——回收区与源目录同在 C 盘，本该
    瞬间改名，却落进复制分支把 0.31 GiB 复制成 1.63 GiB，随后 rmtree 撞上被加载的 DLL
    失败，留下三份数据。errno 13 与 18 能干净区分，就不该混为一谈。
    """
    import errno as _errno

    copied = []

    def locked_rename(a, b):
        raise PermissionError(_errno.EACCES, "拒绝访问", str(a))

    monkeypatch.setattr(os, "rename", locked_rename)
    monkeypatch.setattr("shutil.copytree",
                        lambda *a, **k: copied.append(a) or None)
    monkeypatch.setattr("shutil.move", lambda *a, **k: copied.append(a) or None)

    with pytest.raises(executor.ExecutionFailed) as e:
        executor.execute_cleanup(record_for(cache_dir))

    assert not copied, "被占用时不该走复制，那会把硬链接拆开并可能多占几倍空间"
    assert cache_dir.exists(), "失败后源目录必须一动没动"
    msg = str(e.value)
    assert "占用" in msg or "打开" in msg, f"要说清是被占用而非跨盘：{msg}"
    ops = manifest.list_operations()
    assert ops[0]["status"] == "failed"
    assert "errno" in (ops[0]["failure"] or ""), "台账要留下系统给的错误码，便于排查"


def test_cross_volume_move_preserves_hardlinks(cache_dir, monkeypatch):
    """真跨卷（EXDEV）时才复制，且必须用不拆硬链接的实现。"""
    import errno as _errno

    used = []
    real_keep = executor.fsdedup.copytree_keep_links

    def spy_keep(src, dst):
        used.append("keep_links")
        return real_keep(src, dst)

    def exdev_rename(a, b):
        raise OSError(_errno.EXDEV, "跨盘", str(a))

    monkeypatch.setattr(os, "rename", exdev_rename)
    monkeypatch.setattr(executor.fsdedup, "copytree_keep_links", spy_keep)
    monkeypatch.setattr("shutil.copytree",
                        lambda *a, **k: pytest.fail("不该用 shutil.copytree，它拆硬链接"))

    op = executor.execute_cleanup(record_for(cache_dir))
    assert used == ["keep_links"], "跨卷复制必须走保留硬链接的实现"
    assert not cache_dir.exists(), "跨卷搬完源目录应已删除"
    assert os.path.isdir(op["recycled_to"])
    assert op["status"] == "done"


def test_orphan_recycle_dir_is_reported(tmp_path):
    """回收区里没有任何台账记录认领的目录，也必须被清单发现。

    台账文件本身丢了或写坏时，只剩这条路能发现那些字节。
    """
    orphan = manifest.recycle_dir() / "deadbeefcafe" / "somecache"
    orphan.mkdir(parents=True)
    (orphan / "x.bin").write_bytes(b"x" * 4321)

    entries = manifest.recycle_entries()
    ids = {e["id"]: e for e in entries}
    assert "deadbeefcafe" in ids, f"孤儿目录没被发现：{list(ids)}"
    got = ids["deadbeefcafe"]
    assert got["status"] == "orphan"
    assert got["unconfirmed"] is True
    assert got["size"] == 4321


def test_cleanup_manifest_is_readable_and_complete(cache_dir):
    op = executor.execute_cleanup(record_for(cache_dir))
    ops = manifest.list_operations()
    assert len(ops) == 1
    saved = ops[0]
    assert saved["id"] == op["id"]
    assert saved["action"] == "cleanup"
    assert saved["source_path"] == str(cache_dir)
    assert saved["recoverable_until"], "缺可恢复期，用户不知道什么时候会真删"
    assert saved["plan"], "缺计划原文，出问题时无法还原当时的判断依据"
    # 记录必须是合法 JSON 且能独立读回
    with open(saved["manifest_path"], encoding="utf-8-sig") as f:
        json.load(f)


def test_cleanup_dry_run_touches_nothing(cache_dir):
    before = tree(cache_dir)
    op = executor.execute_cleanup(record_for(cache_dir), dry_run=True)
    assert op["status"] == "dry_run"
    assert cache_dir.exists() and tree(cache_dir) == before
    assert not manifest.list_operations(), "dry-run 不该留下操作记录"


def test_cleanup_refuses_when_plan_invalid(tmp_path):
    """计划失效（这里是性质不符）时拒绝执行，且不留痕。"""
    d = tmp_path / "userdata"
    d.mkdir()
    with pytest.raises(executor.ExecutionRefused):
        executor.execute_cleanup(record_for(d, cat="用户数据"))
    assert not manifest.list_operations()


def test_cleanup_revalidates_against_disk(cache_dir):
    """计划可能是几分钟前算的：执行前要拿当下实况重算。"""
    rec = record_for(cache_dir)
    import shutil
    shutil.rmtree(cache_dir)          # 目录在"出计划之后"消失
    with pytest.raises(executor.ExecutionRefused) as e:
        executor.execute_cleanup(rec)
    assert "失效" in str(e.value) or "不存在" in str(e.value)


# ============================================================ 安全闸
@pytest.mark.parametrize("bad", [
    r"C:\Windows",
    r"C:\Windows\System32",
    r"C:\Program Files",
    r"C:\Program Files (x86)\something",
])
def test_guard_refuses_system_directories(bad):
    """即使计划这么说也不许动系统目录。这是最后一道闸。"""
    with pytest.raises(executor.ExecutionRefused) as e:
        executor._guard(bad)
    assert "系统目录" in str(e.value)


@pytest.mark.parametrize("root", ["C:\\", "D:\\", "E:"])
def test_guard_refuses_drive_roots(root):
    with pytest.raises(executor.ExecutionRefused):
        executor._guard(root)


def test_guard_allows_normal_cache_dir(cache_dir):
    executor._guard(str(cache_dir))   # 不该抛


# ============================================================ 回滚：清理
def test_rollback_restores_cleaned_directory(cache_dir):
    before = tree(cache_dir)
    op = executor.execute_cleanup(record_for(cache_dir))
    assert not cache_dir.exists()

    executor.rollback(op["id"])

    assert cache_dir.is_dir(), "回滚没把目录还回来"
    assert tree(cache_dir) == before, "回滚后数据与原始不一致"
    assert manifest.find(op["id"])["status"] == "rolled_back"


def test_rollback_cleans_empty_recycle_shell(cache_dir):
    """回滚后回收区不该留下以操作 id 命名的空壳。

    真机验证时发现的：数据确实还回原位了，但回收区里剩一个空目录。回滚一次攒一个，
    用久了回收区全是空壳，看着像"还有东西没清干净"，与"回收区是空的"这句话矛盾。
    """
    op = executor.execute_cleanup(record_for(cache_dir))
    shell = Path(op["recycled_to"]).parent
    assert shell.is_dir(), "前提不成立：回收区里没有这个操作的目录"

    executor.rollback(op["id"])

    assert not shell.exists(), f"回滚后回收区仍留着空壳：{shell}"
    assert not any(manifest.recycle_dir().iterdir()), "回收区不干净"


def test_rollback_keeps_recycle_shell_when_not_empty(cache_dir):
    """壳子里还有别的东西时不许删——那说明状态与预期不符，留着让人看见。"""
    op = executor.execute_cleanup(record_for(cache_dir))
    shell = Path(op["recycled_to"]).parent
    (shell / "unexpected.bin").write_bytes(b"someone else's data")

    executor.rollback(op["id"])

    assert shell.is_dir(), "壳子非空却被删了"
    assert (shell / "unexpected.bin").exists(), "把别的数据删掉了"


def test_rollback_twice_is_refused(cache_dir):
    op = executor.execute_cleanup(record_for(cache_dir))
    executor.rollback(op["id"])
    with pytest.raises(executor.ExecutionRefused):
        executor.rollback(op["id"])


def test_rollback_refuses_when_source_recreated(cache_dir):
    """软件已在原位置重建了缓存时不能盲目覆盖。

    这是真实场景：清理后软件启动、重新下载了缓存，此时回滚若直接移回就会覆盖新数据。
    应当中止并告诉用户数据还在回收区。
    """
    op = executor.execute_cleanup(record_for(cache_dir))
    cache_dir.mkdir(parents=True)
    (cache_dir / "new.bin").write_bytes(b"new")

    with pytest.raises(executor.ExecutionRefused) as e:
        executor.rollback(op["id"])
    assert "已重新存在" in str(e.value)
    assert (cache_dir / "new.bin").exists(), "新数据被覆盖了"
    assert os.path.isdir(op["recycled_to"]), "旧数据应仍在回收区"


def test_rollback_unknown_id_refused():
    with pytest.raises(executor.ExecutionRefused):
        executor.rollback("nonexistent")


# ============================================================ 重定向
@pytest.fixture
def clean_test_var():
    """确保测试前后这个变量都不存在，绝不影响真实环境。"""
    envvar.delete_user_var(TEST_VAR)
    yield
    envvar.delete_user_var(TEST_VAR)


def test_env_var_roundtrip_and_absence_is_distinguishable(clean_test_var):
    """None（不存在）与 ""（空字符串）必须能区分——回滚正确性依赖这个。"""
    assert envvar.get_user_var(TEST_VAR) is None
    envvar.set_user_var(TEST_VAR, "")
    assert envvar.get_user_var(TEST_VAR) == ""
    envvar.set_user_var(TEST_VAR, r"E:\somewhere")
    assert envvar.get_user_var(TEST_VAR) == r"E:\somewhere"
    assert envvar.delete_user_var(TEST_VAR) is True
    assert envvar.get_user_var(TEST_VAR) is None
    assert envvar.delete_user_var(TEST_VAR) is False, "删不存在的变量应返回 False"


def test_redirect_sets_var_and_recycles_old_cache(cache_dir, tmp_path,
                                                  monkeypatch, clean_test_var):
    monkeypatch.setattr(planner, "drive_free_bytes", lambda p: 500 * 2**30)
    monkeypatch.setitem(
        __import__("lostpath.act.redirect", fromlist=["MECHANISMS"]).MECHANISMS,
        "TESTVAR", {"kind": "env", "var": TEST_VAR, "note": "测试用"})
    before = tree(cache_dir)
    target_root = str(tmp_path / "store")

    op = executor.execute_redirect(
        record_for(cache_dir, redirect="TESTVAR"), target_root=target_root)

    assert op["status"] == "done"
    assert envvar.get_user_var(TEST_VAR) == op["env_new"]
    assert os.path.isdir(op["env_new"]), "目标目录应已创建"
    assert not cache_dir.exists(), "旧缓存应已移入回收区"
    assert tree(op["recycled_to"]) == before
    assert op["env_previous"] is None, "原本不存在，应记为 None 以便回滚时删除"


def test_redirect_rollback_deletes_var_when_absent_before(
        cache_dir, tmp_path, monkeypatch, clean_test_var):
    """原本没有该变量时，回滚必须删掉它而不是留个空值。"""
    monkeypatch.setattr(planner, "drive_free_bytes", lambda p: 500 * 2**30)
    monkeypatch.setitem(
        __import__("lostpath.act.redirect", fromlist=["MECHANISMS"]).MECHANISMS,
        "TESTVAR", {"kind": "env", "var": TEST_VAR, "note": "测试用"})
    before = tree(cache_dir)
    op = executor.execute_redirect(record_for(cache_dir, redirect="TESTVAR"),
                                   target_root=str(tmp_path / "store"))

    executor.rollback(op["id"])

    assert envvar.get_user_var(TEST_VAR) is None, "回滚后变量应被删除"
    assert cache_dir.is_dir() and tree(cache_dir) == before


def test_redirect_rollback_restores_previous_value(
        cache_dir, tmp_path, monkeypatch, clean_test_var):
    """原本有值时，回滚要写回原值而不是删掉。"""
    monkeypatch.setattr(planner, "drive_free_bytes", lambda p: 500 * 2**30)
    monkeypatch.setitem(
        __import__("lostpath.act.redirect", fromlist=["MECHANISMS"]).MECHANISMS,
        "TESTVAR", {"kind": "env", "var": TEST_VAR, "note": "测试用"})
    envvar.set_user_var(TEST_VAR, r"D:\old-location")

    op = executor.execute_redirect(record_for(cache_dir, redirect="TESTVAR"),
                                   target_root=str(tmp_path / "store"))
    assert op["env_previous"] == r"D:\old-location"

    executor.rollback(op["id"])
    assert envvar.get_user_var(TEST_VAR) == r"D:\old-location"


def test_redirect_rollback_refuses_external_environment_change(
        cache_dir, tmp_path, monkeypatch, clean_test_var):
    """Recovery must not overwrite a value changed after LostPath's operation."""
    monkeypatch.setattr(planner, "drive_free_bytes", lambda p: 500 * 2**30)
    monkeypatch.setitem(
        __import__("lostpath.act.redirect", fromlist=["MECHANISMS"]).MECHANISMS,
        "TESTVAR", {"kind": "env", "var": TEST_VAR, "note": "测试用"})
    op = executor.execute_redirect(record_for(cache_dir, redirect="TESTVAR"),
                                   target_root=str(tmp_path / "store"))
    envvar.set_user_var(TEST_VAR, r"G:\changed-externally")

    with pytest.raises(executor.ExecutionRefused, match="其它程序修改"):
        executor.rollback(op["id"])

    assert not cache_dir.exists(), "拒绝恢复前不应先移动文件"
    assert os.path.isdir(op["recycled_to"])
    assert envvar.get_user_var(TEST_VAR) == r"G:\changed-externally"


def test_rollback_restores_files_before_env(cache_dir, tmp_path, monkeypatch,
                                            clean_test_var):
    """回滚顺序必须是先还文件、再还变量。

    反序的中间态是"变量指回 C 盘但缓存还在回收区"，此时软件启动会在 C 盘重建缓存，
    随后移回就会撞上已存在的目录。
    """
    monkeypatch.setattr(planner, "drive_free_bytes", lambda p: 500 * 2**30)
    monkeypatch.setitem(
        __import__("lostpath.act.redirect", fromlist=["MECHANISMS"]).MECHANISMS,
        "TESTVAR", {"kind": "env", "var": TEST_VAR, "note": "测试用"})
    op = executor.execute_redirect(record_for(cache_dir, redirect="TESTVAR"),
                                   target_root=str(tmp_path / "store"))

    order = []
    real_rename = os.rename
    real_del = envvar.delete_user_var

    monkeypatch.setattr(os, "rename",
                        lambda a, b: (order.append("file"), real_rename(a, b))[1])
    monkeypatch.setattr(envvar, "delete_user_var",
                        lambda n: (order.append("env"), real_del(n))[1])
    executor.rollback(op["id"])

    assert order[:2] == ["file", "env"], f"回滚顺序错误：{order}"


# ============================================================ 回收区真删
def test_purge_refuses_within_recovery_window(cache_dir):
    """回收期内不许真删，这是"可回滚"的实质保障。"""
    op = executor.execute_cleanup(record_for(cache_dir))
    res = executor.purge_expired()
    assert res["purged"] == []
    assert res["skipped"] and res["skipped"][0]["reason"] == "仍在回收期内"
    assert os.path.isdir(op["recycled_to"]), "回收期内数据被删了"


def test_purge_removes_expired(cache_dir):
    op = executor.execute_cleanup(record_for(cache_dir))
    # 把可恢复期改成过去
    saved = manifest.find(op["id"])
    saved["recoverable_until"] = (
        datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    manifest.save(saved)

    recycled = op["recycled_to"]
    res = executor.purge_expired()
    assert op["id"] in res["purged"]
    assert not os.path.exists(recycled), "过期数据应已真删"
    assert manifest.find(op["id"])["recycled_to"] is None
    # 空壳也要清掉。与 rollback 同一类问题：先只修了 rollback，purge 这条路径漏了，
    # 结果永久删除后回收区照样攒空目录。
    assert not Path(recycled).parent.exists(), "永久删除后回收区仍留着空壳"
    assert not any(manifest.recycle_dir().iterdir()), "回收区不干净"


def test_purge_force_requires_explicit_id(cache_dir):
    """立刻永久删除必须点名 id，不能一刀切清空。"""
    op = executor.execute_cleanup(record_for(cache_dir))
    res = executor.purge_expired(force_ids=[op["id"]])
    assert op["id"] in res["purged"]
    assert not os.path.exists(op["recycled_to"])


def test_expiry_helper():
    now = datetime.now(timezone.utc)
    assert not manifest.is_expired(
        {"recoverable_until": (now + timedelta(days=1)).isoformat()})
    assert manifest.is_expired(
        {"recoverable_until": (now - timedelta(days=1)).isoformat()})
    assert not manifest.is_expired({}), "没有该字段时不应判为过期"
    assert not manifest.is_expired({"recoverable_until": "垃圾值"})


# ============================================================ 崩溃可追溯
def test_failed_operation_leaves_manifest_with_reason(cache_dir, monkeypatch):
    """中途失败也必须留下记录与原因，否则用户不知道东西去哪了。"""
    def boom(a, b):
        raise OSError("模拟磁盘错误")

    monkeypatch.setattr(os, "rename", boom)
    monkeypatch.setattr("shutil.move", boom)
    with pytest.raises(executor.ExecutionFailed):
        executor.execute_cleanup(record_for(cache_dir))

    ops = manifest.list_operations()
    assert len(ops) == 1
    assert ops[0]["status"] == "failed"
    assert "模拟磁盘错误" in (ops[0]["failure"] or "")
    assert cache_dir.exists(), "失败了但源目录也没了，这是最坏情况"


def test_planned_status_signals_interrupted_run(cache_dir, monkeypatch):
    """记录停在 planned 意味着"写了记录但没做完"，必须能看出来。"""
    real_save = manifest.save
    saved_ops = []

    def capture(op):
        saved_ops.append(dict(op))
        return real_save(op)

    monkeypatch.setattr(manifest, "save", capture)
    monkeypatch.setattr(os, "rename", lambda a, b: (_ for _ in ()).throw(
        KeyboardInterrupt()))
    monkeypatch.setattr("shutil.move", lambda a, b: (_ for _ in ()).throw(
        KeyboardInterrupt()))
    with pytest.raises(BaseException):
        executor.execute_cleanup(record_for(cache_dir))

    assert saved_ops[0]["status"] == "planned", \
        "第一次落盘时状态应是 planned，供崩溃后识别"
