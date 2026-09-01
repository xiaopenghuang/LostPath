r"""子目录级计划：父目录整块不可动，但下面某个子目录本身是缓存。

这一层解决的是"整块表达不了一半能清一半不能"。实测最典型的是
`Roaming\Code` 9.93 GiB 判"未定性"整块拦下，而它下面 `WebStorage` 6.40 GiB 名字
就是缓存、`User` 3.14 GiB 是真设置不能碰。

本文件重点守三条我在实现时真踩过的坑：
1. plan_for 见到 blockers 就 return，所以父级拦阻必须在**施加前**排除，事后摘掉
   拿到的是 action=none 的空计划；
2. 去重（父可执行则丢子）必须发生在环境变量冲突标记**之后**，否则父被冲突拦下、
   子又已按"父可执行"丢掉，两头落空；
3. 体积不许算两遍——父与子同时可执行的话界面上"可腾出"就是虚数。
"""
import os

import pytest

from lostpath.act import planner

SEP = os.sep


def rec(path, name, size, cat="未定性", kind="app", conf=0.9, children=None,
        owner="某软件", **kw):
    """造一条快照记录。字段名与 attribute_v4 输出保持一致。"""
    out = {
        "path": path, "name": name, "size": size, "files": 10, "cat": cat,
        "owner": owner, "owner_kind": kind, "conf": conf, "why": "测试",
        "redirect": None, "children": children or [], "zone": "RoamingAppData",
    }
    out.update(kw)
    return out


@pytest.fixture(autouse=True)
def no_process_probe(monkeypatch):
    """打掉进程探测。

    plan_all 会跑 PowerShell 枚举进程路径（超时 30s）。本文件每条用例都调 plan_all，
    不打桩的话快套件要多花 40 秒——而这些用例考的是计划逻辑，与"谁在运行"无关。
    in_use 拦阻另有 test_planner 覆盖。
    """
    monkeypatch.setattr(planner, "running_process_dirs", lambda: set())


@pytest.fixture
def tree(tmp_path):
    """真建目录：plan_for 会 os.path.isdir 校验，假路径一律被判 missing。"""
    def mk(*parts):
        d = tmp_path.joinpath(*parts)
        d.mkdir(parents=True, exist_ok=True)
        (d / "f.bin").write_bytes(b"x" * 64)
        return str(d)
    return mk


# 稳过清理门槛（50 MiB），又要让"父 = BIG*2"停在 junction 门槛（500 MiB）以下——
# 否则父目录会够格 junction 而变成可执行，子计划按去重规则被丢掉，本文件考的东西就
# 全被那条规则盖住了。父子如何取舍另见 test_junction_supersedes_child_cleanup。
BIG = 200 * 2**20


# ------------------------------------------------------- 基本行为
def test_cleanable_child_of_blocked_parent_becomes_plan(tree):
    """父目录未定性被拦，子目录是缓存 -> 子目录应单独出计划。"""
    parent = tree("Code")
    child = tree("Code", "WebStorage")
    records = [rec(parent, "Code", BIG * 2, cat="未定性", children=[
        rec(child, "WebStorage", BIG, cat="可再生缓存"),
    ])]
    out = planner.plan_all(records, target_root=str(tree("target")))
    kids = [p for p in out["plans"] if p["parent_path"]]
    assert len(kids) == 1, "子目录没出计划"
    assert kids[0]["path"] == child
    assert kids[0]["executable"], f"子计划应可执行，拦阻={kids[0]['blockers']}"
    assert kids[0]["action"] == "cleanup"
    assert kids[0]["parent_path"] == parent


def test_child_skipped_when_parent_executable(tree):
    """父目录整块可清理时不再出子计划，否则同一份空间算两遍。"""
    parent = tree("cache")
    child = tree("cache", "sub")
    records = [rec(parent, "cache", BIG * 2, cat="可再生缓存", children=[
        rec(child, "sub", BIG, cat="可再生缓存"),
    ])]
    out = planner.plan_all(records, target_root=str(tree("target")))
    assert [p["path"] for p in out["plans"] if p["executable"]] == [parent]
    assert not [p for p in out["plans"] if p["parent_path"]], "父可执行却仍出了子计划"


