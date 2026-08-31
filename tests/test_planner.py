r"""计划器回归。**重点在拦阻，不在候选列表。**

这个工具的用户是非程序员，他会信任界面给的建议。所以宁可把能动的判成不能动（他损失
一次优化机会），也不能把不能动的判成能动（他丢数据）。因此每条拦阻规则都单独有测试，
而"能动"只需少量用例。
"""
import ntpath
import os
from types import SimpleNamespace

import pytest

from lostpath.act import planner
from lostpath.act import redirect as redirect_mod


def rec(tmp_path, name="cache", size=200 * 2**20, cat="可再生缓存",
        owner_kind="toolchain", conf=0.9, redirect=None, files=10,
        make=True, owner="某工具"):
    """造一条痕迹记录，目录默认真实存在（计划器会查磁盘）。"""
    p = tmp_path / name
    if make:
        p.mkdir(parents=True, exist_ok=True)
    return {
        "path": str(p), "name": name, "size": size, "files": files,
        "cat": cat, "owner_kind": owner_kind, "conf": conf,
        "redirect": redirect, "owner": owner, "why": "测试用",
    }


def codes(plan):
    return {b.code for b in plan.blockers}


# --------------------------------------------------------------- 拦阻规则
def test_missing_directory_is_blocked(tmp_path):
    p = planner.plan_for(rec(tmp_path, make=False))
    assert not p.executable
    assert "missing" in codes(p)


def test_too_small_is_blocked(tmp_path):
    p = planner.plan_for(rec(tmp_path, size=1 * 2**20))
    assert not p.executable
    assert "too_small" in codes(p)


def test_min_size_threshold_is_50mib():
    assert planner.MIN_ACTIONABLE_SIZE == 50 * 1024 * 1024


@pytest.mark.parametrize("cat", ["用户数据", "不可动", "未定性", None, "混合"])
def test_non_cleanable_categories_are_blocked(tmp_path, cat):
    """只处理可再生缓存与可清理项。未定性的先补知识库规则，不靠猜。"""
    p = planner.plan_for(rec(tmp_path, cat=cat))
    assert not p.executable
    assert "not_cleanable" in codes(p)


@pytest.mark.parametrize("kind,code", [
    ("container", "owner_container"),
    ("system", "owner_system"),
    ("vendor", "owner_vendor"),
])
def test_blocked_owner_kinds(tmp_path, kind, code):
    """容器体积由子目录承担；系统与厂商共享目录影响面超出单个软件。"""
    p = planner.plan_for(rec(tmp_path, owner_kind=kind))
    assert not p.executable
    assert code in codes(p)


def test_low_confidence_is_blocked(tmp_path):
    """判错归属会动到别的软件的数据，所以置信度不足一律不动。"""
    p = planner.plan_for(rec(tmp_path, conf=0.5))
    assert not p.executable
    assert "low_confidence" in codes(p)


def test_confidence_threshold_boundary(tmp_path):
    assert planner.plan_for(rec(tmp_path, conf=0.59)).blockers
    assert not planner.plan_for(rec(tmp_path, conf=0.60)).blockers


def test_reparse_point_is_blocked(tmp_path, monkeypatch):
    """已是 junction 的目录体积本就不在本盘，再迁移毫无意义且会绕晕自己。"""
    monkeypatch.setattr(planner, "is_reparse_point", lambda p: True)
    p = planner.plan_for(rec(tmp_path))
    assert not p.executable
    assert "already_linked" in codes(p)


def test_running_process_blocks_action(tmp_path):
    """软件正在运行时不动它的文件。"""
    r = rec(tmp_path)
    p = planner.plan_for(r, process_dirs={r["path"].lower()})
    assert not p.executable
    assert "in_use" in codes(p)


