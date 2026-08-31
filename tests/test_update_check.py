r"""检查新版本这条路的结构性约束（`desktop/main.js`）。

**为什么用结构性检查而不是跑起来看**：跟 `test_no_console_window.py` 同一个道理。
真跑一遍要起 Electron、连 GitHub、还要人去点对话框——CI 上做不到，开发机上做一次
也只覆盖"当时那个版本号组合"。而这里真正怕的两件事都是**代码形状**问题：

  ① 把 API 响应里的 `html_url` 交给 `shell.openExternal`
     那个字段来自网络。DNS 被劫持、响应被篡改、或哪天 GitHub 改了字段语义，
     它就能变成任意 URL（含 `file://`）。发布页地址本来是固定的，没有任何理由
     从网络取。这条一旦被"顺手优化"回去，靠跑测试是发现不了的——正常网络下
     `html_url` 就是对的，测起来永远绿。

  ② 版本比较退化成字符串比大小
     `"0.10.0" > "0.9.0"` 为 false，于是 0.10.0 的用户被反复推送 0.9.0。
     只在跨十位时才暴露，用 0.1.0/0.2.0 测永远绿。

所以判据钉在源码形状上：URL 必须是字面常量、比较必须逐段取数。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

MAIN_JS = Path(__file__).resolve().parents[1] / "desktop" / "main.js"


def _function_body(src: str, name: str) -> str:
    r"""按花括号配平取出一个函数的源码。

    别用"截到下一个 `\nfunction `"那种写法：`maybeNotifyUpdate` 是
    `async function`，那个模式匹配不到，截取范围会溢进下一个函数，
    于是"fetchLatestRelease 里不该有 showMessageBox"这条会误报。
    """
    m = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\b", src)
    assert m, f"找不到 {name}——被改名或删了，这条测试失去意义"
    depth, began = 0, False
    for i in range(m.start(), len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
            began = True
        elif ch == "}":
            depth -= 1
            if began and depth == 0:
                return src[m.start():i + 1]
    raise AssertionError(f"{name} 的花括号不配平")


@pytest.fixture(scope="module")
def src() -> str:
    if not MAIN_JS.exists():
        pytest.skip(f"没有 {MAIN_JS}")
    return MAIN_JS.read_text("utf-8")


def test_release_url_is_a_literal_constant(src: str):
    """打开的地址必须是写死的 https://github.com/ 字面量。"""
    m = re.search(r"const RELEASES_URL = '([^']+)';", src)
    assert m, "找不到 RELEASES_URL 常量声明——它被改成变量或删了"
    url = m.group(1)
    assert url.startswith("https://github.com/"), \
        f"RELEASES_URL 必须指向 github.com 且用 https，实际 {url!r}"
    # 不能含插值——模板字符串或拼接都意味着有值能从外部进来
    assert "${" not in url and "+" not in url, \
        f"RELEASES_URL 不能含插值或拼接：{url!r}"


def test_open_external_never_takes_a_network_derived_url(src: str):
    """`shell.openExternal` 的实参不能来自网络响应。

    允许的形态只有两种：常量标识符（RELEASES_URL）、或 setWindowOpenHandler 里
    那个 `url`（那是用户在页面上点的链接，Electron 给的，不是我们从网络取的）。
    """
    calls = re.findall(r"shell\.openExternal\(\s*([^)]*?)\s*\)", src)
    assert calls, "一个 shell.openExternal 都没有——函数被改名了？这条测试失去意义"

    allowed = {"RELEASES_URL", "url"}
    for arg in calls:
        assert arg in allowed, (
            f"shell.openExternal({arg}) 的实参不在白名单里。"
            f"若它来自 GitHub 响应（html_url / assets[].browser_download_url 等），"
            f"等于让远端决定我们打开什么。允许的只有 {sorted(allowed)}"
        )

    # 更直接的一条：响应字段名不该出现在 openExternal 附近
    for field in ("html_url", "browser_download_url", "upload_url", "assets_url"):
        assert f"openExternal({field}" not in src.replace(" ", ""), \
            f"shell.openExternal 拿了网络字段 {field}"


def test_version_compare_is_numeric_not_lexicographic(src: str):
    """版本比较必须逐段取数字，不能按字符串比大小。"""
    body = _function_body(src, "compareVersions")

    assert "Number(" in body, \
        "compareVersions 里没有 Number() —— 没做数值转换就是在比字符串"
    # 必须按 . 拆出三段
    assert re.search(r"\\d\+\)\\\.\(\\d\+\)", body) or re.search(r"\(\\d\+\)", body), \
        "compareVersions 里找不到逐段匹配数字的正则"
    # 反面：不该出现直接比较两个原始入参的写法
    assert not re.search(r"\breturn\s+a\s*[<>]\s*b\b", body), \
        "compareVersions 直接对入参做 < / > 比较 —— 那是字符串比较"


def test_update_check_failures_are_silent(src: str):
    """查更新失败不能弹东西给用户。

    失败是常态：断网、公司代理、被墙、匿名频率限制（60 次/小时/IP）。
    用户打开这个软件是为了清 C 盘，不是为了知道我们连不上 GitHub。
    """
    body = _function_body(src, "fetchLatestRelease")

    assert "showMessageBox" not in body and "showErrorBox" not in body, \
        "fetchLatestRelease 里出现了对话框调用——失败路径不该打扰用户"
    # 每条失败路径都该 resolve(null)，而不是 reject
    assert "reject" not in body, \
        "fetchLatestRelease 不该 reject——调用方按 null 处理失败，抛出去会漏到上层"


def test_engine_stays_offline():
    """引擎侧不能出网。

    这是"检查更新放壳层"这个决定的另一半：壳层出网可以，引擎不行。
    一个读注册表、扫全盘的本地服务不出网，用户才好放心；而这条性质
    只要有人在引擎里加一次 requests 调用就没了。
    """
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for py in list((root / "lostpath").rglob("*.py")) + list((root / "engine").rglob("*.py")):
        text = py.read_text("utf-8", errors="replace")
        for mod in ("requests", "httpx", "aiohttp", "urllib.request", "urllib3"):
            # 只看 import，不看注释里提到的名字
            if re.search(rf"^\s*(?:import|from)\s+{re.escape(mod)}\b", text, re.M):
                offenders.append(f"{py.relative_to(root)} 引入了 {mod}")
    assert not offenders, (
        "引擎侧出现了 HTTP 客户端库，破坏了「引擎不出网」这条性质：\n  "
        + "\n  ".join(offenders)
    )
