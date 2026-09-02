r"""junction 迁移：把"不能删但能搬"的目录移到别的盘，原位留链接。

这是全项目风险最高的动作——它会真复制几 GiB 数据、真移走源目录、真在原位建重解析点。
所以本文件的重点不是"功能能跑"，而是**每一种中途失败都不丢数据**：

* 复制失败 -> 源目录一动没动
* 校验不过 -> 源目录一动没动
* 建链接失败 -> 源目录在回收区里，可整份放回
* 回滚 -> 先摘链接再还原，且新盘那份副本只在确认原位数据完整后才清

用真目录、真 junction、真回滚跑，不打桩文件系统——打桩的话恰好测不到这些顺序问题。
数据目录由 conftest 的 autouse fixture 隔离到临时目录。
"""
import os
from pathlib import Path

import pytest

from lostpath.act import executor, manifest, planner

pytestmark = pytest.mark.skipif(os.name != "nt", reason="junction 是 Windows 特性")

BIG = 600 * 2**20        # 过 JUNCTION_MIN_SIZE(500 MiB) 门槛


@pytest.fixture(autouse=True)
def no_process_probe(monkeypatch):
    monkeypatch.setattr(planner, "running_process_dirs", lambda: set())


def make_tree(root, files=("a.txt", "sub/b.txt", "sub/deep/c.bin")):
    """建一棵小目录树。体积靠 record 里的 size 撑，不真写 600 MiB。"""
    root.mkdir(parents=True, exist_ok=True)
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(rel.encode() * 8)
    return root


def rec(path, name, size=BIG, cat="用户数据", kind="app", owner="某软件", conf=0.9):
    return {
        "path": str(path), "name": name, "size": size, "files": 3, "cat": cat,
        "owner": owner, "owner_kind": kind, "conf": conf, "why": "测试",
        "redirect": None, "children": [], "zone": "RoamingAppData",
    }


# ------------------------------------------------------------ 计划层
def test_junction_proposed_for_unremovable_app_dir(tmp_path):
    """定性不可清理、归属明确、体积够大 -> 应给出 junction 而不是拦阻。"""
    src = make_tree(tmp_path / "AppProfile")
    target_root = tmp_path / "E"
    target_root.mkdir()
    p = planner.plan_for(rec(src, "AppProfile"), target_root=str(target_root))
    assert p.action == "junction", f"{p.action} / {[b.code for b in p.blockers]}"
    assert p.executable
    assert not any(b.code == "not_cleanable" for b in p.blockers), (
        "not_cleanable 不该拦住 junction——它一个字节都不删")


def test_high_risk_dir_never_junctioned(tmp_path):
    """定性"不可动"的目录禁止 junction：那批目录已知会因重解析点而修复/卸载失败。"""
    src = make_tree(tmp_path / "Installer")
    p = planner.plan_for(rec(src, "Installer", cat="不可动"),
                         target_root=str(tmp_path / "E"))
    assert p.action != "junction"
    assert not p.executable


def test_live_high_risk_rule_blocks_stale_snapshot(monkeypatch, tmp_path):
    """旧快照仍写未定性时，计划器也要按当前知识库立即挡住。"""
    src = make_tree(tmp_path / "SecurityData")
    monkeypatch.setattr(
        planner.attribution_kb, "high_risk",
        lambda path: "安全软件隔离区与自保护数据" if path == str(src) else None,
    )
    p = planner.plan_for(
        rec(src, "SecurityData", cat="未定性"),
        target_root=str(tmp_path / "E"),
    )
    assert not p.executable
    assert p.action == "none"
    assert p.cat == "不可动"
    assert any(b.code == "high_risk" for b in p.blockers)


@pytest.mark.parametrize("kind", ["vendor", "container", "system"])
def test_non_app_owner_not_junctioned(tmp_path, kind):
    """厂商/容器/系统目录不整块搬：影响面超出单个软件。"""
    src = make_tree(tmp_path / f"d-{kind}")
    p = planner.plan_for(rec(src, "d", kind=kind), target_root=str(tmp_path / "E"))
    assert p.action != "junction"


