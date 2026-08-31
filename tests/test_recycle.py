r"""回收区清单与永久删除。

这一层的价值在于让"数据还占着盘"这件事可见可操作：清理只是把数据移进回收区，磁盘空间
并没真释放。清单要给磁盘实测体积（而非 manifest 里记的快照值），剩余天数要准，而永久
删除是全工具唯一不可撤销的操作，所以门槛要守住。
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from lostpath.act import executor, manifest


@pytest.fixture
def cleaned(tmp_path):
    """跑一次真清理，返回 (op, 原始体积)。"""
    d = tmp_path / "cache"
    (d / "sub").mkdir(parents=True)
    (d / "a.bin").write_bytes(b"a" * 3000)
    (d / "sub" / "b.bin").write_bytes(b"b" * 7000)
    rec = {
        "path": str(d), "name": "cache", "size": 200 * 2**20, "files": 2,
        "cat": "可再生缓存", "owner_kind": "toolchain", "conf": 0.9,
        "redirect": None, "owner": "某工具", "why": "测试",
    }
    op = executor.execute_cleanup(rec)
    return op, 10000


def expire(op_id):
    op = manifest.find(op_id)
    op["recoverable_until"] = (
        datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    manifest.save(op)


# --------------------------------------------------------------- 清单
def test_recycle_entries_empty_initially():
    assert manifest.recycle_entries() == []


def test_entry_reports_measured_size_not_snapshot_value(cleaned):
    """体积必须是磁盘实测，不能沿用归因时记的 size。

    manifest 里的 size 是快照值（这里故意设成 200 MiB），实际内容只有 10000 字节。
    用户要据此决定删不删，给错数就是误导。
    """
    op, real = cleaned
    entries = manifest.recycle_entries()
    assert len(entries) == 1
    e = entries[0]
    assert e["size"] == real, f"应为实测 {real}，实际 {e['size']}"
    assert e["size"] != op["size"], "沿用了 manifest 里的快照值"
    assert e["files"] == 2


def test_entry_has_source_and_recycled_paths(cleaned):
    op, _ = cleaned
    e = manifest.recycle_entries()[0]
    assert e["source_path"] == op["source_path"]
    assert e["recycled_to"] == op["recycled_to"]
    assert e["id"] == op["id"]


def test_days_left_counts_down(cleaned):
    op, _ = cleaned
    e = manifest.recycle_entries()[0]
    assert e["expired"] is False
    assert e["days_left"] is not None
    assert 0 < e["days_left"] <= manifest.RECOVERABLE_DAYS


def test_expired_entry_flagged_with_zero_days(cleaned):
    op, _ = cleaned
    expire(op["id"])
    e = manifest.recycle_entries()[0]
    assert e["expired"] is True
    assert e["days_left"] == 0, "过期项剩余天数应为 0，不能是负数"


def test_rolled_back_entry_leaves_recycle_listing(cleaned):
    """还原之后就不该再出现在回收区清单里。"""
    op, _ = cleaned
    executor.rollback(op["id"])
    assert manifest.recycle_entries() == []


def test_entries_sorted_by_size_desc(tmp_path):
    """按体积倒序：用户最关心哪个占得多。"""
    for name, n in (("small", 1000), ("big", 50000), ("mid", 9000)):
        d = tmp_path / name
        d.mkdir()
        (d / "f.bin").write_bytes(b"x" * n)
        executor.execute_cleanup({
            "path": str(d), "name": name, "size": 100 * 2**20, "files": 1,
            "cat": "可再生缓存", "owner_kind": "toolchain", "conf": 0.9,
            "redirect": None, "owner": "t", "why": "测试",
        })
    sizes = [e["size"] for e in manifest.recycle_entries()]
    assert sizes == sorted(sizes, reverse=True), sizes


def test_days_left_helpers():
    now = datetime.now(timezone.utc)
    assert manifest.days_left(
        {"recoverable_until": (now + timedelta(days=5)).isoformat()}) in (4, 5)
    assert manifest.days_left(
        {"recoverable_until": (now - timedelta(days=5)).isoformat()}) == 0
    assert manifest.days_left({}) is None
    assert manifest.days_left({"recoverable_until": "垃圾"}) is None


def test_dir_size_skips_unreadable(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    (d / "a").write_bytes(b"1234")
    size, files = manifest.dir_size(str(d))
    assert (size, files) == (4, 1)
    # 不存在的路径不该抛
    assert manifest.dir_size(str(tmp_path / "nope")) == (0, 0)


# --------------------------------------------------------- 永久删除门槛
def test_purge_all_refuses_unexpired(cleaned):
    """不带 force_ids 时只清过期项，回收期内的一律跳过。"""
    op, _ = cleaned
    res = executor.purge_expired()
    assert res["purged"] == []
    assert res["skipped"][0]["id"] == op["id"]
    assert os.path.isdir(op["recycled_to"]), "回收期内数据被删了"


def test_purge_expired_frees_space(cleaned):
    op, _ = cleaned
    expire(op["id"])
    dst = op["recycled_to"]
    res = executor.purge_expired()
    assert op["id"] in res["purged"]
    assert not os.path.exists(dst)
    assert manifest.recycle_entries() == [], "删完清单应为空"


def test_force_purge_requires_naming_the_id(cleaned):
    """提前删必须点名 id——后端不接受"清空全部"这种一刀切指令。"""
    op, _ = cleaned
    other = executor.purge_expired(force_ids=["some-other-id"])
    assert other["purged"] == []
    assert os.path.isdir(op["recycled_to"]), "点名了别的 id 却删了这条"

    res = executor.purge_expired(force_ids=[op["id"]])
    assert op["id"] in res["purged"]
    assert not os.path.exists(op["recycled_to"])


def test_purged_entry_cannot_be_rolled_back(cleaned):
    """永久删除之后回滚必须明确拒绝，不能默默"成功"。

    默默成功的后果：界面提示"已还原到原位置"，用户以为数据回来了，实际什么都没有。
    这比直接报错糟糕得多。
    """
    op, _ = cleaned
    executor.purge_expired(force_ids=[op["id"]])
    with pytest.raises(executor.ExecutionRefused) as e:
        executor.rollback(op["id"])
    assert "永久删除" in str(e.value)
    assert not os.path.exists(op["source_path"]), "回滚被拒却仍造出了目录"


def test_purged_entry_not_listed_as_rollbackable(cleaned):
    """已永久删除的不能再算进"可回滚"计数，否则界面会给出无效的撤销按钮。

    坑在于清空时会把 recycled_to 置空，而"从未搬过东西"的操作那里本来也是空的——
    只看这个字段会把两者混为一谈。
    """
    op, _ = cleaned
    assert len(manifest.pending_rollback()) == 1
    executor.purge_expired(force_ids=[op["id"]])
    assert manifest.pending_rollback() == []
    assert manifest.is_purged(manifest.find(op["id"])) is True


def test_purge_survives_locked_entry(cleaned, monkeypatch):
    """某条删不掉时要报出来并继续处理其余，不能整批中断。"""
    op, _ = cleaned
    expire(op["id"])

    def boom(path, ignore_errors=False):
        raise OSError("目录被占用")

    monkeypatch.setattr("shutil.rmtree", boom)
    res = executor.purge_expired()
    assert res["purged"] == []
    assert "删除失败" in res["skipped"][0]["reason"]
    assert manifest.find(op["id"])["recycled_to"] == op["recycled_to"], \
        "删除失败却把记录标成已清空，数据会变成孤儿"


# ------------------------------- 孤儿目录：显形之后必须也能清掉
def test_orphan_recycle_dir_can_be_purged_when_named(tmp_path):
    r"""回收区里无台账记录的目录，用户点名后必须真能删掉。

    `recycle_entries()` 现在会把这类目录列出来（不列的话那些字节界面看不见、也永远清
    不掉——真实故障留下过 3.22 GiB 这样的副本）。但**列出来却删不掉是个死胡同**：界面
    给了「永久删除」按钮，而 `purge_expired` 遍历的是台账记录，纯孤儿压根不在其中，
    于是按钮报一句没有信息的"删除失败"。显形与可清理必须成对。
    """
    orphan = manifest.recycle_dir() / "nobodyclaims" / "junk"
    orphan.mkdir(parents=True)
    (orphan / "a.bin").write_bytes(b"a" * 1234)

    listed = {e["id"] for e in manifest.recycle_entries()}
    assert "nobodyclaims" in listed, "前提：孤儿应当被列出"

    res = executor.purge_expired(force_ids=["nobodyclaims"])
    assert "nobodyclaims" in res["purged"], (
        f"点名删除孤儿应当成功，实得 purged={res['purged']} skipped={res['skipped']}")
    assert not (manifest.recycle_dir() / "nobodyclaims").exists()
    assert "nobodyclaims" not in {e["id"] for e in manifest.recycle_entries()}


def test_orphan_is_never_purged_automatically(tmp_path):
    """不点名就绝不删。连它是什么留下的都说不出来，就不该替用户决定它可以消失。"""
    orphan = manifest.recycle_dir() / "keepme" / "junk"
    orphan.mkdir(parents=True)
    (orphan / "a.bin").write_bytes(b"a" * 999)

    res = executor.purge_expired()          # 不传 force_ids = 自动清理那条路
    assert "keepme" not in res["purged"], "自动清理不该碰无台账记录的数据"
    assert (manifest.recycle_dir() / "keepme" / "junk" / "a.bin").exists()


def test_purging_one_orphan_does_not_touch_another(tmp_path):
    r"""点名删 A 不能连带删 B。

    这条补的是一个**判别力缺口**：`_purge_orphans` 有两道防护（`if not force` 早退、
    循环里的 `shell.name not in force`），而"不传 force_ids 时不删"那条用例只覆盖前者
    ——把后者改坏它照样绿，两道防护互相掩护。删掉任一道都必须有测试变红。
    """
    for name in ("deleteme", "keepme"):
        d = manifest.recycle_dir() / name / "junk"
        d.mkdir(parents=True)
        (d / "a.bin").write_bytes(b"a" * 100)

    res = executor.purge_expired(force_ids=["deleteme"])
    assert res["purged"] == ["deleteme"], f"只该删被点名的那个，实得 {res['purged']}"
    assert not (manifest.recycle_dir() / "deleteme").exists()
    assert (manifest.recycle_dir() / "keepme" / "junk" / "a.bin").exists(), \
        "没被点名的孤儿被连带删掉了"
