# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller 规格：把引擎打成单个 exe。

    python -m PyInstaller tools/engine.spec --noconfirm

产物 dist/lostpath-engine.exe。桌面壳 spawn 它，从此不再需要目标机器上有 conda
或 Python——那三项硬编码（conda 路径、环境名、源码目录）是本项目最后的发布阻塞。

三件容易漏的：

1. **ui/dist 必须一起打进去。** 引擎用 StaticFiles 把界面挂在 /，少了它服务能起来
   但页面全白——而且这种失败很安静，只有打开浏览器才看得见。
2. **export_inventory.ps1 是运行期读的数据文件**，不是被 import 的模块，PyInstaller
   不会自动收。少了它扫描第二阶段直接失败。
3. **console=False**。留着控制台的话每次启动都闪一个黑窗，而壳本身已经是 GUI。
4. **conda 的 OpenSSL DLL 要手动带上。** conda 把 libssl/libcrypto 放在
   `Library/bin` 而不是 `DLLs`，PyInstaller 扫不到，打出来的 exe 一启动就
   `ImportError: DLL load failed while importing _ssl`（uvicorn 导入 ssl 就炸）。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent          # SPECPATH 由 PyInstaller 注入
UI_DIST = ROOT / "ui" / "dist"

# 解释器所在环境的 Library/bin。源码跑在 conda 环境里，这个目录一定存在；
# 换成非 conda 的 Python 时它不存在，此时系统自带的 DLL 能被正常发现，跳过即可。
#
# 整批带上而不是逐个点名：这类缺失是一个接一个暴露的（先 _ssl 缺 libssl，补上后
# _ctypes 缺 ffi-8，再往后还有 sqlite3、liblzma…），逐个补等于打地鼠，而且每次
# 都要重新打包才能发现下一个。这些库总共十几 MB，宁可多带。
#
# 排除项说明：api-ms-win-* 是系统 API 集转发桩，Windows 自带且版本必须与系统一致，
# 带上反而可能在别的机器上冲突；tcl/tk 是 GUI 工具包，本项目界面在浏览器里，用不到。
CONDA_BIN = Path(sys.executable).parent / "Library" / "bin"
SKIP_PREFIX = ("api-ms-win-",)
SKIP_NAMES = ("tcl86t.dll", "tk86t.dll")

binaries = []
if CONDA_BIN.is_dir():
    for p in sorted(CONDA_BIN.glob("*.dll")):
        n = p.name.lower()
        if n.startswith(SKIP_PREFIX) or n in SKIP_NAMES:
            continue
        binaries.append((str(p), "."))
    if not any("libssl" in b[0] for b in binaries):
        raise SystemExit(
            f"{CONDA_BIN} 里没找到 libssl，打出来的 exe 会在 uvicorn 导入 ssl 时崩。"
            f"确认 openssl 还在这个环境里。")

if not (UI_DIST / "index.html").is_file():
    raise SystemExit(
        f"找不到 {UI_DIST / 'index.html'}。先在 ui/ 下跑 npm run build，"
        f"否则打出来的 exe 能启动但界面全白。")

datas = [
    # (源, 包内目标目录)。目标路径要与 engine/main.py 里 UI_DIST 的推导一致
    (str(UI_DIST), "ui/dist"),
    (str(ROOT / "lostpath" / "scan" / "export_inventory.ps1"), "lostpath/scan"),
]

# uvicorn 的协议/生命周期实现是运行期按名字动态导入的，静态分析看不到
hiddenimports = (
    collect_submodules("uvicorn")
    + ["encodings.idna"]              # 某些 Windows 环境下 socket 解析要用
)

a = Analysis(
    [str(ROOT / "engine" / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 明确排掉打包/测试期依赖，别让它们进产物
    excludes=["pytest", "PyInstaller", "PIL", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="lostpath-engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                        # UPX 常被杀软误报，收益不值这个代价
    console=False,
    icon=str(ROOT / "ico" / "LostPath.ico"),
)