def test_running_process_in_subdir_blocks_action(tmp_path):
    r"""进程在**子目录**里跑，同样得拦——这是真实故障的形状。

    `C:\...\Local\uv` 被判可执行并执行，而解释器在
    `...\uv\cache\archive-v0\<hash>\Scripts\` 里跑着。上面那条用例传的是
    `{r["path"].lower()}`，**恰好是相等那种**，所以只判相等的实现也能过——闸门
    实际是靠这个巧合看起来有效的。这条用例专门传子孙路径。
    """
    r = rec(tmp_path)
    deep = os.path.join(r["path"], "cache", "archive-v0", "abc123", "Scripts")
    p = planner.plan_for(r, process_dirs={deep.lower()})
    assert not p.executable, "进程在子目录里跑，也必须拦下"
    assert "in_use" in codes(p)
    reason = " ".join(b.reason for b in p.blockers)
    assert "子目录" in reason, f"提示该说清进程在哪一层：{reason}"


def test_sibling_process_dir_does_not_block(tmp_path):
    r"""同前缀但不同目录的进程不该误拦。

    `...\uv-tools` 以 `...\uv` 为字符串前缀，但它不是 `uv` 的子目录。纯
    startswith 不补分隔符就会把它误判成"uv 正在运行"，让本可清理的目录永远清不掉。
    """
    r = rec(tmp_path)
    # 必须由 r["path"] 派生：拼 tmp_path 得到的是计划路径的**叔父**，与目标不同前缀，
    # 那样这条用例碰不到被测的那行判断，也就抓不住"少补分隔符"这个错。
    sibling = r["path"] + "-tools"
    p = planner.plan_for(r, process_dirs={sibling.lower()})
    assert "in_use" not in codes(p), "同前缀的兄弟目录不是子目录，不该拦"


def test_temp_redirect_is_refused(tmp_path):
    """TEMP/TMP 是全系统共用的，本工具不该改它。

    这条最重要：文档里写着"7 条自带官方重定向变量，改环境变量即可"，照字面实现就会
    做出一个能改用户系统 TEMP 的功能。
    """
    p = planner.plan_for(rec(tmp_path, name="Temp", cat="可清理",
                             owner_kind="system", redirect="TEMP / TMP"))
    assert not p.executable
    assert "unsafe_redirect" in codes(p) or "owner_system" in codes(p)


def test_manual_redirect_is_not_auto_executed(tmp_path):
    """官方做法不是环境变量的，交回给人并给出具体命令，不假装能自动办。"""
    p = planner.plan_for(rec(tmp_path, name="npm-cache",
                             redirect="npm config set cache"))
    assert not p.executable
    assert "manual_redirect" in codes(p)
    assert any("npm config set cache" in b.reason for b in p.blockers)


def test_unknown_redirect_hint_falls_back_to_manual(tmp_path):
    """知识库新增规则而这里没跟上时，必须退成 manual 而不是猜成环境变量。

    猜错的后果：去设一个软件根本不读的变量，然后报"迁移成功"，而旧缓存已删、软件
    在老位置重新下载。用户看到的是"清理了但没效果"。
    """
    p = planner.plan_for(rec(tmp_path, redirect="SOME_FUTURE_THING=x"))
    assert not p.executable
    assert "manual_redirect" in codes(p)


def test_target_drive_full_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "drive_free_bytes", lambda p: 100 * 2**20)
    p = planner.plan_for(rec(tmp_path, redirect="UV_CACHE_DIR"),
                         target_root=str(tmp_path / "dst"))
    assert not p.executable
    assert "target_full" in codes(p)


def test_unknown_free_space_warns_but_does_not_block(tmp_path, monkeypatch):
    """读不到容量时给提示而非放行成"充足"，也不该直接否掉。"""
    monkeypatch.setattr(planner, "drive_free_bytes", lambda p: None)
    p = planner.plan_for(rec(tmp_path, redirect="UV_CACHE_DIR"),
                         target_root=str(tmp_path / "dst"))
    assert p.executable
    assert any("无法读取目标盘" in n for n in p.notes)


# --------------------------------------------------------------- 动作选择
def test_env_redirect_preferred_over_moving_files(tmp_path, monkeypatch):
    """有官方环境变量时不搬文件——让软件自己去新位置重建，比搬运可靠。"""
    monkeypatch.setattr(planner, "drive_free_bytes", lambda p: 500 * 2**30)
    p = planner.plan_for(rec(tmp_path, redirect="UV_CACHE_DIR"),
                         target_root=str(tmp_path / "dst"))
    assert p.action == "redirect"
    assert p.env_var == "UV_CACHE_DIR"
    assert p.executable
    assert os.path.isabs(p.target), f"目标必须是绝对路径：{p.target}"
    assert any("重启" in n for n in p.notes), "环境变量只对新进程生效，必须提示"


