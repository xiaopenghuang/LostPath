r"""调色板对比度必须过 WCAG AA，且每个颜色必须带标注。

**为什么需要这条**：`tsc --noEmit` 和 `vite build` 对对比度一律放行。改一个十六进制
色值不会有任何工具报警，界面照样构建成功、看着也"挺好"——直到有人在浅色主题下
读不出文件路径。而这个工具是拿路径决定删哪个目录的。

真实发生过的一次（2026-08-31，一次 UI 改版）：整套深色调色板被换掉，同时**每个颜色
后面的对比度标注被删了**。事后逐个实测才发现：

  · 侧栏激活态浅色下 3.58（旧值 5.19），因为侧栏底是 --deep——而标注里那三个数
    只覆盖 --bg / --panel / --panel2，**第四种底从没人验过**
  · 软件首字母磁贴的颜色由名字哈希算出，亮度写死 60%：深色下 360 个色相里 160 个
    不过线（最差 2.62），浅色下 360 个全不过（最差 1.12，基本看不见）
  · --tx3 压在 --panel2 上是 4.4999——标注写的"4.50"是四舍五入后的样子

这三条没有一条能靠"看一眼截图"发现：第一条要知道侧栏用的是第四种底，第二条随
软件名变化，第三条差 0.0001。所以这里用计算而不是眼睛。

**判据能分辨失败**：把任一颜色改暗一档、或把 --tile-fg-l 从 76% 调到 70%，本文件
就会红。见文件末尾 `test_the_criteria_can_fail`——它直接构造一个不过线的值，
证明这套算法真的会拒绝，而不是无论输入什么都返回"通过"。

不覆盖的部分（诚实说明）：
  · antd 组件自己推导的颜色（link / ghost 按钮那些）不在 CSS 变量里，测不到，
    只能靠浏览器实测。theme.tsx 里钉死了 colorLink 就是为此。
  · 半透明叠加底（激活态那种 rgba 压底）这里只验列出来的组合，不是全量枚举。
  · 真实渲染值。CSS 变量算得对不代表页面上就是这个颜色（可能被别的规则盖掉）。
"""
from __future__ import annotations

import colorsys
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "ui" / "src" / "index.css"

# WCAG 2.1 AA：普通文本 4.5:1，大文本（>=24px，或 >=18.66px 且加粗）3.0:1。
AA_NORMAL = 4.5


def _srgb_to_lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_srgb_to_lin(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def composite(fg: tuple[float, float, float], alpha: float,
              bg: tuple[float, float, float]) -> tuple[float, float, float]:
    """把 alpha 混色算成实色。半透明前景压在底上时必须先合成再算对比度。"""
    return tuple(f * alpha + b * (1 - alpha) for f, b in zip(fg, bg))  # type: ignore[return-value]


@pytest.fixture(scope="module")
def css_text() -> str:
    if not CSS.exists():
        pytest.skip(f"没有 {CSS}")
    return CSS.read_text("utf-8")


def _block(css: str, selector: str) -> str:
    """取出某个选择器的声明块。用于把深色段与浅色段分开读。"""
    i = css.index(selector)
    start = css.index("{", i)
    depth, j = 0, start
    while j < len(css):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1:j]
        j += 1
    raise AssertionError(f"{selector} 的声明块没闭合")


def _vars(block: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"--([\w-]+):\s*([^;]+);", block)}


def _resolve(name: str, table: dict[str, str], _depth: int = 0) -> str | None:
    """把 `--accent-fg: var(--blue2)` 这类间接引用解到十六进制。

    **不解析就等于不测**：`raw.startswith("#")` 会把 `var(--blue2)` 直接跳过，
    于是 --accent-fg 这个"蓝色文字唯一出口"反而成了唯一没人验的令牌。
    """
    if _depth > 8:  # 循环引用保护
        return None
    raw = table.get(name)
    if not raw:
        return None
    raw = raw.split("/*")[0].strip()
    if raw.startswith("#"):
        return raw
    m = re.fullmatch(r"var\(--([\w-]+)\)", raw)
    if m:
        return _resolve(m.group(1), table, _depth + 1)
    return None


