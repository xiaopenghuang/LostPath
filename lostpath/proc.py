"""子进程启动的唯一入口。

**存在的理由只有一个：不让黑框闪出来。**

打包后的引擎是 GUI 子系统程序（PyInstaller 的 `runw.exe` bootloader，没有控制台）。
在这种进程里 `subprocess.run(["powershell", ...])` 会让 Windows **给子进程新建一个
控制台窗口** —— 用户点一下「软件台账」或「迁移中心」，屏幕上就闪过一个 PowerShell
黑框。开发时引擎跑在 `python.exe` 下（控制台程序），子进程继承现成的控制台，
什么也不弹，所以这个缺陷在开发机上永远看不到，**只有打包后才暴露**。

修法是给每个子进程传 `CREATE_NO_WINDOW`。放在这里而不是三处各写一遍，是因为
"新增一处 PowerShell 调用忘了加标志"是必然会发生的事 —— 让它没有别处可写。
"""
from __future__ import annotations

import subprocess
import sys

__all__ = ["run_hidden", "NO_WINDOW"]

# 仅 Windows 有这个标志；别的平台上取 0（按位或进去等于没加）。
# 本项目只跑 Windows，写成条件取值是为了 import 本身不炸——测试收集阶段
# 在任何平台都要能 import 到这个模块。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def run_hidden(args, **kwargs):
    """`subprocess.run` 的替身，保证不弹窗。

    除了固定注入 `creationflags` 之外不改变任何行为：超时、捕获、返回值都交给
    调用方，异常也照原样抛出。**调用方已经传了 creationflags 时按位或上去**，
    不覆盖它的意图。
    """
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | NO_WINDOW
    return subprocess.run(args, **kwargs)