def test_ignored_child_prevents_parent_from_covering_it(monkeypatch, tree):
    """用户保留子目录时，不能因父级去重把保留规则吞掉。"""
    parent = tree("cache")
    child = tree("cache", "keep")
    monkeypatch.setattr(
        planner.rules_mod, "ignored_rule",
        lambda path: {"path": child, "reason": "用户保留"}
        if os.path.normcase(path) == os.path.normcase(child) else None,
    )
    records = [rec(parent, "cache", BIG * 2, cat="可再生缓存", children=[
        rec(child, "keep", BIG, cat="可再生缓存"),
    ])]
    out = planner.plan_all(records, target_root=str(tree("target")))
    parent_plan = [p for p in out["plans"] if p["path"] == parent][0]
    assert not parent_plan["executable"]
    assert any(b["code"] == "user_ignored" for b in parent_plan["blockers"])
    child_plan = [p for p in out["plans"] if p["path"] == child][0]
    assert any(b["code"] == "user_ignored" for b in child_plan["blockers"])


def test_non_cleanable_child_is_not_planned(tree):
    """子目录本身不是可清理性质，不能因为父目录被拦就顺带处理它。"""
    parent = tree("Code")
    child = tree("Code", "User")
    records = [rec(parent, "Code", BIG * 2, cat="未定性", children=[
        rec(child, "User", BIG, cat="用户数据"),
    ])]
    out = planner.plan_all(records, target_root=str(tree("target")))
    assert not [p for p in out["plans"] if p["parent_path"]]


# ------------------------------------------------------- 坑 1：早退
def test_vendor_child_not_blocked_by_parent_level_reason(tree):
    r"""厂商目录下的缓存子目录必须能处理。

    `owner_vendor` 的理由是"下面是多个产品，不可整块处理"——而子目录计划正是它
    要求的做法。曾因事后摘拦阻（plan_for 已 return）导致 action 恒为 none。
    """
    parent = tree("Tencent")
    child = tree("Tencent", "Logs")
    records = [rec(parent, "Tencent", BIG * 3, cat="未定性", kind="vendor",
                   owner="腾讯", children=[
                       rec(child, "Logs", BIG, cat="可再生缓存", kind="vendor",
                           owner="腾讯", conf=0.0, inherited=True),
                   ])]
    out = planner.plan_all(records, target_root=str(tree("target")))
    kids = [p for p in out["plans"] if p["parent_path"]]
    assert len(kids) == 1
    k = kids[0]
    assert k["action"] == "cleanup", f"动作没算出来：{k['action']}"
    assert k["executable"], f"仍被拦：{k['blockers']}"
    assert not any(b["code"] == "owner_vendor" for b in k["blockers"])


def test_system_owned_child_stays_blocked(tree):
    """system 归属的风险与层级无关，子目录照拦——这条不能跟着 vendor 一起放开。"""
    parent = tree("Windows")
    child = tree("Windows", "Logs")
    records = [rec(parent, "Windows", BIG * 2, cat="系统所有", kind="system",
                   owner=None, children=[
                       rec(child, "Logs", BIG, cat="可再生缓存", kind="system",
                           owner=None),
                   ])]
    out = planner.plan_all(records, target_root=str(tree("target")))
    kids = [p for p in out["plans"] if p["parent_path"]]
    assert len(kids) == 1
    assert not kids[0]["executable"]
    assert any(b["code"] == "owner_system" for b in kids[0]["blockers"])


# ------------------------------------------------------- 坑 2：顺序
def test_child_survives_when_parent_blocked_by_env_conflict(tree):
    r"""父目录被环境变量冲突拦下时，它的子目录必须仍然可处理。

    实测原型：`Local\uv` 与 `Roaming\uv` 都申领 UV_CACHE_DIR，冲突把两个父目录都
    拦下；若去重发生在冲突标记之前，`Local\uv\cache` 会因"父目录可执行"被丢掉，
    于是这 1.57 GiB 父子两头落空。
    """
    a = tree("uv-local")
    b = tree("uv-roaming")
    kid = tree("uv-local", "cache")
    records = [
        rec(a, "uv", BIG * 2, cat="可再生缓存", redirect="UV_CACHE_DIR",
            children=[rec(kid, "cache", BIG, cat="可再生缓存")]),
        rec(b, "uv", BIG * 2, cat="可再生缓存", redirect="UV_CACHE_DIR"),
    ]
    out = planner.plan_all(records, target_root=str(tree("target")))
    parents = [p for p in out["plans"] if not p["parent_path"]]
    assert all(not p["executable"] for p in parents), "冲突没拦住父目录，前提不成立"
    assert any(b["code"] == "env_var_conflict"
               for p in parents for b in p["blockers"])
    kids = [p for p in out["plans"] if p["parent_path"]]
    assert len(kids) == 1 and kids[0]["executable"], (
        f"父目录被冲突拦下，子目录却没救回来：{kids}")