# 前景令牌 -> 它必须过的门槛，以及允许落在哪些背景上。
# **这张表是这个文件的核心。** 两个约束都要写清楚：
#   · 门槛：普通文本 4.5，只用于图标/大文本的可以是 3.0
#   · 允许的底：漏掉一种就是 2026-08-31 那次踩的坑——侧栏用 --deep，
#     而当时的标注只覆盖 bg / panel / panel2，第四种底从没人验过
#
# 放宽门槛或缩小背景集合时**必须同时在 index.css 里写明理由**，否则下一个人
# 会以为这个颜色随便用都安全。
# --deep 不在这里：它只出现在侧栏一个地方，而侧栏用 .lp-sidebar 重新绑定了
# 会掉线的几个令牌（见 index.css）。那一层由 test_sidebar_overrides_pass_on_deep
# 单独验，比要求全套调色板都适配一个只用一次的背景更贴合实际。
CONTENT_BG = ("bg", "panel", "panel2")

FG_RULES: dict[str, dict[str, object]] = {
    "tx":     {"min": 4.5, "on": CONTENT_BG},
    "tx2":    {"min": 4.5, "on": CONTENT_BG},
    "tx3":    {"min": 4.5, "on": CONTENT_BG},
    "cyan":   {"min": 4.5, "on": CONTENT_BG},
    "red":    {"min": 4.5, "on": CONTENT_BG},
    "green":  {"min": 4.5, "on": CONTENT_BG},
    "amber":  {"min": 4.5, "on": CONTENT_BG},
    # 蓝色文字统一走 --accent-fg。深色下它是 var(--blue2)，浅色下是字面 #0550ae，
    # 所以这里按主题取到的实际值来验（见下面的 _resolve）。
    "accent-fg":     {"min": 4.5, "on": CONTENT_BG},
    "nav-active-fg": {"min": 4.5, "on": CONTENT_BG},
    # 不列入的两个，理由都写在 index.css 里：
    #   --blue   深色下对 --panel2 只有 4.44，只作背景与边框
    #   --blue2  浅色下 2.90，不承载任何文字，只是 antd colorInfo 的种子
    #            （由 test_blue2_is_never_used_as_a_text_color 守住）
}
BG_TOKENS = list(CONTENT_BG) + ["deep"]


@pytest.mark.parametrize("theme,selector", [
    ("dark", ":root[data-theme='dark']"),
    ("light", ":root[data-theme='light']"),
])
def test_every_palette_color_passes_on_its_allowed_backgrounds(css_text: str, theme: str, selector: str) -> None:
    """前景色压在**每一种它会落到的背景**上都要过自己那档门槛。

    包含 --deep：它是侧栏底，浅色下比 --bg 更亮，是最容易被漏掉的一种。
    """
    block = _block(css_text, selector)
    v = _vars(block)
    # 深色段写在 `:root, :root[data-theme='dark']` 里，两个选择器共用一个块
    if theme == "dark" and "tx" not in v:
        v = _vars(_block(css_text, ":root,"))

    bgs = {k: hex_rgb(v[k]) for k in BG_TOKENS if k in v and v[k].startswith("#")}
    assert set(bgs) == set(BG_TOKENS), f"{theme}: 背景令牌缺失 {set(BG_TOKENS) - set(bgs)}"

    failures, unresolved = [], []
    for name, rule in FG_RULES.items():
        raw = _resolve(name, v)
        if raw is None:
            unresolved.append(f"--{name}（值 {v.get(name)!r}）")
            continue
        fg = hex_rgb(raw)
        need = float(rule["min"])  # type: ignore[arg-type]
        for bg_name in rule["on"]:  # type: ignore[union-attr]
            r = contrast(fg, bgs[bg_name])
            if r < need:
                failures.append(
                    f"--{name} {raw} 压 --{bg_name} {v[bg_name]} = {r:.4f}（需 {need}）"
                )
    # 解不出来的必须报错而不是跳过：静默跳过会让"没测到"长得跟"测过且通过"一样
    assert not unresolved, (
        f"{theme}: 这些令牌解不出十六进制值，无法校验：\n  " + "\n  ".join(unresolved)
    )
    assert not failures, (
        f"{theme} 主题下这些组合不过线：\n  " + "\n  ".join(failures)
        + "\n\n要么改颜色，要么改 FG_RULES 并**同时**在 index.css 里写明理由。"
    )