def test_no_redirect_becomes_cleanup(tmp_path):
    """无官方重定向的可再生缓存直接清理，比建 junction 低一个量级的风险。"""
    p = planner.plan_for(rec(tmp_path, redirect=None))
    assert p.action == "cleanup"
    assert p.executable
    assert p.reclaimable == p.size


# ------------------------------------------- 变量已被用户自己设过（回归防线）
# 这一组防的是一个会造成实际损失的 bug：原先出计划前从不读环境变量现值，于是对一台
# 已经把 UV_CACHE_DIR 设到 G:\UV\uvcache 的机器，照样提议"设置 UV_CACHE_DIR = <本
# 工具目标盘>"。真执行的话写进 HKCU 的值会盖掉 HKLM 那份（用户级优先），软件转而用
# 本工具选的位置，而用户原来那份缓存（实测某机器 16.10 GiB）变成谁都不读的孤儿——它
# 既不在 C 盘所以本工具扫不到，也不再被软件使用，而界面显示的是"腾出成功"。
#
# 当时唯一"变量已设"的真实案例（两个 uv 目录）恰好被 env_var_conflict 拦住了，但那
# 条拦阻的理由是另一件事（两个目录争同一个变量）。**保护是巧合而非设计**，与项目里
# 记过的 Package Cache 那次"两个缺陷互相掩盖"同款。

def lookup(value, scope="machine"):
    """冒充"这个变量当前是什么值"。conftest 默认全部未设，这里按需覆盖。"""
    return lambda name: (value, scope)


def test_var_already_pointing_off_drive_cleans_instead_of_resetting(tmp_path):
    r"""变量已指向别的盘 → 只清残留，绝不再设变量。

    用户自己做过重定向，软件已经在用新位置。此时再设一次只会把他选的位置换成我们
    的，让他原来那份缓存变孤儿。而旧位置那份残留仍该清掉——所以动作降级为 cleanup，
    腾出的空间一分不少。
    """
    p = planner.plan_for(rec(tmp_path, redirect="UV_CACHE_DIR"),
                         env_lookup=lookup(r"G:\UV\uvcache"))
    assert p.action == "cleanup", "已重定向过就不该再走 redirect"
    assert p.env_var is None, "env_var 非空意味着'这份计划会设这个变量'，此处不设"
    assert p.executable, "旧残留仍然该能清掉，不该被一并否掉"
    assert p.reclaimable == p.size
    titles = " ".join(s["title"] for s in p.steps)
    assert "环境变量" not in titles, f"步骤里不许出现设变量：{titles}"
    assert any(r"G:\UV\uvcache" in n for n in p.notes), "要告诉用户现值是什么"


def test_var_already_set_on_same_drive_is_blocked(tmp_path):
    """变量已设但仍在本盘上 → 是用户的明确选择，不静默覆盖，交回给人。"""
    same = str(tmp_path / "mycache")
    p = planner.plan_for(rec(tmp_path, redirect="UV_CACHE_DIR"),
                         env_lookup=lookup(same, scope="user"))
    assert not p.executable
    assert "env_var_already_set" in codes(p)
    reason = " ".join(b.reason for b in p.blockers)
    assert same in reason, "拦阻理由里要带上现值，否则用户不知道该怎么决定"


def test_empty_var_counts_as_unset(tmp_path, monkeypatch):
    """空字符串按未设处理——软件读到空值会退回自己的默认位置，与没设一样。"""
    monkeypatch.setattr(planner, "drive_free_bytes", lambda p: 500 * 2**30)
    p = planner.plan_for(rec(tmp_path, redirect="UV_CACHE_DIR"),
                         target_root=str(tmp_path / "dst"),
                         env_lookup=lookup("", scope="user"))
    assert p.action == "redirect"