# ------------------------------------------------------- 坑 3：不重复计数
def test_no_ancestor_descendant_pair_in_executable_set(tree):
    """可执行集合里不允许出现祖孙关系，否则 reclaimable 把同一份空间算两遍。"""
    p1 = tree("a")
    c1 = tree("a", "cache")
    p2 = tree("b")
    records = [
        rec(p1, "a", BIG * 2, cat="未定性",
            children=[rec(c1, "cache", BIG, cat="可再生缓存")]),
        rec(p2, "b", BIG, cat="可再生缓存"),
    ]
    out = planner.plan_all(records, target_root=str(tree("target")))
    ok = [p["path"].lower().rstrip(SEP) for p in out["plans"] if p["executable"]]
    pairs = [(x, y) for x in ok for y in ok if x != y and y.startswith(x + SEP)]
    assert not pairs, f"体积被算两遍：{pairs}"
    assert out["summary"]["reclaimable"] == BIG * 2   # c1 + p2


# ------------------------------------------------------- 置信度继承
def test_child_inherits_parent_confidence(tree):
    """归属继承自父目录时置信度也该继承，否则 conf=0 会被 low_confidence 拦死。

    子目录通常一条自己的证据都没有（attribute 只给它算自身证据分）。但门禁问的是
    "会不会判错归属"，而这个判断完全建立在父目录之上：父目录 90% 属于 X，它的子
    目录属于 X 的把握就是 90%，不是 0。
    """
    parent = tree("Code")
    child = tree("Code", "WebStorage")
    records = [rec(parent, "Code", BIG * 2, cat="未定性", conf=0.93,
                   children=[rec(child, "WebStorage", BIG, cat="可再生缓存",
                                 conf=0.0, inherited=True)])]
    out = planner.plan_all(records, target_root=str(tree("target")))
    k = [p for p in out["plans"] if p["parent_path"]][0]
    assert k["confidence"] == pytest.approx(0.93)
    assert not any(b["code"] == "low_confidence" for b in k["blockers"])


def test_independent_child_keeps_own_confidence(tree):
    """子目录若有独立归属（与父不同），用它自己的分，不借父目录的。"""
    parent = tree("vendor")
    child = tree("vendor", "cache")
    records = [rec(parent, "vendor", BIG * 2, cat="未定性", conf=0.95,
                   owner="厂商 A", kind="vendor",
                   children=[rec(child, "cache", BIG, cat="可再生缓存",
                                 conf=0.2, owner="别的软件", inherited=False)])]
    out = planner.plan_all(records, target_root=str(tree("target")))
    k = [p for p in out["plans"] if p["parent_path"]][0]
    assert k["confidence"] == pytest.approx(0.2), "不该借用父目录的置信度"
    assert any(b["code"] == "low_confidence" for b in k["blockers"])


def test_junction_supersedes_child_cleanup(tree):
    r"""父目录够格 junction 时，整块搬走优先于清掉它下面的缓存子目录。

    依据是项目红线"优先迁移而非删除"：搬走 `Roaming\Code` 全部 9.93 GiB 且一个字节
    都不删，优于删掉其中 6.40 GiB 的 WebStorage。两者互斥，同时给出会把同一份空间
    在"可腾出"里算两遍。
    """
    parent = tree("Code")
    child = tree("Code", "WebStorage")
    big = planner.JUNCTION_MIN_SIZE * 2
    records = [rec(parent, "Code", big, cat="未定性", children=[
        rec(child, "WebStorage", big // 2, cat="可再生缓存"),
    ])]
    out = planner.plan_all(records, target_root=str(tree("target")))
    ok = [p for p in out["plans"] if p["executable"]]
    assert [p["action"] for p in ok] == ["junction"], [
        (p["path"], p["action"]) for p in ok]
    assert ok[0]["path"] == parent
    assert out["summary"]["reclaimable"] == big, "父子被算了两遍"


def test_child_plan_carries_reason_in_notes(tree):
    """界面要能说明"为什么单独处理这个子目录"，notes 必须带上缘由。"""
    parent = tree("Code")
    child = tree("Code", "WebStorage")
    records = [rec(parent, "Code", BIG * 2, cat="未定性",
                   children=[rec(child, "WebStorage", BIG, cat="可再生缓存")])]
    out = planner.plan_all(records, target_root=str(tree("target")))
    k = [p for p in out["plans"] if p["parent_path"]][0]
    assert any("整块不可动" in n for n in k["notes"]), k["notes"]