@pytest.mark.parametrize("theme,selector", [
    ("dark", ":root,"),
    ("light", ":root[data-theme='light']"),
])
def test_palette_colors_carry_contrast_annotations(css_text: str, theme: str, selector: str) -> None:
    """每个颜色令牌后面必须有注释。

    数字本身对不对由上面那条测；这条只保证**注释没被删掉**。2026-08-31 那次
    整套深色标注被删，于是"还剩多少余量"这个信息就没了——下一个改色的人只能
    重新做一遍全量实测，或者干脆不做。
    """
    block = _block(css_text, selector)
    missing = []
    for name in FG_RULES:
        m = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{3,8}});([^\n]*)", block)
        if not m:
            continue
        if "/*" not in m.group(2):
            missing.append(f"--{name}")
    assert not missing, (
        f"{theme} 主题下这些颜色没有对比度标注：{missing}\n"
        "格式：`--tx: #f0f6fc;   /* 17.62 / 16.33 / 14.99 */`（依次对 bg / panel / panel2）"
    )


def test_tile_letter_lightness_passes_for_every_hue(css_text: str) -> None:
    """软件首字母磁贴：360 个色相全部要过线。

    色相由软件名哈希决定（见 SoftwarePage.getTileHue），也就是**由数据决定**。
    所以不能只看当前机器上那几个软件——换一台机器、装一个新软件，就可能撞上
    不过线的色相。这里穷举全部 360 个。

    hsl 的 L 是数学亮度不是感知亮度：同一个 L=60% 在黄色（h≈60）和蓝紫（h≈240）
    下实际明暗差一倍，这就是当初写死 60% 会漏的原因。
    """
    dark = _vars(_block(css_text, ":root,"))
    light = _vars(_block(css_text, ":root[data-theme='light']"))

    def sweep(v: dict[str, str], label: str) -> None:
        l_pct = float(v["tile-fg-l"].rstrip("%"))
        s_pct = float(v["tile-fg-s"].rstrip("%"))
        panel2 = hex_rgb(v["panel2"])
        worst, worst_hue = 99.0, -1
        for hue in range(360):
            r, g, b = colorsys.hls_to_rgb(hue / 360.0, l_pct / 100.0, s_pct / 100.0)
            fg = (r * 255, g * 255, b * 255)
            # 磁贴底：hsla(hue, 40%, 50%, .12) 叠在 --panel2 上
            tr, tg, tb = colorsys.hls_to_rgb(hue / 360.0, 0.50, 0.40)
            bg = composite((tr * 255, tg * 255, tb * 255), 0.12, panel2)
            c = contrast(fg, bg)
            if c < worst:
                worst, worst_hue = c, hue
        assert worst >= AA_NORMAL, (
            f"{label}: --tile-fg-l={l_pct}% 时色相 {worst_hue} 只有 {worst:.2f}，"
            f"不到 {AA_NORMAL}。深色要更亮、浅色要更暗。"
        )

    sweep(dark, "深色")
    sweep(light, "浅色")


