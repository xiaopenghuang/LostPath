"""归因基准回归。改归因逻辑后必须跑这个，wrong 与 missing 分开断言。

    python tests/test_attribution_baseline.py

装了 pytest 也可直接 `pytest tests/`（P3 会把它接进 CI）。写成双模式是因为
目前环境没装 pytest，而基准不该等到装了测试框架才能跑。

数据来自 tests/fixtures/machine-a/（脱敏快照，用户名已换成 devuser），因此
本基准不依赖开发者本机状态，换机器/换人接手都跑得出同一个数。

**fixtures 上跑不出 marker 证据，这是预期的**：KB.probe_markers() 要
os.path.exists() 探目录内标识文件、junction 要 os.readlink() 解目标，而
C:\\Users\\devuser\\... 在任何真机上都不存在。影响面已量化并锁在
test_only_marker_dependent_fields_differ 里：24 条丢 family、5 条因此丢归属。
若哪天差异面变了，那个测试会失败——它不是宽容，是把差异钉死。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from baseline_judge import tally  # noqa: E402
from lostpath.attribute import attribute_footprint  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures", "machine-a")
FIXTURE_USER_HOME = r"C:\Users\devuser"
# fixtures 采自一台系统盘为 C 的机器。**显式钉住，不许从 %ProgramData% 取**：否则
# 在系统装 D 盘的机器上会静默丢掉整个 ProgramData 分区的记录，而基准那几个数
# （27/0/0、条数、体积）就变成"取决于跑测试的人是谁"。这个文件刻意不 import 归因
# 模块的常量，同理也不该 import 环境。
FIXTURE_PROGRAM_DATA = r"C:\ProgramData"

# 依赖真实文件系统探测的证据源，在脱敏 fixtures 上必然缺失
FS_DEPENDENT_SOURCES = {
    "kb_marker_boost", "kb_marker_only", "unregistered_app", "junction_target",
}
# 上述证据缺失会波及的字段
FS_DEPENDENT_FIELDS = {"family", "conf", "owner", "owner_kind", "cat", "why"}

# 当前基线。改归因逻辑若动了这三个数，必须在提交信息里说明为什么。
EXPECTED = {"correct": 27, "wrong": 0, "missing": 0}
EXPECTED_ENTRIES = 106
EXPECTED_FS_DEPENDENT_RECORDS = 24

# 必须在 fixtures 上真实触发的证据源。列表存在的理由：appx_family 曾因索引键名
# 写错而一次都没触发，但汇总精度仍是 100%，所以没人发现——只有逐证据源点名，
# 死代码才藏不住。marker 类与 junction 类不在此列，fixtures 上探不到（见模块
# 顶部说明）。
EXPECTED_LIVE_EVIDENCE = {
    "appx_family", "msi_productcode", "container", "role_rule", "system_owned",
    "role_fixed", "role_stem", "role_stem_unreg", "kb_toolchain", "vendor_dir",
    "exact_name", "stripped_name", "kb_alias", "install_path_part", "shortcut",
    "rdns_segment", "rdns_unreg", "integration",
}


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8-sig") as f:
        return json.load(f)


def run_attribution():
    return attribute_footprint(load_fixture("scan_c.json"),
                               load_fixture("inventory.json"),
                               load_fixture("shortcuts.json"),
                               user_home=FIXTURE_USER_HOME,
                               program_data=FIXTURE_PROGRAM_DATA)


def test_truth_baseline():
    """truth.json 基准：correct 27 / wrong 0 / missing 0。"""
    records, _ = run_attribution()
    counts, detail = tally(records, load_fixture("truth.json"))
    bad = [d for d in detail if d["verdict"] != "correct"]
    assert counts == EXPECTED, (
        f"基准退化：{counts}，期望 {EXPECTED}\n" + "\n".join(
            f"  {d['size'] / 2**30:6.2f} GiB {d['name']:<26} {d['verdict']:<8} "
            f"基准={d['truth_kind']}/{d['truth_owner']} "
            f"预测={d['pred_kind']}/{d['pred_owner']}" for d in bad))


def test_entry_count_and_conservation():
    """条数与体积守恒：与存档快照同 106 条，且每条体积一致。"""
    records, stats = run_attribution()
    stored = load_fixture("attribution_v4.json")
    assert len(records) == EXPECTED_ENTRIES == len(stored)
    live = {r["path"].lower(): r for r in records}
    arch = {r["path"].lower(): r for r in stored}
    assert set(live) == set(arch), "足迹目录集合变了"
    mismatched = [p for p in arch
                  if live[p]["size"] != arch[p]["size"]
                  or live[p]["files"] != arch[p]["files"]]
    assert not mismatched, f"体积/文件数不守恒：{mismatched[:5]}"
    assert stats["total_size"] == sum(r["size"] for r in stored)


def test_only_marker_dependent_fields_differ():
    """实跑与存档的差异必须只出现在依赖文件系统探测的记录上。

    这条把"fixtures 上 marker 探不到"的影响面钉死。任何其他字段漂移都会
    在这里失败，包括本次搬迁若不小心改了算法。
    """
    records, _ = run_attribution()
    arch = {r["path"].lower(): r for r in load_fixture("attribution_v4.json")}
    fields = ("owner", "owner_kind", "family", "conf", "cat", "why", "role",
              "redirect", "zone", "name")

    fs_dependent, unexpected = set(), []
    for r in records:
        a = arch[r["path"].lower()]
        uses_fs = {e["source"] for e in a.get("evidence") or []} & FS_DEPENDENT_SOURCES
        differing = [f for f in fields if a.get(f) != r.get(f)]
        if not differing:
            continue
        if uses_fs and set(differing) <= FS_DEPENDENT_FIELDS:
            fs_dependent.add(r["path"])
            continue
        unexpected.append((r["name"], differing,
                           {f: (a.get(f), r.get(f)) for f in differing}))

    assert not unexpected, (
        "出现与文件系统探测无关的字段漂移（疑似算法被改动）：\n" + "\n".join(
            f"  {n}: {d} {v}" for n, d, v in unexpected))
    assert len(fs_dependent) == EXPECTED_FS_DEPENDENT_RECORDS, (
        f"受 marker 缺失影响的记录数变为 {len(fs_dependent)}，"
        f"期望 {EXPECTED_FS_DEPENDENT_RECORDS}")


def test_no_dead_evidence_source():
    """每个应生效的证据源都得真的触发过，且索引不得静默为空。

    这条守的是"引擎坏了但指标好看"这一类失效。v1 曾因读错字段名让最强证据源
    全程失效，汇总覆盖率却有 71.4%；appx_family 也曾长期 0 命中而基准满分。
    """
    _, stats = run_attribution()
    hits = stats["evidence_hits"]
    dead = sorted(s for s in EXPECTED_LIVE_EVIDENCE if not hits.get(s))
    assert not dead, (
        f"这些证据源一次都没触发，疑似索引键名对不上或规则失效：{dead}\n"
        f"实际命中：{hits}")
    assert not stats["index_warnings"], (
        f"索引健康检查报警：{stats['index_warnings']}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {fn.__name__}\n{e}\n")
        else:
            print(f"[ OK ] {fn.__name__}")

    records, stats = run_attribution()
    counts, detail = tally(records, load_fixture("truth.json"))
    print(f"\n基准 {sum(counts.values())} 条：{counts}  "
          f"（足迹 {stats['entries']} 条 / "
          f"{stats['total_size'] / 2**30:.2f} GiB，"
          f"未归因 {stats['unknown_size'] / 2**30:.2f} GiB，"
          f"耗时 {stats['elapsed_sec']}s）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
