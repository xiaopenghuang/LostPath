"""pytest 共用装置。

**头号约束：测试绝不能碰真实的 %LOCALAPPDATA%\\LostPath。** 扫描管道会写快照、
归档旧快照、写日志，跑一次测试就把用户真实数据覆盖掉是不可接受的。所以
isolated_data_dir 是 autouse——不需要每个测试记得去隔离，忘了也不会出事。
"""
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(REPO), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

FIXTURES = HERE / "fixtures" / "machine-a"
FIXTURE_USER_HOME = r"C:\Users\devuser"
# fixtures 是在一台系统盘为 C 的机器上采的，所以 ProgramData 的路径是 C:\ProgramData。
# **必须显式钉住，不能让它从 %ProgramData% 取**：否则在系统装 D 盘的机器上跑基准会
# 静默丢掉整个 ProgramData 分区的记录，条数/体积/准确率跟着变，就成了"指标取决于跑
# 测试的人是谁"——本项目明令禁止的那类。
FIXTURE_PROGRAM_DATA = r"C:\ProgramData"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """把用户数据目录指到临时目录，autouse 覆盖所有测试。

    lostpath.storage.paths 读 LOSTPATH_DATA_DIR 环境变量，这是它存在的原因之一
    （见 paths.py 的设计约束）。monkeypatch 会在测试结束后自动还原。
    """
    d = tmp_path / "lostpath-data"
    monkeypatch.setenv("LOSTPATH_DATA_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def guard_real_user_data(isolated_data_dir):
    """兜底断言：确认隔离真的生效了，而不是自以为生效。

    单靠"我设了环境变量"不够——paths 若哪天改成缓存首次解析的结果，隔离就会失效
    而测试照样通过，然后某次跑测试把用户快照冲掉。所以每个测试前实际问一次
    paths 现在指向哪。
    """
    from lostpath.storage import paths

    root = paths.data_root()
    real_local = os.environ.get("LOCALAPPDATA")
    assert str(root).startswith(str(isolated_data_dir)), (
        f"数据目录隔离失效，当前指向 {root}")
    if real_local:
        assert not str(root).startswith(str(Path(real_local) / "LostPath")), (
            "数据目录指向了真实用户目录，测试会覆盖用户快照")


@pytest.fixture(autouse=True)
def unset_env_vars(monkeypatch, request):
    r"""让计划器看到"所有重定向变量都没设过"，autouse 覆盖所有测试。

    **为什么必须打桩。** 计划器出计划前要读 `UV_CACHE_DIR` 这类变量的现值
    （见 planner.env_var_state 的理由）。而开发机上这些变量**恰好是设过的**——
    直接读真注册表，同一条断言 `action == "redirect"` 在设过的机器上会得到
    `cleanup`，在没设的机器上得到 `redirect`，测试结果取决于跑它的人是谁。
    本项目已经吃过多次"指标随机器变化"的亏，所以默认一律隔离。

    要测"已设过"那两条分支的用例，自己传一个 env_lookup 进 plan_for，
    别改这里——这里代表的是"干净机器"这个基线。

    带 `real_env_lookup` 标记的用例退出打桩（用于测 effective_var 自身的组合
    逻辑），那些用例仍不许碰真注册表，得自己打桩底层两个读函数。
    """
    if "real_env_lookup" in request.keywords:
        return
    monkeypatch.setattr(
        "lostpath.act.envvar.effective_var", lambda name: (None, None))


def load_fixture(name):
    """读脱敏基准数据。utf-8-sig 是因为 PowerShell 产出的那份带 BOM。"""
    import json

    with open(FIXTURES / name, encoding="utf-8-sig") as f:
        return json.load(f)


@pytest.fixture
def fixture_loader():
    return load_fixture


@pytest.fixture
def fake_collectors():
    """用 fixtures 冒充三个采集器，让管道测试不必真扫盘。

    真扫一次十几秒且结果随机器变化；管道自身的逻辑（阶段推进、取消检查点、
    写快照前先归档）不该依赖全盘扫描来验。
    """
    scan = load_fixture("scan_c.json")
    inventory = load_fixture("inventory.json")
    shortcuts = load_fixture("shortcuts.json")

    calls = {"scan": 0, "inventory": 0, "shortcuts": 0, "progress": []}

    def scan_fn(root, progress=None, should_cancel=None):
        calls["scan"] += 1
        # 模拟真实扫描会周期性回报进度并给取消检查点
        for i in (1, 2, 3):
            if should_cancel and should_cancel():
                from lostpath.scan.scan_dirs import ScanCancelled
                raise ScanCancelled()
            if progress:
                progress(i * 1000, f"{root}fake{i}")
                calls["progress"].append(i * 1000)
        return scan

    def inventory_fn():
        calls["inventory"] += 1
        return inventory

    def shortcuts_fn():
        calls["shortcuts"] += 1
        return shortcuts

    return {"scan_fn": scan_fn, "inventory_fn": inventory_fn,
            "shortcuts_fn": shortcuts_fn, "calls": calls}