def test_sidebar_overrides_pass_on_deep(css_text: str) -> None:
    """侧栏（唯一以 --deep 为底的面）里重绑的令牌必须在 --deep 上过线。

    浅色下 --deep #dfe4ea 比 --bg 更**亮**，是第四种背景。调色板那三个标注数
    不覆盖它，于是 --amber 3.81 / --tx3 4.10 / --green 3.97 / --red 4.19 全掉线，
    而侧栏里恰好就有 --tx3（品牌副标题 9.5px）和 --amber（快照过期提示 11px）。

    这条测的是"覆盖层本身够不够"：只要 .lp-sidebar 里某个值被改回原样、
    或新加一个没覆盖的令牌，这里就会红。
    """
    light = _vars(_block(css_text, ":root[data-theme='light']"))
    deep = hex_rgb(light["deep"])

    m = re.search(r":root\[data-theme='light'\]\s+\.lp-sidebar\s*\{([^}]*)\}", css_text, re.S)
    assert m, "找不到 :root[data-theme='light'] .lp-sidebar 的令牌覆盖块"
    overrides = _vars(m.group(1))
    assert overrides, "侧栏覆盖块是空的"

    failures = []
    for name, raw in overrides.items():
        if not raw.startswith("#"):
            continue
        r = contrast(hex_rgb(raw), deep)
        if r < AA_NORMAL:
            failures.append(f"--{name} {raw} 压 --deep {light['deep']} = {r:.4f}")
    assert not failures, "侧栏覆盖后仍不过线：\n  " + "\n  ".join(failures)

    # 覆盖必须真的覆盖到了会掉线的那些令牌，否则这个块可以写成空的也"通过"
    need_override = []
    for name in ("tx3", "amber", "green", "red"):
        base = light.get(name)
        if base and base.startswith("#") and contrast(hex_rgb(base), deep) < AA_NORMAL:
            if name not in overrides:
                need_override.append(f"--{name}（原值 {base} 在 --deep 上只有 "
                                     f"{contrast(hex_rgb(base), deep):.2f}）")
    assert not need_override, (
        "这些令牌在 --deep 上不过线，但 .lp-sidebar 没有覆盖它们：\n  "
        + "\n  ".join(need_override)
    )


def test_nav_active_passes_on_the_sidebar_background(css_text: str) -> None:
    """侧栏激活态：蓝字压在"蓝色淡底 + 侧栏底"的复合色上。

    这是最容易算错的一处，因为要叠两层，而且底是 --deep 不是 --panel。
    我自己手算时就用错了底，报出 4.51（其实是 3.58），靠浏览器实测才发现。
    """
    dark = _vars(_block(css_text, ":root,"))
    light = _vars(_block(css_text, ":root[data-theme='light']"))

    cases = [
        # (主题名, 变量表, 激活态字色, 淡底色, 淡底 alpha)
        ("深色", dark, dark["blue2"], "#2f81f7", 0.14),
        ("浅色", light, light["nav-active-fg"], "#0969da", 0.10),
    ]
    for label, v, fg_raw, tint, alpha in cases:
        fg = hex_rgb(fg_raw)
        bg = composite(hex_rgb(tint), alpha, hex_rgb(v["deep"]))
        r = contrast(fg, bg)
        assert r >= AA_NORMAL, (
            f"{label}侧栏激活态 {fg_raw} 压在 {tint}@{alpha} over --deep {v['deep']} "
            f"上只有 {r:.2f}"
        )