def test_small_dir_not_worth_junction(tmp_path):
    """小目录不搬：要真复制一遍，还给软件加一层重解析点，不值得。"""
    src = make_tree(tmp_path / "small")
    p = planner.plan_for(rec(src, "small", size=60 * 2**20),
                         target_root=str(tmp_path / "E"))
    assert p.action != "junction"


def test_capacity_precheck_counts_data_plus_margin(tmp_path, monkeypatch):
    """junction 要真搬数据，容量检查必须算上"数据体积 + 安全余量"。

    只留安全余量就会在搬 10 GiB 到只剩 3 GiB 的盘时"计划可执行"，搬到一半盘满。
    """
    src = make_tree(tmp_path / "AppProfile")
    # 目标盘只剩 数据 + 余量 - 1 字节
    need = BIG + planner.TARGET_FREE_MARGIN
    monkeypatch.setattr(planner, "drive_free_bytes", lambda _p: need - 1)
    p = planner.plan_for(rec(src, "AppProfile"), target_root=str(tmp_path / "E"))
    assert not p.executable
    assert any(b.code == "target_full" for b in p.blockers), [b.code for b in p.blockers]

    monkeypatch.setattr(planner, "drive_free_bytes", lambda _p: need)
    p2 = planner.plan_for(rec(src, "AppProfile"), target_root=str(tmp_path / "E"))
    assert p2.executable, [b.reason for b in p2.blockers]


# ------------------------------------------------------------ 执行层
def test_full_migration_and_rollback(tmp_path):
    """完整走一遍：搬走 -> 原位是 junction 且内容可读 -> 回滚 -> 完全复原。"""
    src = make_tree(tmp_path / "AppProfile")
    target_root = tmp_path / "E"
    target_root.mkdir()
    before = sorted(p.relative_to(src).as_posix()
                    for p in src.rglob("*") if p.is_file())

    op = executor.execute_junction(rec(src, "AppProfile"),
                                   target_root=str(target_root))
    assert op["status"] == "done", op

    # 原位是 junction，且透过它能读到原来的文件
    assert executor._is_junction(str(src))
    through = sorted(p.relative_to(src).as_posix()
                     for p in src.rglob("*") if p.is_file())
    assert through == before, "透过 junction 看到的内容变了"

    # 源数据在回收区留着（不是删掉）
    assert op["recycled_to"] and os.path.isdir(op["recycled_to"])

    # 回滚：链接摘掉、数据回原位、新盘副本清掉
    executor.rollback(op["id"])
    assert not executor._is_junction(str(src)), "回滚后原位还是 junction"
    after = sorted(p.relative_to(src).as_posix()
                   for p in src.rglob("*") if p.is_file())
    assert after == before, "回滚后文件不一致"
    assert not os.path.exists(op["junction_target"]), "新盘那份多余副本没清掉"


def test_copy_failure_leaves_source_untouched(tmp_path, monkeypatch):
    """复制阶段炸掉时源目录必须一动没动——这是"先复制后动源"的全部意义。"""
    src = make_tree(tmp_path / "AppProfile")
    before = sorted(p.name for p in src.rglob("*"))

    def boom(*a, **kw):
        raise OSError("磁盘忙")
    monkeypatch.setattr(executor.fsdedup, "copytree_keep_links", boom)

    with pytest.raises(executor.ExecutionFailed):
        executor.execute_junction(rec(src, "AppProfile"),
                                  target_root=str(tmp_path / "E"))
    assert os.path.isdir(src) and not executor._is_junction(str(src))
    assert sorted(p.name for p in src.rglob("*")) == before