def test_two_dirs_claiming_a_redirected_var_both_become_cleanable(tmp_path):
    r"""实测原型：`Local\uv` 与 `Roaming\uv` 都申领 UV_CACHE_DIR，而该变量已被用户
    设到 G 盘。两条都该降级成 cleanup，于是 env_var_conflict 自然消失、两份残留都能
    清——原先是两条都卡在冲突上，一份都清不掉。
    """
    a = rec(tmp_path, name="uv-local", redirect="UV_CACHE_DIR")
    b = rec(tmp_path, name="uv-roaming", redirect="UV_CACHE_DIR")
    res = planner.plan_all([a, b], target_root=str(tmp_path / "dst"),
                           env_lookup=lookup(r"G:\UV\uvcache"))
    assert res["summary"]["blocker_counts"].get("env_var_conflict") is None
    for p in res["plans"]:
        assert p["action"] == "cleanup"
        assert p["executable"], p["blockers"]


@pytest.mark.real_env_lookup
def test_effective_var_prefers_user_over_machine(monkeypatch):
    """用户级优先，其次系统级，都无才算未设。

    **必须连 HKLM 一起读**：只读 HKCU 是这个 bug 的成因——把变量设在系统级的机器
    会被判成"没设过"，于是计划器提议再设一个用户级值把它盖掉。
    """
    from lostpath.act import envvar

    monkeypatch.setattr(envvar, "get_user_var", lambda n: None)
    monkeypatch.setattr(envvar, "get_machine_var", lambda n: r"G:\only-machine")
    assert envvar.effective_var("X") == (r"G:\only-machine", "machine")

    monkeypatch.setattr(envvar, "get_user_var", lambda n: r"G:\user-wins")
    assert envvar.effective_var("X") == (r"G:\user-wins", "user")

    monkeypatch.setattr(envvar, "get_user_var", lambda n: None)
    monkeypatch.setattr(envvar, "get_machine_var", lambda n: None)
    assert envvar.effective_var("X") == (None, None)


def test_cleanup_steps_are_reversible_before_expiry(tmp_path):
    """清理必须先移入回收区、写回滚清单，过期才真删。"""
    p = planner.plan_for(rec(tmp_path))
    titles = " ".join(s["title"] for s in p.steps)
    assert "回收区" in titles
    assert any("回滚" in (s.get("reversible") or "") + s["detail"] for s in p.steps)


def test_plan_has_no_side_effects(tmp_path):
    """计划器全程只读：跑完之后目录内容与目标位置都不该有任何变化。"""
    r = rec(tmp_path, redirect="UV_CACHE_DIR")
    (tmp_path / r["name"] / "payload.bin").write_bytes(b"x" * 100)
    dst = tmp_path / "dst"
    before = sorted(os.walk(tmp_path))

    planner.plan_for(r, target_root=str(dst))

    assert sorted(os.walk(tmp_path)) == before, "计划器动了文件系统"
    assert not dst.exists(), "计划器提前创建了目标目录"


# ------------------------------------------------------- 环境变量冲突检测
def test_same_env_var_claimed_twice_blocks_both(tmp_path, monkeypatch):
    """一个变量只能指向一处。实测触发：Local\\uv 与 Roaming\\uv 都要 UV_CACHE_DIR。

    不拦的后果：两条各自执行，第二条覆盖第一条，结果"都报成功、其中一个没生效、
    而旧缓存都已删"。这种错误执行完之后极难发现。
    """
    monkeypatch.setattr(planner, "drive_free_bytes", lambda p: 500 * 2**30)
    monkeypatch.setattr(planner, "running_process_dirs", lambda: set())
    a = rec(tmp_path, name="uv-local", redirect="UV_CACHE_DIR")
    b = rec(tmp_path, name="uv-roaming", redirect="UV_CACHE_DIR")

    out = planner.plan_all([a, b], target_root=str(tmp_path / "dst"))
    assert out["summary"]["executable"] == 0, "冲突的两条都必须拦下"
    for p in out["plans"]:
        assert any(x["code"] == "env_var_conflict" for x in p["blockers"])


