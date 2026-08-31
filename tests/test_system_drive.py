r"""系统盘不一定是 C。

**这不是为极少数机器过度设计。** 本项目原先有四处把系统盘写死成 `C`，其中两处的失败
是静默的：`act/executor.py` 的 `FORBIDDEN_PREFIXES`（自称"执行器的最后一道闸"）在系统
装 D 盘的机器上对 `d:\windows` 完全不设防；`footprint_roots()` 里写死的
`C:\ProgramData` 会让整个 ProgramData 足迹根**扫不到**（不是扫错，是漏）。

而 `lostpath_kb.high_risk()` 早就泛化过盘符、还有测试钉着
（`test_classify_tokens.py:test_high_risk_not_hardcoded_to_c_drive`）——**同一件事在
两处实现、只修了一处**，这是本项目"两个缺陷互相掩盖"的又一例，掩盖它的是"大家的
Windows 通常在 C 盘"。这个文件把另外四处一起钉住。

**测试自己也不许依赖跑它的机器**：一律 monkeypatch 环境变量，不读本机真实布局。
"""
import os

import pytest

from lostpath import sysdirs


# ------------------------------------------------------------ 系统盘解析
def test_system_drive_prefers_system_drive_var(monkeypatch):
    monkeypatch.setenv("SystemDrive", "D:")
    assert sysdirs.system_drive() == "D:"
    assert sysdirs.system_drive_root() == "D:\\"


def test_system_drive_falls_back_to_system_root(monkeypatch):
    """%SystemDrive% 缺失时从 %SystemRoot% 推盘符。"""
    monkeypatch.delenv("SystemDrive", raising=False)
    monkeypatch.setenv("SystemRoot", r"E:\Windows")
    assert sysdirs.system_drive() == "E:"


def test_system_drive_falls_back_to_c_when_nothing_is_set(monkeypatch):
    """三个变量都没有才退 C:——退回而不抛异常，扫描与安全闸都要用这个函数。"""
    for var in ("SystemDrive", "SystemRoot", "windir"):
        monkeypatch.delenv(var, raising=False)
    assert sysdirs.system_drive() == "C:"


@pytest.mark.parametrize("bogus", [
    r"\\server\share",     # UNC：参数化扫描根的注入面，正是要挡的
    r"\\?\C:",
    "C",                   # 少个冒号
    "",
    "   ",
    "CC:",
])
def test_unc_and_garbage_never_become_the_system_drive(monkeypatch, bogus):
    """通不过 [A-Za-z]: 的一律退回，UNC 进不来。"""
    monkeypatch.setenv("SystemDrive", bogus)
    for var in ("SystemRoot", "windir"):
        monkeypatch.delenv(var, raising=False)
    assert sysdirs.system_drive() == "C:"


def test_lowercase_drive_is_normalised(monkeypatch):
    monkeypatch.setenv("SystemDrive", "d:")
    assert sysdirs.system_drive() == "D:"


# ------------------------------------------------------------ ProgramData
def test_program_data_uses_the_env_var(monkeypatch):
    monkeypatch.setenv("ProgramData", r"D:\ProgramData")
    assert sysdirs.program_data_dir() == r"D:\ProgramData"


def test_program_data_falls_back_to_system_drive(monkeypatch):
    """变量缺失时按系统盘拼——Windows 的固定布局。"""
    monkeypatch.delenv("ProgramData", raising=False)
    monkeypatch.setenv("SystemDrive", "E:")
    assert sysdirs.program_data_dir() == r"E:\ProgramData"


def test_program_data_rejects_unc(monkeypatch):
    monkeypatch.setenv("ProgramData", r"\\server\share\ProgramData")
    monkeypatch.setenv("SystemDrive", "C:")
    assert sysdirs.program_data_dir() == r"C:\ProgramData"


# ------------------------------------------------------------ 系统目录保护
@pytest.mark.parametrize("path", [
    r"D:\Windows",
    r"D:\Windows\System32",
    r"E:\Program Files",
    r"E:\Program Files\Something",
    r"F:\Program Files (x86)\Old",
    r"D:\ProgramData\Microsoft\Windows\Start Menu",
])
def test_system_dirs_are_protected_on_any_drive(path):
    assert sysdirs.protected_system_dir(path), f"{path} 没被拦住"