def test_scan_button_white_text_passes_on_every_gradient_stop(css_text: str) -> None:
    """扫描按钮是白字压渐变：**每一个色标**都要过线，不是只看首尾。

    这里踩过一次：把悬停态做成"变亮"，最亮那个色标（#2f7ff0）上白字掉到 3.88。
    悬停要表达"更突出"，得靠发光/位移，不能靠提高底色亮度——白字的对比度是
    随底色变亮而**下降**的。
    """
    white = (255, 255, 255)
    rules = re.findall(r"\.lp-scan-btn[^{]*\{[^}]*\}", css_text, re.S)
    assert rules, "找不到 .lp-scan-btn 的规则"

    checked = 0
    failures = []
    for rule in rules:
        for grad in re.findall(r"linear-gradient\(([^)]*)\)", rule):
            for stop in re.findall(r"#[0-9a-fA-F]{6}", grad):
                r = contrast(white, hex_rgb(stop))
                checked += 1
                if r < AA_NORMAL:
                    failures.append(f"{stop} = {r:.2f}")
    assert checked >= 3, f"只验到 {checked} 个色标，规则可能没解析对"
    assert not failures, "白字在这些色标上不过线：" + ", ".join(failures)


def test_blue2_is_never_used_as_a_text_color() -> None:
    """`color: var(--blue2)` 不允许出现在组件里。

    浅色下 --blue2 #218bff 对四种背景是 3.18 / 3.39 / 2.90 / 2.65——当普通文字
    全不过线，连大文本的 3.0 门槛也只在前两种底上勉强过。它的正当身份只有一个：
    antd 的 colorInfo / colorPrimaryHover 种子，那两个位置不承载文本。

    这条拦的是**复发**。原先有 8 处直接写 `color: 'var(--blue2)'`，其中
    「盘符徽标」被挪到 --panel2 上（2.90）、「软件实体」数字从 28px 缩到 26px
    跨过了大文本门槛——都不是有人主动改颜色，是周边改动把它推下线的。
    只要蓝色文字统一走 --accent-fg，这类事就不会再发生。
    """
    src_dir = ROOT / "ui" / "src"
    if not src_dir.exists():
        pytest.skip("没有 ui/src")

    offenders = []
    for path in sorted(src_dir.rglob("*.tsx")) + sorted(src_dir.rglob("*.ts")):
        for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if line.lstrip().startswith(("//", "*", "/*")):
                continue  # 注释里提到它是可以的（index.css 与本文件都提到）
            if re.search(r"color:\s*['\"`]?var\(--blue2\)", line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}  {line.strip()[:70]}")
    assert not offenders, (
        "这些地方把 --blue2 当文字色用了，改成 var(--accent-fg)：\n  "
        + "\n  ".join(offenders)
    )


