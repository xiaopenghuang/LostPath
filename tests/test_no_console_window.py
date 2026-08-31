r"""子进程一律不弹控制台窗口（`lostpath/proc.py`）。

**这一族缺陷开发机上永远看不到。** 开发时引擎跑在 `python.exe` 下——控制台程序，
子进程继承现成的控制台，什么也不弹。打包后引擎是 PyInstaller 的 `runw.exe`
bootloader（GUI 子系统、无控制台），此时 Windows 会**给子进程新建一个控制台窗口**，
于是用户点一下「软件台账」或「迁移中心」（两者都拉 `/api/plan` → `running_process_dirs()`）
屏幕上就闪过一个 PowerShell 黑框。用户实际报的就是这个。

所以这里钉的不是"功能对不对"，是"每个 PowerShell 调用点都经过了 `run_hidden`"。
判据刻意做成**结构性**的（扫源码里还有没有裸 `subprocess.run` 调 powershell），
而不是去跑一遍看有没有窗口——后者在开发机上恒为"没有窗口"，是个永远绿的假测试。
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from lostpath.proc import NO_WINDOW, run_hidden

ROOT = Path(__file__).resolve().parent.parent

# 会起子进程的模块。新增 PowerShell 调用点时把文件加进来。
SCANNED_DIRS = ("lostpath", "engine")


def test_no_window_flag_is_the_real_constant():
    """NO_WINDOW 必须是 Windows 的 CREATE_NO_WINDOW，不是随手写的数字。"""
    assert NO_WINDOW == subprocess.CREATE_NO_WINDOW
    assert NO_WINDOW == 0x08000000


def test_run_hidden_injects_the_flag(monkeypatch):
    """run_hidden 必须把标志传下去——这是它唯一的职责。"""
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        seen["args"] = args
        return "sentinel"

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_hidden(["powershell", "-Command", "x"], capture_output=True, timeout=5)

    assert out == "sentinel", "不该改变 subprocess.run 的返回值"
    assert seen["creationflags"] & NO_WINDOW, "creationflags 里没有 CREATE_NO_WINDOW"
    # 其余参数原样透传，不能被吞掉
    assert seen["capture_output"] is True
    assert seen["timeout"] == 5
    assert seen["args"] == ["powershell", "-Command", "x"]


def test_run_hidden_preserves_caller_flags(monkeypatch):
    """调用方自己传了 creationflags 时按位或，不覆盖它的意图。"""
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda a, **k: seen.update(k))
    run_hidden(["x"], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    assert seen["creationflags"] & NO_WINDOW
    assert seen["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP


def _powershell_calls_without_run_hidden() -> list[str]:
    """找出仍在用裸 subprocess.run/Popen 起 powershell 的地方。

    用 AST 而不是正则找调用点，但判定"这次调用是不是 powershell"要看实参里的
    字面量——所以两者结合：AST 定位 `subprocess.run(...)` / `subprocess.Popen(...)`，
    再看该调用的源码片段里有没有 powershell。
    """
    bad = []
    for d in SCANNED_DIRS:
        for py in (ROOT / d).rglob("*.py"):
            if "test" in py.name:
                continue
            src = py.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src)
            except SyntaxError:  # pragma: no cover - 语法错时别的测试会先红
                continue
            lines = src.splitlines()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                # 只看 subprocess.run / subprocess.Popen 这种属性调用
                if not (isinstance(f, ast.Attribute)
                        and f.attr in ("run", "Popen")
                        and isinstance(f.value, ast.Name)
                        and f.value.id == "subprocess"):
                    continue
                seg = "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
                if re.search(r"powershell", seg, re.I):
                    rel = py.relative_to(ROOT).as_posix()
                    bad.append(f"{rel}:{node.lineno}")
    return bad


def test_every_powershell_call_goes_through_run_hidden():
    """全仓不许再出现裸 subprocess 调 powershell。

    这条是**结构性**判据，不依赖运行环境：新增一处调用点忘了用 `run_hidden`，
    它就红。三处历史调用点（planner / inventory_export / extract_icons）都是
    这么漏的——每处单独写 subprocess，谁也没想起来加标志。
    """
    bad = _powershell_calls_without_run_hidden()
    assert not bad, (
        "这些地方用裸 subprocess 起 powershell，打包后会弹控制台窗口，"
        f"改用 lostpath.proc.run_hidden：{bad}"
    )


def test_the_known_call_sites_still_use_run_hidden():
    """已知调用点必须还在用 run_hidden。

    上一条是"没有裸调用"，这条是"确实有在用" —— 缺了它，把 powershell 调用整个
    删掉也能让上一条通过（空集合恒真）。

    **`planner.py` 曾在这张表里，后来它不调 PowerShell 了**（改走
    `lostpath/winproc.py` 的 Win32 API，4.35s → 21.7ms）。这条断言当时红了一次，
    正是它该做的——名单与现实脱节就该有人吭一声。少一处调用点是**改进**，
    所以更新名单而不是把断言放宽。
    """
    expect = {
        "lostpath/scan/inventory_export.py": "扫描阶段导出软件清单",
        "engine/extract_icons.py": "启动时后台补图标",
    }
    for rel, why in expect.items():
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "run_hidden(" in src, f"{rel} 不再用 run_hidden（{why}）"
        assert re.search(r"powershell", src, re.I), f"{rel} 已不调 powershell？请更新本测试"


def test_planner_no_longer_spawns_powershell():
    """计划器不许再起子进程判"软件是否在跑"。

    这是性能回归闸：起一个 PowerShell 要 4.35 秒，而 `/api/plan` 每次都走这里，
    软件台账与迁移中心每次打开都得等它。改回 subprocess 就该红。
    """
    src = (ROOT / "lostpath/act/planner.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("run", "Popen")
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"
    ]
    assert not calls, f"planner 又起子进程了（第 {[c.lineno for c in calls]} 行）"
    assert "winproc" in src, "planner 应经 lostpath/winproc.py 取进程列表"


@pytest.mark.real_env_lookup
def test_run_hidden_actually_works():
    """真跑一次 PowerShell，确认没把功能一起关掉。

    加标志加错了（比如误用 DETACHED_PROCESS）会让管道拿不到输出，而"没有窗口"
    这个目标看起来照样达成 —— 所以要验它仍然能取到 stdout。
    """
    out = run_hidden(
        ["powershell", "-NoProfile", "-Command", "Write-Output LOSTPATH_OK"],
        capture_output=True, timeout=60,
    )
    assert out.returncode == 0
    assert b"LOSTPATH_OK" in out.stdout