@pytest.mark.parametrize("path", [
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\ProgramData\Microsoft\Windows",
])
def test_c_drive_system_dirs_still_protected(path):
    """反向守门：泛化盘符不能把原来拦住的放过去。"""
    assert sysdirs.protected_system_dir(path)


def test_protection_survives_a_wiped_environment(monkeypatch):
    """环境变量全没了也要拦住——这是"盘符无关固定名"那一组存在的理由。"""
    for var in ("SystemRoot", "windir", "ProgramFiles",
                "ProgramFiles(x86)", "ProgramW6432", "SystemDrive"):
        monkeypatch.delenv(var, raising=False)
    assert sysdirs.protected_system_dir(r"D:\Windows\System32")


def test_program_files_installed_off_default_is_protected(monkeypatch):
    r"""装到非默认位置也要拦——这是"环境变量真实路径"那一组存在的理由。

    `D:\Apps` 的尾巴是 `\apps`，固定名那组抓不到它，只有环境变量那组能。
    """
    monkeypatch.setenv("ProgramFiles", r"D:\Apps")
    assert sysdirs.protected_system_dir(r"D:\Apps\SomeVendor")


@pytest.mark.parametrize("path", [
    r"C:\ProgramData\SomeApp",
    r"D:\ProgramData\SomeApp\cache",
    r"C:\Users\someone\AppData\Local\Temp",
    r"C:\Users\someone\AppData\Local\Windows",   # 尾巴不是 \windows 开头
    r"D:\MyStuff\Program Files Backup",
])
def test_ordinary_targets_are_not_protected(path):
    r"""**反向守门，比正向更要紧**：拦过头本工具就没活干了。

    `ProgramData\<某软件>` 正是要清理的对象，只有它下面的 `Microsoft\Windows` 不能动。
    """
    assert sysdirs.protected_system_dir(path) is None, (
        f"{path} 被误拦，本工具会没法干正事")


# ------------------------------------------------------------ 四处接线
def test_executor_guard_refuses_system_dirs_on_any_drive():
    """执行器的最后一道闸走同一份判据。"""
    from lostpath.act import executor

    with pytest.raises(executor.ExecutionRefused, match="系统目录"):
        executor._guard(r"D:\Windows\System32")


def test_executor_guard_still_refuses_drive_roots(tmp_path):
    """盘根那条原有拦阻不能被这次改动带坏。"""
    from lostpath.act import executor

    with pytest.raises(executor.ExecutionRefused, match="盘根"):
        executor._guard("D:\\")


def test_scan_root_follows_the_system_drive(monkeypatch):
    r"""扫描根取系统盘。

    **这条必须是函数而不是模块常量才测得动。** 常量在 import 时就算完了，等测试
    monkeypatch 环境变量已经太晚——本机系统盘恰好是 C，那样写出来的断言
    `SCAN_ROOT == system_drive_root()` 两边都是 `C:\`，永远绿，什么也没证明。
    这正是"拿手边这台机器当验收标准"的典型形状。
    """
    from lostpath.scan import runner

    monkeypatch.setenv("SystemDrive", "D:")
    assert runner.scan_root() == "D:\\"
    monkeypatch.setenv("SystemDrive", "C:")
    assert runner.scan_root() == "C:\\"


def test_scan_root_is_still_not_caller_controlled():
    """泛化盘符不能顺手把"不接受调用方传入"这条设计约束破掉。"""
    import inspect

    from lostpath.scan import runner

    assert "root" not in inspect.signature(runner.run_pipeline).parameters
    assert not inspect.signature(runner.scan_root).parameters


def test_scan_tree_default_root_is_resolved_at_call_time():
    r"""`scan_tree` 的默认根不许是字面量。

    默认值在 def 时求值，写 `root="C:\\"` 就把它冻死了；改成 `None` 并在函数体内
    向 sysdirs 要，才会跟着机器走。
    """
    import inspect

    from lostpath.scan import scan_dirs

    default = inspect.signature(scan_dirs.scan_tree).parameters["root"].default
    assert default is None, f"默认根还是写死的：{default!r}"


