r"""各处版本号必须一致。

**为什么需要这条**：版本号散在四个文件里（两个 package.json + 两个 package-lock.json），
外加 `lostpath/__version__`。而 `__version__` **没有任何代码读它** —— 于是它可以
静静地停在旧值上，谁也不会发现：

  · 发布时只改 desktop/package.json（那是 app.getVersion() 与安装包文件名的来源），
    __version__ 就此落后一个版本
  · 之后有人拿 __version__ 去做诊断输出或 bug 报告模板，报的就是错的版本
  · 而"检查新版本"拿 app.getVersion() 与远端 tag 比，两个来源不一致时排查会绕远路

一个无人读取的常量不会自己出错，但它一定会漂移。钉住它的成本是这几行，比日后
对着两个不同的版本号排查便宜得多。

不把 __version__ 做成唯一来源：那要让 Node 侧在构建时读 Python 文件，跨语言取值
比多一条断言复杂得多，收益只是少写一个数字。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# desktop/package.json 是权威来源：app.getVersion() 读它，electron-builder 也用它
# 决定安装包文件名（LostPath.Setup.<版本>.exe）。
AUTHORITY = ROOT / "desktop" / "package.json"


def _pkg_version(p: Path) -> str:
    return json.loads(p.read_text("utf-8"))["version"]


@pytest.fixture(scope="module")
def expected() -> str:
    if not AUTHORITY.exists():
        pytest.skip(f"没有 {AUTHORITY}")
    return _pkg_version(AUTHORITY)


def test_version_is_semver(expected: str):
    """必须是三段语义版本。

    检查新版本那条路按 `\\d+\\.\\d+\\.\\d+` 解析（见 desktop/main.js 的
    compareVersions），解析不出来就会被判成"没有更新"—— 静默失效，用户永远收不到
    升级提示而没有任何报错。
    """
    assert re.fullmatch(r"\d+\.\d+\.\d+", expected), \
        f"desktop/package.json 的版本 {expected!r} 不是 x.y.z，会让更新检查静默失效"


@pytest.mark.parametrize("rel", [
    "ui/package.json",
    "desktop/package-lock.json",
    "ui/package-lock.json",
])
def test_node_manifests_agree(rel: str, expected: str):
    """其余 Node 清单与权威来源一致。

    锁文件不一致时 `npm ci` 直接拒绝安装（EUSAGE: "can only install packages when
    your package.json and package-lock.json are in sync"），CI 的 ui job 会红。
    """
    p = ROOT / rel
    if not p.exists():
        pytest.skip(f"没有 {rel}")
    d = json.loads(p.read_text("utf-8"))
    assert d.get("version") == expected, \
        f"{rel} 顶层版本 {d.get('version')!r} != {expected!r}"
    # 锁文件里 packages[""] 是本包自己那一条，也得跟上
    root_pkg = d.get("packages", {}).get("")
    if isinstance(root_pkg, dict) and "version" in root_pkg:
        assert root_pkg["version"] == expected, \
            f'{rel} 的 packages[""].version {root_pkg["version"]!r} != {expected!r}'


def test_python_package_version_agrees(expected: str):
    r"""`lostpath.__version__` 与权威来源一致。

    它目前没有调用方，所以错了不会有任何症状 —— 这正是要钉住它的理由。
    """
    init = ROOT / "lostpath" / "__init__.py"
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init.read_text("utf-8"), re.M)
    assert m, "lostpath/__init__.py 里找不到 __version__"
    assert m.group(1) == expected, (
        f"lostpath.__version__ = {m.group(1)!r}，而 desktop/package.json 是 {expected!r}。"
        f"发布时改了后者忘了这里 —— 它没有调用方，所以不会以别的方式暴露。"
    )