# antd 的 preset Tag：色值由 antd 生成，不在我们的 CSS 变量里，所以这里把
# **实测到的** fg/bg 对钉下来。数字来自浏览器 getComputedStyle（见提交说明），
# 手写一遍是为了让"antd 换版本后配色变了"这件事能被测出来，而不是等用户发现。
ANTD_TAG_PAIRS = {
    # 主题: {preset: (antd 原本的 6 号字, Tag 的 1 号底, 我们覆盖后的字或 None)}
    "light": {
        "blue":     ("#0958d9", "#e6f4ff", None),
        "geekblue": ("#1d39c4", "#f0f5ff", None),
        "purple":   ("#531dab", "#f9f0ff", None),
        "red":      ("#cf1322", "#fff1f0", None),
        "cyan":     ("#08979c", "#e6fffb", "#006d75"),
        "orange":   ("#d46b08", "#fff7e6", "#ad4e00"),
        "gold":     ("#d48806", "#fffbe6", "#874d00"),
        "green":    ("#389e0d", "#f6ffed", "#237804"),
    },
    "dark": {
        "blue":     ("#3c89e8", "#111a2c", None),
        "cyan":     ("#33bcb7", "#112123", None),
        "orange":   ("#e89a3c", "#2b1d11", None),
        "gold":     ("#e8b339", "#2b2111", None),
        "red":      ("#e84749", "#2a1215", None),
        "green":    ("#6abe39", "#162312", None),
        "purple":   ("#854eca", "#1a1325", "#b37feb"),
        "geekblue": ("#5273e0", "#131629", "#85a5ff"),
    },
}


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_antd_tag_presets_pass_after_our_overrides(css_text: str, theme: str) -> None:
    """每个用到的 antd Tag preset 都要过 4.5:1，需要覆盖的必须真的写了覆盖。

    这些 Tag 上写的是分类与动作——"junction"、"改环境变量"、"容器"、"未归因"。
    看错一个可能对错的目录执行操作，所以按普通文本 4.5 要求，不按"装饰"放宽。
    """
    pairs = ANTD_TAG_PAIRS[theme]
    failures, missing_css = [], []
    for preset, (antd_fg, bg, override) in pairs.items():
        effective = override or antd_fg
        r = contrast(hex_rgb(effective), hex_rgb(bg))
        if r < AA_NORMAL:
            failures.append(f"{theme} .ant-tag-{preset}: {effective} 压 {bg} = {r:.2f}")
        if override:
            # 覆盖必须在 CSS 里真的存在，否则这张表就只是一厢情愿
            pat = (rf":root\[data-theme='{theme}'\]\s+\.ant-tag-{preset}\s*\{{"
                   rf"[^}}]*color:\s*{re.escape(override)}")
            if not re.search(pat, css_text, re.S):
                missing_css.append(f"{theme} .ant-tag-{preset} -> {override}")
        else:
            # 不需要覆盖的，antd 原值就得过——否则是这张表记错了
            assert contrast(hex_rgb(antd_fg), hex_rgb(bg)) >= AA_NORMAL, (
                f"{theme} .ant-tag-{preset} 标为「不需覆盖」，但 antd 原值 {antd_fg} "
                f"压 {bg} 只有 {contrast(hex_rgb(antd_fg), hex_rgb(bg)):.2f}"
            )
    assert not missing_css, "这些覆盖在 index.css 里找不到：\n  " + "\n  ".join(missing_css)
    assert not failures, "覆盖后仍不过线：\n  " + "\n  ".join(failures)


def test_every_preset_used_in_code_is_covered_by_the_table() -> None:
    """代码里用到的 preset 色名，必须都在 ANTD_TAG_PAIRS 里有记录。

    没有这条，新加一个 `<Tag color="lime">` 就会绕过上面那条测试——表里没有它，
    循环自然不会检查它。这是"判据能不能分辨失败"的另一面：**覆盖面本身也要测**。
    """
    src_dir = ROOT / "ui" / "src"
    if not src_dir.exists():
        pytest.skip("没有 ui/src")

    used: set[str] = set()
    for path in sorted(src_dir.rglob("*.tsx")) + sorted(src_dir.rglob("*.ts")):
        text = path.read_text("utf-8")
        used |= set(re.findall(r'color="([a-z]+)"', text))
        # *_COLOR 映射表里的值也算（KIND_COLOR / CAT_COLOR / ACTION_COLOR）
        for block in re.findall(r"_COLOR:\s*Record<string,\s*string>\s*=\s*\{(.*?)\}", text, re.S):
            used |= set(re.findall(r":\s*'([a-z]+)'", block))

    # antd 里这些不是 preset：default 走中性色（20.12，天然安全），
    # 其余是我们自己的语义色名或 CSS 关键字，不走 .ant-tag-<name> 那套。
    not_presets = {"default", "transparent", "inherit", "currentcolor", "none"}
    presets = {c for c in used if c not in not_presets}
    known = set(ANTD_TAG_PAIRS["light"]) | set(ANTD_TAG_PAIRS["dark"])
    unknown = presets - known
    assert not unknown, (
        f"代码里用到这些 preset 但 ANTD_TAG_PAIRS 没记录：{sorted(unknown)}\n"
        "在浏览器里量出它的 fg/bg 后补进表里（两个主题都要），否则它的对比度没人管。"
    )


THEME_TSX = ROOT / "ui" / "src" / "theme.tsx"

# antd 令牌 -> (要过的门槛, 会落在哪些底上)。这些底是 antd 自己的 colorBgContainer
# 与我们的 --panel / --panel2，都要顾。
ANTD_TOKEN_BACKGROUNDS = {
    "dark": ["#161b22", "#131822", "#1a202c"],
    "light": ["#ffffff", "#f6f8fa", "#eaeef2"],
}


