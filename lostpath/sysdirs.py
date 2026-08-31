r"""系统盘与系统目录的解析。全部只读，且**不依赖包内任何其他模块**。

**为什么值得单独一个模块。** 本项目原先有四处把系统盘写死成 `C`，其中
`act/executor.py` 那处是"执行器的最后一道闸"，而同一个概念
`attribute/lostpath_kb.high_risk()` 早已泛化过盘符、还有测试钉着
（`test_high_risk_not_hardcoded_to_c_drive`）——**同一件事在两处实现、只修了一处**。
这正是本项目反复出现的"两个缺陷互相掩盖"的形状，掩盖它的是"大家的 Windows 通常在
C 盘"。抽成一个模块，是为了让"系统盘在哪"只有一个答案，而不是每处各写一遍。

**为什么不接受调用方传参。** 系统盘是机器的事实，不是调用方的选择。参数化只会引入
路径注入面（UNC 路径能把扫描指到网络共享）而换不来任何功能。所以这里只读环境变量，
且**必须过严格正则才采用**——`[A-Za-z]:` 通不过就退回默认值，UNC 一律进不来。

**为什么环境变量缺失时退回而不抛异常。** 这些函数在扫描与安全闸两条路上都要用。
宁可给出最常见的答案继续跑，也不能因为一个环境变量缺失让整个程序起不来；而安全闸
那一侧另有一组盘符无关的判据兜底（见 `protected_system_dir`），不靠环境变量。
"""
import os
import re

_DRIVE_RE = re.compile(r"^[A-Za-z]:$")


def _drive_of(path: str) -> str | None:
    """取路径的本地盘符（形如 `C:`）。拿不到、或是 UNC，一律 None。"""
    drive = os.path.splitdrive((path or "").strip())[0]
    return drive.upper() if _DRIVE_RE.match(drive) else None


def system_drive() -> str:
    r"""系统盘盘符，形如 `C:`。

    优先 `%SystemDrive%`；缺了从 `%SystemRoot%` / `%windir%` 推盘符；都拿不到才退
    `C:`（见模块 docstring 里"为什么退回而不抛异常"）。
    """
    for candidate in (os.environ.get("SystemDrive"),
                      os.environ.get("SystemRoot"),
                      os.environ.get("windir")):
        drive = _drive_of(candidate or "")
        if drive:
            return drive
    return "C:"


def system_drive_root() -> str:
    r"""系统盘根，形如 `C:\`。扫描的唯一入口扫的就是这个。"""
    return system_drive() + "\\"


def program_data_dir() -> str:
    r"""`%ProgramData%`，通常是 `C:\ProgramData`。

    环境变量缺失或不是本地绝对路径时按系统盘拼——那是 Windows 的固定布局。
    """
    value = (os.environ.get("ProgramData") or "").strip().rstrip("\\")
    if _drive_of(value) and os.path.isabs(value):
        return value
    return os.path.join(system_drive_root(), "ProgramData")


# ---------------------------------------------------------------- 系统目录保护
# ① 盘符无关的固定名。Windows 的布局里这些名字在**任何**盘上都不该被本工具动，
#    所以比对的是剥掉盘符后的尾巴。环境变量全没了也还有这一组。
_PROTECTED_TAILS = (
    ("\\windows", "Windows 系统目录"),
    ("\\program files", "程序安装目录"),
    ("\\program files (x86)", "程序安装目录（32 位）"),
    ("\\programdata\\microsoft\\windows", "系统级 Microsoft\\Windows 数据"),
)

# ② 环境变量给出的真实路径，覆盖装到非默认位置的情形（如 `D:\Apps` 当 ProgramFiles）。
#    **`ProgramData` 自身刻意不在这一组**：清理 `ProgramData\<某软件>` 正是本工具的
#    活，只有它下面的 `Microsoft\Windows` 不能动，那条已在 ① 里。
_PROTECTED_ENV_VARS = ("SystemRoot", "windir", "ProgramFiles",
                       "ProgramFiles(x86)", "ProgramW6432")


def protected_system_dir(path: str) -> str | None:
    r"""落在系统目录里就返回原因，否则 None。

    两组判据取并集，冗余是刻意的：① 不因系统装在 D 盘而失守，也不依赖环境变量；
    ② 覆盖装到非默认位置的情形。**安全闸宁可多拦**——误拦的后果是拒绝执行，漏拦的
    后果是删掉系统目录，两者代价差着量级。UNC 路径同样照拦（`splitdrive` 会把
    `\\server\share` 剥掉，尾巴照常比对）。
    """
    low = os.path.abspath(path).lower().rstrip("\\")
    tail = os.path.splitdrive(low)[1]
    for bad, why in _PROTECTED_TAILS:
        if tail == bad or tail.startswith(bad + "\\"):
            return why
    for var in _PROTECTED_ENV_VARS:
        value = (os.environ.get(var) or "").strip().rstrip("\\").lower()
        if value and (low == value or low.startswith(value + "\\")):
            return f"%{var}% 指向的系统目录"
    return None