def test_distinct_env_vars_do_not_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "drive_free_bytes", lambda p: 500 * 2**30)
    monkeypatch.setattr(planner, "running_process_dirs", lambda: set())
    a = rec(tmp_path, name="uv", redirect="UV_CACHE_DIR")
    b = rec(tmp_path, name="Yarn", redirect="YARN_CACHE_FOLDER")
    out = planner.plan_all([a, b], target_root=str(tmp_path / "dst"))
    assert out["summary"]["executable"] == 2


# --------------------------------------------------------------- 汇总与路径
def test_summary_counts_and_reclaimable(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "running_process_dirs", lambda: set())
    good = rec(tmp_path, name="good", size=200 * 2**20)
    small = rec(tmp_path, name="small", size=1 * 2**20)
    data = rec(tmp_path, name="userdata", cat="用户数据")
    out = planner.plan_all([good, small, data], target_root=str(tmp_path / "dst"))
    s = out["summary"]
    assert s["total_candidates"] == 3
    assert s["executable"] == 1
    assert s["blocked"] == 2
    assert s["reclaimable"] == good["size"]
    assert s["blocker_counts"]["too_small"] == 1


def test_default_target_root_is_absolute():
    """os.path.join("E:", x) 得到盘符相对路径 "E:x"，不是 "E:\\x"。

    它在进程当前目录恰为该盘根目录时看起来正常，换个工作目录就会写到别处——对一个
    搬用户数据的工具是事故。这条盯着别再退回去。
    """
    root = planner.default_target_root()
    assert os.path.isabs(root), f"目标根不是绝对路径：{root}"
    assert not root[1:].startswith(":") or root[2:3] in ("\\", "/"), \
        f"盘符后缺分隔符：{root}"


def test_default_target_root_skips_actual_system_drive(monkeypatch):
    """系统盘是 D 时，自动目标不能把 D 自己当成迁移盘。"""
    class Kernel:
        def GetLogicalDrives(self):
            return (1 << 2) | (1 << 3) | (1 << 4)  # C, D, E

        def GetDriveTypeW(self, _root):
            return 3

    monkeypatch.setattr(planner.ctypes, "windll",
                        SimpleNamespace(kernel32=Kernel()))
    monkeypatch.setattr(planner.sysdirs, "system_drive", lambda: "D:")
    monkeypatch.setattr(planner, "drive_free_bytes",
                        lambda root: {"C:\\": 100, "D:\\": 900, "E:\\": 500}[root])

    root = planner.default_target_root()

    assert root.startswith("E:\\"), root


def test_safe_name_strips_path_separators():
    assert "/" not in planner._safe_name("a/b")
    assert "\\" not in planner._safe_name("a\\b")
    assert planner._safe_name("...") == "unnamed"


# ------------------------------------------------------- 重定向机制分级表
def test_env_mechanisms_are_single_variables():
    """标成 env 的必须真是单个变量名——这一级是唯一允许自动执行的。"""
    for hint, m in redirect_mod.MECHANISMS.items():
        if m["kind"] != "env":
            continue
        assert "var" in m, f"{hint} 标成 env 却没给变量名"
        v = m["var"]
        assert " " not in v and "/" not in v, f"{hint} 的变量名可疑：{v}"


def test_temp_is_classified_unsafe():
    assert redirect_mod.resolve("TEMP / TMP")["kind"] == "unsafe"
    assert not redirect_mod.is_auto_redirectable("TEMP / TMP")


def test_multi_target_hints_are_not_env():
    """含多个变量或要改配置文件的，一律不能标成可自动执行。"""
    for hint in ("TEMP / TMP", "npm config set cache", "PNPM_HOME / store-dir",
                 "settings.xml localRepository", "GOMODCACHE / GOCACHE"):
        assert not redirect_mod.is_auto_redirectable(hint), hint


def test_resolve_none_returns_none():
    assert redirect_mod.resolve(None) is None
    assert redirect_mod.resolve("") is None


# ── 目标路径按源结构镜像 ──────────────────────────────────────────────────
#
# 全部传 user_home 而不读本机 HOME：基准 fixture 是别的机器扫的，用户名不同，
# 读本机的话断言只在开发机成立——那是"靠巧合绿"那一族。

