r"""名称定性的整词匹配。

原先只有"分隔符正则"，要求缓存词前后是 \ / _ - . 之一，于是驼峰与空格分隔的目录名
整片漏掉：FontCache、CachedData、Installer Cache 实测都判成"未定性"。

放宽必须整词比对，**不能**退化成子串匹配：`Templates` 里含 "Temp"，子串匹配会把
Office 模板（真用户数据）判成临时文件并提议删除。这类误判的代价是不可逆的数据丢失，
所以本文件把每个易混词都钉住。
"""
import pytest

from lostpath.attribute import lostpath_kb as KB


# --------------------------------------------- 原先漏掉、现在应命中
@pytest.mark.parametrize("name", [
    "FontCache",            # 驼峰，cache 前无分隔符
    "CachedData",           # cached 变形
    "Installer Cache",      # 空格分隔
    "GPUCache",
    "ShaderCache",
    "CrashDumps",
])
def test_camel_and_space_names_are_cache(name):
    cat, why = KB.classify_name(name)
    assert cat == "可再生缓存", f"{name} 判成了 {cat}（{why}）"


# --------------------------------------------- 绝不能误判成缓存
@pytest.mark.parametrize("name", [
    "Templates",       # 含 Temp。Office 模板是用户数据，删了不可恢复
    "Template",
    "Tempo",
    "Attempts",
    "Contemporary",
    "Catalogue",       # 含 log
    "Dialogue",
    "Prologue",
    "Blogs",           # 含 log 但是完整词 blogs，不是 log
])
def test_lookalike_words_are_not_cache(name):
    cat, why = KB.classify_name(name)
    assert cat != "可再生缓存", (
        f"{name} 被误判为可再生缓存（{why}）——子串匹配的典型误伤，会导致删错数据")


def test_templates_is_user_data():
    """Templates 不只是"不算缓存"，它本身就是用户数据，应明确标出来。"""
    cat, _ = KB.classify_name("Templates")
    assert cat == "用户数据"


# --------------------------------------------- 缓存优先于用户数据
def test_cached_data_prefers_cache():
    r"""CachedData 切出 cached + data 两词都命中，必须判缓存。

    它是 VS Code 的编译产物缓存（`Roaming\Code\CachedData`），不是用户资料。
    """
    cat, why = KB.classify_name("CachedData")
    assert cat == "可再生缓存", why


# --------------------------------------------- 分词本身
@pytest.mark.parametrize("name,expect", [
    ("FontCache", ["font", "cache"]),
    ("Installer Cache", ["installer", "cache"]),
    ("Templates", ["templates"]),
    ("browser-cache", ["browser", "cache"]),
    ("blob_storage", ["blob", "storage"]),
    ("GPUCache", ["gpucache"]),          # 全大写接大写，不切
])
def test_tokens(name, expect):
    assert KB.tokens(name) == expect


# --------------------------------------------- 高风险优先级
def test_per_user_package_cache_is_high_risk():
    r"""按用户安装的 Package Cache 必须判"不可动"。

    两处 Package Cache：ProgramData（全机）与 AppData\Local（按用户）。原先只登记了
    前者，后者名字带空格恰好躲过分隔符正则、显示"未定性"——两个缺陷互相掩盖。整词
    匹配上线后它会被判成可再生缓存并提议删除，而删掉它同样让 VS/VC++ 修复与卸载失败。
    """
    p = r"C:\Users\someone\AppData\Local\Package Cache"
    assert KB.high_risk(p), "按用户的 Package Cache 未登记为高风险"


def test_program_data_package_cache_is_high_risk():
    assert KB.high_risk(r"C:\ProgramData\Package Cache")


@pytest.mark.parametrize("p", [
    r"D:\Windows\Installer",
    r"E:\Windows\WinSxS",
])
def test_high_risk_not_hardcoded_to_c_drive(p):
    """系统不一定装在 C 盘，高风险目录的判定不该写死盘符。"""
    assert KB.high_risk(p), f"{p} 没被识别为高风险"


def test_high_risk_wins_over_name_classification():
    """定性顺序：high_risk 必须压过名称特征，否则"Package Cache"会按缓存处理。"""
    from lostpath.attribute import attribute_v4 as A
    cat, why = A.classify("Package Cache", r"C:\ProgramData\Package Cache")
    assert cat == "不可动", f"{cat} / {why}"


# --------------------------------------- 同名目录在不同 zone 下是不同的东西
# 实测案例：`^uv$` 一条规则通吃 Local 与 Roaming 两侧，而它们装的不是一类东西——
# Local\uv 是缓存（UV_CACHE_DIR 管），Roaming\uv 是 python\（受管解释器）与 tools\
# （已装的 CLI 工具），归 UV_PYTHON_INSTALL_DIR 与 UV_TOOL_DIR 管。通吃的后果是给
# 用户看的理由是错的：Roaming 那侧显示"可再生缓存 · 改 UV_CACHE_DIR 即可"，而设那个
# 变量对它毫无作用，删掉 tools\ 会让用户已装的命令消失。

def test_local_uv_is_cache_with_cache_dir_var():
    """Local\\uv 是缓存，UV_CACHE_DIR 管它——这条不能被 zone 规则带坏。"""
    r = KB.lookup_owner_toolchain("uv", r"C:\Users\someone\AppData\Local\uv")
    assert r["cat"] == "可再生缓存"
    assert r["redirect"] == "UV_CACHE_DIR"


