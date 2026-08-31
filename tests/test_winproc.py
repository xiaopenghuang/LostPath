r"""进程列表用 Win32 API 取（`lostpath/winproc.py`）。

这个模块存在的理由是**性能**：原先 `planner.running_process_dirs()` 起 PowerShell
跑 `Get-Process`，实测 4.35 秒，而 `/api/plan` 每次都要它——软件台账与迁移中心
每次打开都卡四秒多。换 `CreateToolhelp32Snapshot` 后 21.7ms。

所以这里既钉正确性（拿到的确实是当前进程、格式对），也钉一条**耗时上限**。
耗时断言给的阈值很宽（1 秒），只为拦住"又改回起子进程"这一类退化，不为在
忙机器上制造假失败。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from lostpath import winproc

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 专用")


def test_finds_the_current_python_process():
    """当前这个 python 进程自己必须在结果里——最硬的正确性判据。

    比"结果非空"强得多：非空只说明拿到了些东西，这条说明拿到的是**真实的、
    此刻在跑的**进程，且路径解析正确。
    """
    paths = winproc.running_process_paths()
    assert paths, "一个进程都没拿到"
    me = os.path.realpath(sys.executable).lower()
    got = {os.path.realpath(p).lower() for p in paths}
    assert me in got, f"没找到当前解释器 {me}"


def test_dirs_are_lowercased_dirnames():
    """dirs 版必须是小写目录名——调用方拿它和快照里的路径比。

    大小写不统一会让比较**静默失效**：Windows 路径不区分大小写，但字符串比较区分，
    于是"软件正在运行"这条拦阻会漏判，用户可能在软件开着时迁移它的目录。
    """
    dirs = winproc.running_process_dirs()
    assert dirs
    for d in dirs:
        assert d == d.lower(), f"没小写：{d}"
        # dirname 的结果不该带尾部分隔符（盘根除外）
        assert not d.endswith("\\") or len(d) <= 3, f"尾部有多余分隔符：{d}"
    mine = os.path.dirname(os.path.realpath(sys.executable)).lower()
    assert mine in dirs


def test_no_subprocess_is_spawned(monkeypatch):
    """一个子进程都不许起。

    这是这个模块的**存在理由**：不起子进程既快 200 倍，也不会在打包后弹控制台
    窗口（见 lostpath/proc.py）。把 subprocess 的两个入口都换成会炸的替身，
    真起了子进程就会红。
    """
    import subprocess

    def boom(*a, **k):  # pragma: no cover - 触发即失败
        raise AssertionError("winproc 起了子进程")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert winproc.running_process_dirs()


def test_fast_enough():
    """耗时上限。阈值 1 秒——起一个 PowerShell 就要 4 秒以上，够拦住退化。

    实测约 22~32ms，留了 30 倍余量：这条测试是防退化的闸，不是性能基准，
    在忙机器上也不该红。
    """
    t = time.perf_counter()
    winproc.running_process_dirs()
    elapsed = time.perf_counter() - t
    assert elapsed < 1.0, (
        f"取进程列表花了 {elapsed:.2f}s。改回起子进程了？"
        "（PowerShell 版实测 4.35s，Win32 版 22ms）"
    )


def test_survives_snapshot_failure(monkeypatch):
    """拿不到快照时返回空集合，不抛异常。

    调用方（planner）把空集合当作"没有软件在跑"，这与旧版异常兜底时的行为一致。
    这里要保证的是**不炸**——计划器不能因为读不到进程列表就整个出不来。
    """
    monkeypatch.setattr(winproc, "_pids", lambda: [])
    assert winproc.running_process_paths() == set()
    assert winproc.running_process_dirs() == set()


def test_skips_system_pids(monkeypatch):
    """pid <= 4 直接跳过：它们没有可执行路径，省两次注定失败的 OpenProcess。"""
    seen = []
    real = winproc._pids
    monkeypatch.setattr(winproc, "_pids", lambda: [0, 4, *real()])
    # 不直接断言"没调 OpenProcess(0)"（那要 hook WinDLL），改为断言结果里
    # 不会因为这两个 pid 多出空路径
    paths = winproc.running_process_paths()
    assert all(p for p in paths), "结果里有空路径"
    assert seen == []


def test_is_elevated_returns_bool():
    """提权判断必须给出真正的 bool，不是 truthy 的整数。

    `IsUserAnAdmin()` 返回 BOOL（本质是 int），直接透出去会让快照信封里存 0/1
    而不是 false/true，前端 `=== false` 的判断就落空 —— 而"数据待更新"正是靠
    `snapshot.elevated === false` 判的，落空则提示永不出现。
    """
    v = winproc.is_elevated()
    assert isinstance(v, bool), f"应为 bool，实际 {type(v).__name__}"


def test_only_one_implementation_of_is_elevated():
    """`engine/main.py` 不许再自带一份提权判断，必须转发到这里。

    扫描管道要把同一件事写进快照（`scan_stats.elevated`），两处各写一份实现
    早晚走偏 —— 本项目已有过"同一个概念两处实现只修了一处"的事故
    （HANDOVER §7 第 13 条：`high_risk` 泛化了盘符，而执行器的安全闸没有）。
    """
    src = (ROOT / "engine/main.py").read_text(encoding="utf-8")
    assert "winproc.is_elevated()" in src, "engine 应转发到 lostpath.winproc"
    assert "IsUserAnAdmin" not in src, "engine 里又出现了第二份提权判断实现"