HOME = r"C:\Users\alice"


def test_mirror_keeps_source_layout_under_root():
    r"""用户目录下的路径：去掉 home 前缀，其余层级原样挂到根下。"""
    assert planner.mirror_suffix(
        r"C:\Users\alice\AppData\Local\ms-playwright", HOME
    ) == ntpath.join("AppData", "Local", "ms-playwright")
    assert planner.mirror_target(
        r"G:\alice", r"C:\Users\alice\AppData\Roaming\Code", HOME
    ) == ntpath.join(r"G:\alice", "AppData", "Roaming", "Code")


def test_mirror_outside_home_drops_only_the_drive():
    r"""不在用户目录下（ProgramData 等）：只去盘符，保留其余层级。"""
    assert planner.mirror_suffix(r"C:\ProgramData\Adobe", HOME) == \
        ntpath.join("ProgramData", "Adobe")
    assert planner.mirror_suffix(r"C:\Program Files\Foo\Bar", HOME) == \
        ntpath.join("Program Files", "Foo", "Bar")


def test_user_sibling_dir_is_not_treated_as_under_home():
    r"""`C:\Users\alice2` 不是 `C:\Users\alice` 的子目录。

    裸 `startswith` 会判成是（实测确认），于是别的用户的目录被按本用户的相对
    路径镜像，算出来的位置完全不对。判据必须带路径分隔符。
    """
    sib = r"C:\Users\alice2\AppData\Local\x"
    assert planner.mirror_suffix(sib, HOME) == \
        ntpath.join("Users", "alice2", "AppData", "Local", "x")


def test_mirror_suffix_never_empty():
    """后缀不能为空——空后缀会让目标退化成根目录本身。

    那意味着直接往根上做 junction 或把环境变量指到根，是事故级的。
    """
    for p in [HOME, HOME + "\\", "C:\\", "", "."]:
        s = planner.mirror_suffix(p, HOME)
        assert s and s.strip("\\. "), f"{p!r} 算出空后缀 {s!r}"


def test_same_name_different_paths_get_distinct_targets(tmp_path):
    r"""同名不同路径必须落到不同目标。

    这是原先 `<root>\<软件显示名>` 的缺陷：基准数据 106 条里有 16 个重名，
    NVIDIA 一家 6 条，全算出同一个 `<root>\NVIDIA`。junction 撞 junction 被
    executor 以"目标目录已存在且非空"拒绝（安全但看不出根因）；redirect 撞
    redirect **不拦**，两个软件的环境变量指到同一个目录、缓存互相覆盖。

    不用 fixture 验：那是别机器扫的，92/106 条会被 `missing` 阻断，一条 target
    都算不出来，"没撞车"是假结论。
    """
    dst = tmp_path / "dst"
    a, b = tmp_path / "L" / "NVIDIA", tmp_path / "R" / "NVIDIA"
    for d in (a, b):
        d.mkdir(parents=True)
        (d / "blob").write_bytes(b"x" * 4096)

    # 不用 rec()：它固定把目录拼成 tmp_path/name，造不出"同名不同路径"。
    # 要落到 junction（而不是 cleanup）得满足 _junction_worthy：cat 不可清理、
    # owner_kind == "app"、owner 非空、体积过门槛。
    def at(p):
        return {"path": str(p), "name": "NVIDIA", "size": 3 * 2**30, "files": 10,
                "cat": "程序数据", "owner_kind": "app", "conf": 0.9,
                "redirect": None, "owner": "NVIDIA", "why": "测试用"}

    ta = planner.plan_for(at(a), target_root=str(dst)).target
    tb = planner.plan_for(at(b), target_root=str(dst)).target
    assert ta and tb, f"至少一条没算出 target: {ta!r} {tb!r}"
    assert ta.lower() != tb.lower(), f"两条同名记录撞到同一个目标：{ta}"
    assert ntpath.basename(ta.rstrip("\\")) == "NVIDIA"
    assert ntpath.basename(tb.rstrip("\\")) == "NVIDIA"