def test_roaming_uv_is_not_a_cache():
    r"""Roaming\uv 不是缓存，也不由 UV_CACHE_DIR 决定。

    定性成"混合"而非"可再生缓存"，这样计划器会按 not_cleanable 拦下——宁可少腾这
    0.06 GiB，也不能拿一个错理由建议用户删东西。
    """
    r = KB.lookup_owner_toolchain("uv", r"C:\Users\someone\AppData\Roaming\uv")
    assert r["cat"] == "混合", f"判成了 {r['cat']}"
    assert r["redirect"] != "UV_CACHE_DIR", "UV_CACHE_DIR 管不到 Roaming\\uv"
    assert r["label"] == "uv (Python)", "归属没错过，别把它一起改掉"


def test_roaming_uv_reason_does_not_say_cache():
    r"""理由里不能出现"缓存"，而且必须验**引擎真的这么产出**。

    attribute_v4 把 role 硬拼成 "<label> 缓存/数据"，所以只改 cat 不够——cat 对了而
    界面上的理由还写着"缓存"，用户看到的仍是一个错解释。

    **这条测试第一版是假的**：它自己构造 role dict 再传给 classify()，于是根本没走到
    attribute_v4 里造 role 的那行。把硬拼改回去做变异，它照样全绿。所以改成跑真归因
    （读脱敏 fixtures，0.05s），让整条链路都在覆盖内。
    """
    r = KB.lookup_owner_toolchain("uv", r"C:\Users\someone\AppData\Roaming\uv")
    assert "缓存" not in (r.get("role") or ""), r.get("role")

    from conftest import (FIXTURE_PROGRAM_DATA, FIXTURE_USER_HOME,
                          load_fixture)
    from lostpath.attribute import attribute_footprint

    records, _ = attribute_footprint(
        load_fixture("scan_c.json"), load_fixture("inventory.json"),
        load_fixture("shortcuts.json"), user_home=FIXTURE_USER_HOME,
        program_data=FIXTURE_PROGRAM_DATA)
    hit = [x for x in records
           if x["path"].lower().endswith("roaming\\uv")]
    assert len(hit) == 1, f"fixtures 里应恰有一条 Roaming\\uv，实得 {len(hit)}"
    rec = hit[0]
    assert rec["cat"] == "混合", rec["cat"]
    assert "缓存" not in (rec["why"] or ""), rec["why"]
    assert "缓存" not in (rec["role"] or ""), rec["role"]
    assert rec["redirect"] == "UV_TOOL_DIR / UV_PYTHON_INSTALL_DIR", rec["redirect"]
    # 归属从来没错过，别把它一起改坏
    assert rec["owner"] == "uv (Python)"


def test_local_uv_reason_still_says_cache():
    """反向守门：Local\\uv 确实是缓存，理由该保留"缓存"字样，别被 zone 规则带坏。"""
    from conftest import (FIXTURE_PROGRAM_DATA, FIXTURE_USER_HOME,
                          load_fixture)
    from lostpath.attribute import attribute_footprint

    records, _ = attribute_footprint(
        load_fixture("scan_c.json"), load_fixture("inventory.json"),
        load_fixture("shortcuts.json"), user_home=FIXTURE_USER_HOME,
        program_data=FIXTURE_PROGRAM_DATA)
    hit = [x for x in records if x["path"].lower().endswith("local\\uv")]
    assert len(hit) == 1
    assert hit[0]["cat"] == "可再生缓存"
    assert hit[0]["redirect"] == "UV_CACHE_DIR"


def test_zone_rule_needs_path_and_does_not_break_generic_lookup():
    """拿不到路径时只走通用表——不能因为少个参数就把归属判没了。"""
    r = KB.lookup_owner_toolchain("uv")
    assert r is not None and r["label"] == "uv (Python)"


def test_uv_tool_dir_redirect_is_manual_not_env():
    r"""两个变量的重定向必须是 manual。

    redirect.py 的分级规则：涉及多个变量就不许自动执行。而且**变量名是 UV_TOOL_DIR**
    ——实测 uv 0.7.13 不认 UV_TOOL_INSTALL_DIR，设了它 `uv tool dir` 仍返回默认位置，
    正是这个模块要防的"设了个软件根本不读的变量"。
    """
    from lostpath.act import redirect as redirect_mod

    hint = KB.lookup_owner_toolchain(
        "uv", r"C:\Users\someone\AppData\Roaming\uv")["redirect"]
    m = redirect_mod.resolve(hint)
    assert m["kind"] == "manual", f"{hint} 判成了 {m['kind']}，会被自动执行"
    assert not redirect_mod.is_auto_redirectable(hint)
    assert "UV_TOOL_DIR" in m["how"]


def test_roaming_uv_is_no_longer_offered_for_cleanup(tmp_path):
    r"""端到端：计划器不再把 Roaming\uv 当可清理项。

    这条才是用户能感知的那一层——上面几条只验规则表，这条验它真的传导到了拦阻。
    """
    from lostpath.act import planner

    d = tmp_path / "uv"
    d.mkdir()
    rec = {"path": str(d), "name": "uv", "size": 200 * 2**20, "files": 10,
           "cat": "混合", "owner_kind": "toolchain", "conf": 0.85,
           "redirect": "UV_TOOL_DIR / UV_PYTHON_INSTALL_DIR", "owner": "uv (Python)"}
    p = planner.plan_for(rec)
    assert not p.executable
    assert "not_cleanable" in {b.code for b in p.blockers}