def test_verify_mismatch_aborts_before_touching_source(tmp_path, monkeypatch):
    """复制后比对不一致，必须在动源目录之前中止。"""
    src = make_tree(tmp_path / "AppProfile")

    real = executor._dir_stats
    calls = {"n": 0}

    def fake(path):
        calls["n"] += 1
        f, b = real(path)
        return (f, b) if calls["n"] == 1 else (f - 1, b)   # 第二次（目标）故意少一个
    monkeypatch.setattr(executor, "_dir_stats", fake)

    with pytest.raises(executor.ExecutionFailed, match="复制后不一致"):
        executor.execute_junction(rec(src, "AppProfile"),
                                  target_root=str(tmp_path / "E"))
    assert os.path.isdir(src) and not executor._is_junction(str(src))
    assert (src / "a.txt").exists(), "校验没过却动了源目录"


def test_junction_failure_keeps_data_in_recycle(tmp_path, monkeypatch):
    """建链接这一步失败时，源数据必须还在回收区里可以整份放回。"""
    import _winapi
    src = make_tree(tmp_path / "AppProfile")

    def boom(t, s):
        raise OSError("拒绝访问")
    monkeypatch.setattr(_winapi, "CreateJunction", boom)

    with pytest.raises(executor.ExecutionFailed):
        executor.execute_junction(rec(src, "AppProfile"),
                                  target_root=str(tmp_path / "E"))
    ops = manifest.list_operations()
    assert len(ops) == 1 and ops[0]["status"] == "failed"
    dst = ops[0].get("recycled_to")
    assert dst and os.path.isdir(dst), "源数据既不在原位也不在回收区"
    assert (Path(dst) / "a.txt").exists()


def test_refuses_target_inside_source(tmp_path):
    """目标在源目录内部会无限递归复制，必须拒绝。"""
    src = make_tree(tmp_path / "AppProfile")
    with pytest.raises(executor.ExecutionRefused, match="无限递归"):
        executor.execute_junction(rec(src, "AppProfile"),
                                  target_root=str(src / "inner"))


def test_refuses_already_junction(tmp_path):
    """已经是 junction 的目录不必再搬。"""
    import _winapi
    real = make_tree(tmp_path / "real")
    link = tmp_path / "link"
    _winapi.CreateJunction(str(real), str(link))
    with pytest.raises(executor.ExecutionRefused):
        executor.execute_junction(rec(link, "link"),
                                  target_root=str(tmp_path / "E"))


def test_rollback_removes_link_before_restoring(tmp_path):
    r"""回滚必须先摘链接。

    不摘的话 os.path.exists(源) 为真（它指向新盘那份），会被误判成"原路径已重新
    存在"而中止；更糟的是若绕过判断去移动，数据会**穿过 junction 写进目标目录**。
    """
    src = make_tree(tmp_path / "AppProfile")
    op = executor.execute_junction(rec(src, "AppProfile"),
                                   target_root=str(tmp_path / "E"))
    target = op["junction_target"]
    executor.rollback(op["id"])

    # 原位是真目录（不是链接），且不含"自己套自己"的痕迹
    assert os.path.isdir(src) and not executor._is_junction(str(src))
    assert not os.path.exists(target)
    assert not (src / os.path.basename(target)).exists(), "数据被塞进了自己里面"


def test_rollback_keeps_copy_when_restore_incomplete(tmp_path, monkeypatch):
    """原位数据没能完整回来时，宁可留着新盘副本占空间，也不能删。"""
    src = make_tree(tmp_path / "AppProfile")
    op = executor.execute_junction(rec(src, "AppProfile"),
                                   target_root=str(tmp_path / "E"))
    target = op["junction_target"]

    # 篡改记录里的字节数，模拟"还原后与操作前不一致"
    o = manifest.find(op["id"])
    o["source_bytes"] = (o.get("source_bytes") or 0) + 12345
    manifest.save(o)

    out = executor.rollback(op["id"])
    assert os.path.isdir(target), "校验不一致却把副本删了"
    assert out.get("leftover_copy") == target