def test_footprint_roots_uses_program_data_env(monkeypatch):
    """归因的四个足迹根里，ProgramData 那个要跟着环境变量走。"""
    from lostpath.attribute import attribute_v4 as A

    monkeypatch.setenv("ProgramData", r"D:\ProgramData")
    roots = dict((zone, path) for path, zone in
                 A.footprint_roots(user_home=r"C:\Users\devuser"))
    assert roots["ProgramData"] == r"D:\ProgramData"


def test_footprint_roots_program_data_is_parameterisable():
    r"""必须能被显式指定，否则基准会随跑测试的机器变。

    脱敏 fixtures 里的路径是 `C:\ProgramData`（采集时那台机器的系统盘是 C）。若这个
    根只能从环境变量取，在系统装 D 盘的机器上跑基准就会**静默丢掉整个 ProgramData
    分区的记录**，而条数、体积、准确率全都跟着变——正是本项目禁止的"指标取决于跑测试
    的人是谁"。所以它跟 `user_home` 一样必须可传入。
    """
    from lostpath.attribute import attribute_v4 as A

    roots = dict((zone, path) for path, zone in A.footprint_roots(
        user_home=r"C:\Users\devuser", program_data=r"C:\ProgramData"))
    assert roots["ProgramData"] == r"C:\ProgramData"


@pytest.mark.parametrize("path", [
    r"C:\ProgramData\Package Cache",
    r"D:\ProgramData\Package Cache",
])
def test_package_cache_container_matches_any_drive(path):
    """容器规则也写死过 C，导致别的机器上它不被识别成容器、子目录不下钻。"""
    from lostpath.attribute import lostpath_kb as KB

    assert KB.container_of(path), f"{path} 没被识别为容器"


# ----------------------------------------------------- 快捷方式采集根（第五处）
def test_lnk_roots_follow_program_data_and_public(monkeypatch):
    r"""快捷方式采集根也写死过两处 C，系统装 D 盘的机器上它们整批扫不到。

    这两处是 R4（快捷方式目标 exe）唯一证据源的一部分——漏掉它，跨盘软件的本体
    定位就少一半依据，而表现是"注册表查不到、显示未归因"，不是报错。
    """
    from lostpath.scan import collect_evidence as CE

    monkeypatch.setenv("ProgramData", r"D:\ProgramData")
    monkeypatch.setenv("PUBLIC", r"E:\Users\Public")
    roots = [r.rstrip("\\") for r in CE.default_lnk_roots(user_home=r"C:\Users\devuser")]
    assert r"D:\ProgramData\Microsoft\Windows\Start Menu" in roots, roots
    assert r"E:\Users\Public\Desktop" in roots, roots
    # 用户自己的两处按 expanduser 走，不受影响
    assert r"C:\Users\devuser\AppData\Roaming\Microsoft\Windows\Start Menu" in roots
    assert r"C:\Users\devuser\Desktop" in roots


def test_lnk_roots_are_parameterisable():
    r"""与 footprint_roots 同因：fixtures 路径是 C:\Users\devuser，必须能钉住。"""
    from lostpath.scan import collect_evidence as CE

    roots = [r.rstrip("\\") for r in CE.default_lnk_roots(
        user_home=r"C:\Users\devuser", program_data=r"C:\ProgramData",
        public_user=r"C:\Users\Public")]
    assert r"C:\ProgramData\Microsoft\Windows\Start Menu" in roots, roots
    assert r"C:\Users\devuser\Desktop" in roots, roots


def test_lnk_roots_public_falls_back_to_system_drive(monkeypatch):
    """%PUBLIC% 缺失时按系统盘拼 Windows 固定布局。"""
    from lostpath.scan import collect_evidence as CE

    monkeypatch.delenv("PUBLIC", raising=False)
    monkeypatch.setenv("SystemDrive", "F:")
    roots = [r.rstrip("\\") for r in CE.default_lnk_roots(
        user_home=r"C:\Users\devuser")]
    assert r"F:\Users\Public\Desktop" in roots, roots
