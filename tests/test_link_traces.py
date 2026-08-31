r"""痕迹挂接与合成的守恒（`engine/inventory.py`）。

`link_traces` + `synth_entities` 把 N 条归因痕迹分成三堆：挂到台账实体上的、合成为
新实体的、仍然挂不上的。这三堆必须**恰好**覆盖输入——它是"C 盘全景页的总量"与"台账
各行体积之和"能对上的唯一保证，而这两个数用户在同屏就能看见。

**为什么这个模块非要自动化验。** 它的失效模式正是本项目摔过三次那一款：分堆时漏掉
一支，总量少一截，而界面上每一行看着都对（见 AGENTS.md「汇总指标看起来正常 ≠ 引擎
没坏」）。汇总数字不会喊疼，只有守恒断言会。

**为什么台账实体是测试自己造的。** `build_entities()` 读真注册表，结果随机器变化，
快套件不许依赖它（本项目吃过多次"指标取决于跑测试的人是谁"的亏）。而实体对这两个被测
函数来说是**输入**、不是产物，自造输入不构成 AGENTS.md 说的那种假测试——那一类是
"测试自己造了被测代码本该造的东西"，这里没有。

**`import inventory` 有副作用，注意别踩**：该模块顶层就把 `PORTABLE_FILE` 与
`ICONS_DIR` 定死了，而那发生在 autouse 的 `isolated_data_dir` 生效**之前**，所以这
两个常量指向**真实**用户目录。本文件只调 `link_traces` / `synth_entities`，两个都
不碰它们；**不要在这里调 `build_entities()` / `load_portable()` / `save_portable()`**，
那会读写用户的真实数据。
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO), str(REPO / "engine")):
    if p not in sys.path:
        sys.path.insert(0, p)

import inventory  # noqa: E402

from conftest import load_fixture  # noqa: E402


def trace(path, owner=None, kind="toolchain", size=100, **extra):
    """一条最小痕迹。字段名与 `attribute_v4` 的产出对齐。"""
    t = {"path": path, "name": Path(path).name, "owner": owner,
         "owner_kind": kind, "size": size, "files": 1, "cat": "可再生缓存"}
    t.update(extra)
    return t


def ledger(*specs):
    """造台账实体。`link_traces` 只读 id / name / publisher 三个字段。"""
    return [{"id": "r:" + n, "name": n, "publisher": pub, "source": "registry",
             "fragments": [], "portable": False} for n, pub in specs]


@pytest.fixture
def real_run():
    r"""在真 fixtures 上跑一遍完整分堆（106 条痕迹）。

    台账用 `inventory.json` 里的注册表条目搭一个替身，按 name 去重——原始注册表同名
    条目会在 HKLM64/HKLM32 各出现一次，不去重会造出 id 重复的台账，让"id 唯一"那条
    断言因为**替身自己的毛病**而红，那就查不出真问题了。
    """
    traces = load_fixture("attribution_v4.json")
    seen, ents = set(), []
    for a in load_fixture("inventory.json")["apps"]:
        if a["name"] in seen:
            continue
        seen.add(a["name"])
        ents.append({"id": "r:" + a["name"], "name": a["name"],
                     "publisher": a.get("publisher"), "source": "registry",
                     "fragments": [], "portable": False})
    linked_map, unlinked = inventory.link_traces(ents, traces)
    synth, rest = inventory.synth_entities(unlinked)
    return {"traces": traces, "ents": ents, "synth": synth, "rest": rest,
            "linked_map": linked_map}


# ------------------------------------------------------------ 守恒（真 fixtures）
def test_count_conservation(real_run):
    """条数守恒：已挂接 + 合成 + 剩余 ≡ 输入条数。"""
    linked = sum(len(e.get("traces", [])) for e in real_run["ents"])
    synth_n = sum(len(s["traces"]) for s in real_run["synth"])
    rest_n = len(real_run["rest"])
    total = len(real_run["traces"])
    assert linked + synth_n + rest_n == total, (
        f"已挂接 {linked} + 合成 {synth_n} + 剩余 {rest_n} "
        f"= {linked + synth_n + rest_n}，而输入是 {total}")


def test_size_conservation(real_run):
    """体积守恒。差额直接报字节数——"少了几条"和"少了多少"是两个线索。"""
    want = sum(t.get("size") or 0 for t in real_run["traces"])
    got = (sum(e.get("traces_size") or 0 for e in real_run["ents"])
           + sum(s["traces_size"] or 0 for s in real_run["synth"])
           + sum(t.get("size") or 0 for t in real_run["rest"]))
    assert got == want, f"三堆合计 {got} != 输入 {want}，差 {want - got} 字节"


def test_every_trace_lands_in_exactly_one_bucket(real_run):
    """按对象身份查不重不漏。

    只查条数与体积会漏掉"A 少一条、B 多一条"这种互相抵消的情形——两个数都对得上，
    而某条痕迹被算了两次、另一条不见了。所以这里逐条认对象。
    """
    buckets = (
        ("linked", [t for e in real_run["ents"] for t in e.get("traces", [])]),
        ("synth", [t for s in real_run["synth"] for t in s["traces"]]),
        ("rest", real_run["rest"]),
    )
    seen = {}
    for label, items in buckets:
        for t in items:
            prev = seen.get(id(t))
            assert prev is None, f"{t['path']} 同时落在 {prev} 与 {label}"
            seen[id(t)] = label
    missing = [t["path"] for t in real_run["traces"] if id(t) not in seen]
    assert not missing, f"凭空消失 {len(missing)} 条，例如 {missing[:5]}"


def test_traces_size_equals_sum_of_own_traces(real_run):
    """每个实体的 traces_size 必须等于自家痕迹之和。

    总量守恒了但某一行的数标错，用户看到的仍是错的——台账是按行读的。
    """
    for e in [*real_run["ents"], *real_run["synth"]]:
        want = sum(t.get("size") or 0 for t in e.get("traces", []))
        assert (e.get("traces_size") or 0) == want, (
            f"{e['id']} 报 {e.get('traces_size')}，自家痕迹之和是 {want}")


def test_synth_ids_are_unique_and_do_not_hit_the_ledger(real_run):
    """合成 id 既不能互相撞，也不能撞上已有台账实体。"""
    sids = [s["id"] for s in real_run["synth"]]
    dup = sorted({k for k in sids if sids.count(k) > 1})
    assert not dup, f"合成实体 id 重复：{dup}"
    clash = sorted(set(sids) & {e["id"] for e in real_run["ents"]})
    assert not clash, f"合成 id 撞上台账实体：{clash}"


def test_link_map_swallows_nothing(real_run):
    """`link_traces` 返回的 id→实体 映射不该比实体列表短。

    它是 `{x["id"]: x for x in entities}`，id 一重复就静默少一行。这条断言等于
    在说"台账 id 必须唯一"，而那正是上游 `build_entities` 的责任。
    """
    assert len(real_run["linked_map"]) == len(real_run["ents"])


def test_no_container_is_synthesized_on_real_data(real_run):
    """真数据上也不许出现 container 合成实体（会与其子目录重复计数）。"""
    bad = [s["id"] for s in real_run["synth"] if s["owner_kind"] == "container"]
    assert not bad, bad


# ------------------------------------------------------------ 分堆规则
def test_container_traces_are_never_synthesized():
    r"""容器故意不合成——体积由子目录承担，收进台账就是重复计数。

    `Packages` / `Package Cache` / `Programs` 这类本身不属于任何软件，归因把它们标为
    container 正是为了让子目录各自担体积。
    """
    ts = [trace(r"C:\x\Packages", owner="Packages", kind="container", size=500)]
    synth, rest = inventory.synth_entities(ts)
    assert not synth, "container 被合成成了实体"
    assert rest == ts, "container 必须留在未挂接堆里，不能凭空消失"


def test_unowned_traces_stay_unlinked():
    """归因没定出 owner 的痕迹不许挂到任何实体上，也不许合成。"""
    ts = [trace(r"C:\x\某个说不清的目录", owner=None, kind="unknown", size=700)]
    ents = ledger(("Firefox", "Mozilla"))
    _, unlinked = inventory.link_traces(ents, ts)
    assert unlinked == ts
    assert not any(e.get("traces") for e in ents)
    synth, rest = inventory.synth_entities(unlinked)
    assert not synth and rest == ts


def test_kind_outside_whitelist_stays_unlinked():
    """owner_kind 不在 SYNTH_KINDS 里的一律留在剩余堆，不能被吞掉。"""
    ts = [trace(r"C:\x\Whatever", owner="Whatever", kind="brand-new-kind")]
    synth, rest = inventory.synth_entities(ts)
    assert not synth
    assert rest == ts


# ------------------------------------------- 同一 owner 两种 kind（曾经产出重复 id）
def test_same_owner_with_two_kinds_becomes_one_entity():
    r"""同一 owner 带两种 owner_kind 只能产出一个实体。

    原先按 `(owner, kind)` 分组而 id 只拼 `"t:" + owner`，于是产出**两个 id 相同**的
    实体。这不是假想：fixtures 的 106 条痕迹里 `NVIDIA` 就同时是 `app` 与 `vendor`
    （唯一一个），本机没炸只因为 NVIDIA 挂上了注册表实体、没走到合成——又是一次"靠
    巧合挡住"。

    后果实打实：`SoftwarePage.tsx` 用 `find(g => g.id === owner)` 取详情，**先到先得**
    ——列表里两行，点第二行看到的是第一行的数据，React 还会报重复 key。
    """
    ts = [
        trace(r"C:\x\NVIDIA", owner="NVIDIA", kind="vendor", size=300),
        trace(r"C:\x\NVIDIA Corporation", owner="NVIDIA", kind="app", size=100),
    ]
    synth, rest = inventory.synth_entities(ts)
    assert len(synth) == 1, f"产出了 {len(synth)} 个实体：{[s['id'] for s in synth]}"
    assert synth[0]["id"] == "t:NVIDIA"
    assert len(synth[0]["traces"]) == 2, "合并时丢了痕迹"
    assert synth[0]["traces_size"] == 400
    assert not rest


def test_mixed_kinds_keep_shared_vendor_conservative():
    """混进 vendor 就按 vendor 标记——厂商目录不等于单个软件，不能宣称可整块处理。"""
    ts = [
        trace(r"C:\x\NVIDIA Corporation", owner="NVIDIA", kind="app", size=900),
        trace(r"C:\x\NVIDIA", owner="NVIDIA", kind="vendor", size=100),
    ]
    synth, _ = inventory.synth_entities(ts)
    assert synth[0]["shared_vendor"] is True, (
        "体积最大那条是 app，但只要有一条 vendor 就该保守标记")


def test_owner_kind_follows_the_largest_trace():
    """代表 kind 取体积最大那条痕迹的——那条最能代表这个实体是什么。"""
    ts = [
        trace(r"C:\x\uv-big", owner="uv", kind="toolchain", size=900),
        trace(r"C:\x\uv-small", owner="uv", kind="app_unregistered", size=100),
    ]
    synth, _ = inventory.synth_entities(ts)
    assert synth[0]["owner_kind"] == "toolchain"


def test_merged_traces_stay_sorted_by_size_desc():
    """合并后痕迹仍按体积降序——界面直接照这个顺序渲染。"""
    ts = [
        trace(r"C:\x\a", owner="X", kind="toolchain", size=10),
        trace(r"C:\x\b", owner="X", kind="app", size=800),
        trace(r"C:\x\c", owner="X", kind="toolchain", size=50),
    ]
    synth, _ = inventory.synth_entities(ts)
    sizes = [t["size"] for t in synth[0]["traces"]]
    assert sizes == sorted(sizes, reverse=True), sizes


def test_redirects_are_deduped_and_sorted():
    """重定向变量去重排序；一个都没有时是 None 而非空列表（UI 据此判有没有）。"""
    ts = [
        trace(r"C:\x\a", owner="X", kind="toolchain", redirect="UV_CACHE_DIR"),
        trace(r"C:\x\b", owner="X", kind="toolchain", redirect="UV_CACHE_DIR"),
        trace(r"C:\x\c", owner="X", kind="app", redirect="A_DIR"),
    ]
    synth, _ = inventory.synth_entities(ts)
    assert synth[0]["redirects"] == ["A_DIR", "UV_CACHE_DIR"]

    plain, _ = inventory.synth_entities(
        [trace(r"C:\x\d", owner="Y", kind="toolchain")])
    assert plain[0]["redirects"] is None


# ------------------------------------------------------------ 挂接匹配
def test_publisher_match_links_traces_without_a_name_hit():
    """owner 对不上软件名时按发布商挂——厂商目录靠的就是这条。"""
    ents = ledger(("GeForce Experience", "NVIDIA Corporation"))
    ts = [trace(r"C:\x\NVIDIA", owner="NVIDIA", kind="vendor", size=42)]
    _, unlinked = inventory.link_traces(ents, ts)
    assert not unlinked
    assert ents[0]["traces"] == ts
    assert ents[0]["traces_size"] == 42


def test_entities_without_traces_report_zero_not_none():
    """没痕迹的实体也要有 traces_size=0，否则前端求和得处理 undefined。"""
    ents = ledger(("Firefox", "Mozilla"))
    inventory.link_traces(ents, [])
    assert ents[0]["traces_size"] == 0
