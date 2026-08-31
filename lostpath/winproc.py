r"""正在运行的进程的可执行文件路径（只读，Win32 API，不起子进程）。

**为什么不用 PowerShell**：原先这件事是
`powershell -Command "Get-Process | ... | ForEach-Object {$_.Path}"`，实测 **4.35 秒**
——而 `/api/plan` 每次都要走它，于是软件台账和迁移中心每次打开都卡四秒多。慢的不是
枚举本身，是起一个 PowerShell 进程（加载 .NET runtime、解析 profile 策略、编译脚本块）。

改用 `CreateToolhelp32Snapshot` + `QueryFullProcessImageNameW` 之后实测 **21.7 毫秒**，
快约 200 倍，而且拿到的目录**更多**（76 vs 74）：PowerShell 版对某些进程取 `.Path`
会抛异常并被整条管道吞掉，而这里逐个进程失败只丢那一个。

顺带解决了另一个问题：不起子进程就不存在"打包后弹控制台窗口"那一类缺陷
（见 `lostpath/proc.py` 的说明）。

**权限**：用 `PROCESS_QUERY_LIMITED_INFORMATION`（Vista+）而不是
`PROCESS_QUERY_INFORMATION`——前者对更高权限的进程也常常够用，且不需要提权。
打不开的进程直接跳过：拿不到就是"未知"，判断上按"没在跑"处理，与旧行为一致
（旧版整条管道抛异常时也是这个结果，只是它一次丢全部）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys

__all__ = ["running_process_dirs", "running_process_paths"]

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_PATH = 260


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_char * _MAX_PATH),
    ]


def _pids() -> list[int]:
    """当前所有进程的 pid。拿不到快照就返回空列表。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == _INVALID_HANDLE_VALUE or not snap:
        return []
    out: list[int] = []
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        if k32.Process32First(snap, ctypes.byref(entry)):
            while True:
                out.append(int(entry.th32ProcessID))
                if not k32.Process32Next(snap, ctypes.byref(entry)):
                    break
    finally:
        k32.CloseHandle(snap)
    return out


def running_process_paths() -> set[str]:
    """正在运行的进程的可执行文件全路径（原样大小写）。

    非 Windows 上返回空集合——本工具只跑 Windows，但 import 与调用都不该炸。
    """
    if sys.platform != "win32":
        return set()
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 显式声明签名：ctypes 默认把指针当 int 处理，64 位下句柄会被截断
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.OpenProcess.restype = wt.HANDLE
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD),
    ]
    k32.QueryFullProcessImageNameW.restype = wt.BOOL

    paths: set[str] = set()
    # 32767 是 \\?\ 前缀下的路径上限；一次分配复用，别在循环里反复建缓冲
    buf = ctypes.create_unicode_buffer(32768)
    for pid in _pids():
        # 0 = System Idle、4 = System，都没有可执行路径，省两次失败的 OpenProcess
        if pid <= 4:
            continue
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue  # 权限不够或进程已退出——按"未知"处理
        try:
            size = wt.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                if buf.value:
                    paths.add(buf.value)
        finally:
            k32.CloseHandle(handle)
    return paths


def running_process_dirs() -> set[str]:
    """正在运行的进程的可执行文件所在目录（**小写**，供路径比较用）。

    调用方拿它判断"这个目录下的软件是否正在运行"，所以统一小写——Windows 路径
    不区分大小写，而两边来源不同（一边是快照里的路径，一边是进程路径）。
    """
    return {os.path.dirname(p).lower() for p in running_process_paths() if p}


def is_elevated() -> bool:
    """当前进程有没有管理员权限。

    放在这里而不是 `engine/main.py`：扫描管道要把它写进快照信封（见
    `scan/runner.py` 的 `scan_stats.elevated`），而 `lostpath/` 不该反向 import
    `engine/`。两处现在共用这一个实现，不会各写一份然后走偏。

    非管理员会有扫描盲区，这件事得让用户看见，不能只写在文档里。
    """
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