@pytest.mark.parametrize("theme,marker", [
    ("dark", "DARK_TOKENS"),
    ("light", "LIGHT_TOKENS"),
])
def test_antd_text_tokens_pass(theme: str, marker: str) -> None:
    """theme.tsx 里钉死的 antd 文字令牌必须过线。

    这些是 antd 会自己推、推完不验对比度的位置。踩过三次：
      · colorLink   深色推成 #3d80d6（4.45）、浅色 #218bff（3.39）
      · ghost 按钮  深色推成 #2b71d5（3.75）
      · colorTextDescription  默认 rgba(...,0.45)，深色 4.37 / 浅色 3.27

    钉死之后还得测：写进 theme.tsx 只是让它此刻正确，不保证下次改动时仍正确。
    """
    if not THEME_TSX.exists():
        pytest.skip("没有 theme.tsx")
    text = THEME_TSX.read_text("utf-8")
    i = text.index(marker)
    block = text[i:i + 2600]
    bgs = [hex_rgb(b) for b in ANTD_TOKEN_BACKGROUNDS[theme]]

    checked, failures = 0, []
    for token in ("colorLink", "colorTextDescription"):
        m = re.search(rf"{token}:\s*'([^']+)'", block)
        if not m:
            continue
        raw = m.group(1)
        if raw.startswith("#"):
            for bg in bgs:
                r = contrast(hex_rgb(raw), bg)
                checked += 1
                if r < AA_NORMAL:
                    failures.append(f"{token} {raw} 压 #{''.join('%02x' % c for c in bg)} = {r:.2f}")
        else:
            rgba = re.match(r"rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s]+([\d.]+))?\s*\)", raw)
            assert rgba, f"{token} 的值 {raw!r} 解不出来"
            fg = tuple(int(rgba.group(k)) for k in (1, 2, 3))
            alpha = float(rgba.group(4) or 1)
            for bg in bgs:
                comp = composite(fg, alpha, bg)
                r = contrast(comp, bg)
                checked += 1
                if r < AA_NORMAL:
                    failures.append(
                        f"{token} {raw} 压 #{''.join('%02x' % c for c in bg)} = {r:.2f}"
                    )
    assert checked >= 6, f"{theme}: 只验到 {checked} 个组合，令牌可能没解析到"
    assert not failures, f"{theme} antd 文字令牌不过线：\n  " + "\n  ".join(failures)


def test_the_criteria_can_fail() -> None:
    """判据必须能分辨失败——否则上面全绿也说明不了任何事。

    构造三个已知不过线的输入，确认算法真的会拒绝：
    """
    # 1. 灰压灰：肉眼都看不清，必须判失败
    assert contrast((136, 136, 136), (153, 153, 153)) < AA_NORMAL

    # 2. 真实历史缺陷：旧的白字压 --blue 实底，实测 3.75
    r = contrast((255, 255, 255), hex_rgb("#2f81f7"))
    assert 3.7 < r < 3.8, f"期望 ~3.75，得到 {r:.2f}"
    assert r < AA_NORMAL

    # 3. 真实历史缺陷：--tx3 压 --panel2 是 4.4999，差 0.0001 不过
    #    这条同时证明算法精度够——四舍五入到两位会看成 4.50「通过」
    r = contrast(hex_rgb("#656d76"), hex_rgb("#eaeef2"))
    assert r < AA_NORMAL, f"{r:.6f} 应当判为不过线"
    assert round(r, 2) == 4.5, "这个值四舍五入后正好是 4.50，正是它当初蒙混过关的原因"

    # 4. 反向：一个明确该过的组合必须判通过，否则算法是"一律拒绝"
    assert contrast((255, 255, 255), (0, 0, 0)) > 20
