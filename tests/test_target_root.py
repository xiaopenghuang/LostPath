r"""迁移目标位置的校验与持久化。

**这些用例存在的理由**：`target_root` 会一路进 `os.path.join`，然后决定把用户几个 GiB
的数据搬到哪。它此前零校验——界面上没有入口，所以只有集成测试在传它，而集成测试传的
都是合法的 tmp_path。加了界面入口就等于把这个参数交到用户手里，每条规则都得有用例盯着。

**为什么大量用 monkeypatch 打 `_drive_type`**：驱动器类型取决于跑测试的机器有哪些盘。
本仓库已经吃过多次"指标随机器变化"的亏（见 conftest 里 unset_env_vars 的理由），
所以除了"真实可写目录"这一类必须用真路径的，其余一律打桩。
"""
import json
import os

import pytest

from lostpath.act import planner, target_root as tr
from lostpath.storage import paths as lp_paths


def codes(res):
    return [e["code"] for e in res["errors"]]


def warn_codes(res):
    return [w["code"] for w in res["warnings"]]


# ------------------------------------------------------------------ 路径形状
@pytest.mark.parametrize("raw", ["X:", "X:store", "X:store\\sub"])
def test_drive_relative_path_is_rejected(raw):
    r"""盘符后缺分隔符必须拒。

    实测 `os.path.join("E:", "x")` 得到 `"E:x"`——这是**相对于进程在 E: 上的当前
    目录**，不是 `E:\x`。当前目录恰为盘根时它看起来完全正常（`abspath` 都对），
    换个工作目录就写到别处。`planner.default_target_root` 为这个坑写了三行注释，
    但那只护住了自动挑的值；用户手输这条路此前完全没有防护。
    """
    res = tr.validate(raw)
    assert not res["ok"]
    assert "not_absolute" in codes(res)
    assert res["normalized"] is None, "校验不过时不该给出可用值"


def test_unc_path_is_rejected():
    r"""网络路径要在 isabs 之前拦。

    实测 `ntpath.isabs(r"\\NAS\share\x")` 是 **True**，只靠 isabs 会放它过去，
    一路走到建 junction 那步才失败——而那时源目录已经被移进回收区了。
    """
    res = tr.validate(r"\\NAS\share\store")
    assert not res["ok"]
    assert "unc_not_supported" in codes(res)


def test_root_relative_path_is_rejected():
    r"""`\store` 的 isabs 也是 True，但它没指定盘——落在"当前盘"，取决于工作目录。"""
    res = tr.validate(r"\store")
    assert not res["ok"]
    assert "no_drive" in codes(res)


def test_empty_is_rejected():
    for raw in (None, "", "   ", '""'):
        res = tr.validate(raw)
        assert not res["ok"], f"{raw!r} 应被拒"
        assert "empty" in codes(res)


def test_long_path_prefix_is_stripped(tmp_path, monkeypatch):
    r"""`\\?\C:\x` 剥成 `C:\x`。

    用户从别处复制路径时可能带着这个前缀。留着不会立刻出错，但会让界面显示
    `\\?\E:\...` 这种不像人写的路径（HANDOVER 里深路径那条债是同一考虑）。
    """
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    res = tr.validate("\\\\?\\" + str(tmp_path / "store"))
    assert res["ok"], res["errors"]
    assert not res["normalized"].startswith("\\\\?\\")
    assert res["normalized"] == str(tmp_path / "store")


def test_forward_slashes_are_normalized(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    raw = str(tmp_path / "store").replace("\\", "/")
    res = tr.validate(raw)
    assert res["ok"], res["errors"]
    assert "/" not in res["normalized"]


# ------------------------------------------------------------------ 驱动器类型
@pytest.mark.parametrize("dtype,code", [
    (tr.DRIVE_NO_ROOT_DIR, "drive_not_found"),
    (tr.DRIVE_REMOVABLE, "removable_drive"),
    (tr.DRIVE_REMOTE, "remote_drive"),
    (tr.DRIVE_CDROM, "cdrom_drive"),
    (tr.DRIVE_RAMDISK, "ramdisk_drive"),
    (tr.DRIVE_UNKNOWN, "unknown_drive_type"),
])
def test_only_fixed_drives_accepted(dtype, code, monkeypatch):
    """只有本机固定盘能当目标，且每种拒绝都要说清是哪种情况。

    理由各不相同、用户要做的事也各不相同：可移动盘是"拔了就找不到数据"，
    网络盘是"junction 建不出来"，盘不存在是"盘符变了"。合并成一句"不支持"
    等于让用户去猜。
    """
    monkeypatch.setattr(tr, "_drive_type", lambda root: dtype)
    res = tr.validate("Q:\\store")
    assert not res["ok"]
    assert code in codes(res)


def test_fixed_drive_passes(tmp_path):
    """tmp_path 是真实存在、可写的固定盘目录——这条不打桩，走完整条链路。"""
    res = tr.validate(str(tmp_path / "store"))
    assert res["ok"], res["errors"]
    assert res["normalized"] == str(tmp_path / "store")


# ------------------------------------------------------------------ 警告而非拒绝
def test_system_drive_is_warning_not_error(tmp_path, monkeypatch):
    """填系统盘只警告，不拦。

    **这条分界是刻意的**：搬到系统盘技术上完全可行，只是达不到用户的目的（腾 C 盘
    空间），该由他自己拍板；而网络盘是执行到一半必然失败，不能让他有机会按下去。

    顺带这也是集成测试能用 tmp_path 的前提——pytest 的临时目录就在系统盘上。
    """
    monkeypatch.setenv("SystemDrive", os.path.splitdrive(str(tmp_path))[0])
    res = tr.validate(str(tmp_path / "store"))
    assert res["ok"], "系统盘不该被拒"
    assert "same_as_system_drive" in warn_codes(res)


@pytest.mark.parametrize("raw", ["Q:\\", "Q:\\\\", "Q:\\."])
def test_drive_root_is_warning(raw, monkeypatch):
    r"""目标设成**盘根**只警告：各软件的目录会直接摊在盘根上，能用但乱。

    参数里带 `Q:\.` 这种形式，是因为判断发生在 normpath 之后——不归一化就会漏。
    """
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    monkeypatch.setattr(tr, "_probe_writable", lambda p: (True, None))
    res = tr.validate(raw)
    assert res["ok"]
    assert "at_drive_root" in warn_codes(res), f"{raw!r} 是盘根，该警告"


def test_folder_directly_under_drive_root_is_not_warned(monkeypatch):
    r"""盘根**下一级**是推荐形状，绝不能警告。

    这条是浏览器实测抓出来的回归：原实现判的是 `dirname(x) == drive`，于是默认值
    `E:\LostPathStore` 自己就中了 at_drive_root，而警告文案还建议"例如
    E:\LostPathStore"，在界面上自相矛盾。

    **当时两条单元测试都是绿的**——它们用的 `Q:\store` 同样是盘根下一级，测试与实现
    犯了同一个错。所以这里必须显式钉住"下一级不警告"，而不只钉住"深路径不警告"。
    """
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    monkeypatch.setattr(tr, "_probe_writable", lambda p: (True, None))
    res = tr.validate("Q:\\LostPathStore")
    assert res["ok"]
    assert "at_drive_root" not in warn_codes(res), \
        "盘根下一级被当成盘根了——默认值就是这个形状"


def test_deep_path_has_no_drive_root_warning(tmp_path):
    res = tr.validate(str(tmp_path / "a" / "b"))
    assert res["ok"], res["errors"]
    assert "at_drive_root" not in warn_codes(res)


# ------------------------------------------------------------------ 自己的数据目录
def test_inside_app_data_is_rejected(isolated_data_dir):
    """不能放进 LostPath 自己的数据目录：回收区就在那儿。

    真出事的形状：目标设成 `...\\LostPath\\store`，junction 把数据搬进去，
    随后"清空回收区"沿着同一棵树走——搬过去的正式数据和待永久删除的数据在一个树下。
    """
    res = tr.validate(str(isolated_data_dir / "store"))
    assert not res["ok"]
    assert "inside_app_data" in codes(res)


def test_sibling_of_app_data_is_allowed(isolated_data_dir):
    r"""兄弟目录不算"在里面"。

    `_is_within` 必须补分隔符再比前缀：`...\lostpath-data-store` 与
    `...\lostpath-data` 有共同前缀但不是父子关系。`planner._running_under`
    踩过同一个坑（只判前缀会把兄弟目录误判成子目录）。
    """
    sibling = str(isolated_data_dir) + "-store"
    res = tr.validate(sibling)
    assert res["ok"], res["errors"]


def test_is_within_rejects_prefix_sibling():
    assert tr._is_within(r"E:\Store\sub", r"E:\Store")
    assert tr._is_within(r"E:\Store", r"E:\Store")
    assert not tr._is_within(r"E:\Store2", r"E:\Store")
    assert not tr._is_within(r"E:\Other", r"E:\Store")


# ------------------------------------------------------------------ 写入探测
def test_probe_writable_walks_up_to_existing_ancestor(tmp_path):
    """目标目录通常还不存在，要往上找第一个存在的祖先来试。

    不预先创建目标目录是刻意的：用户改两次主意就会在盘上留下两个空目录。
    """
    ok, why = tr._probe_writable(str(tmp_path / "a" / "b" / "c"))
    assert ok, why


def test_probe_writable_leaves_no_files(tmp_path):
    """探测不能留垃圾——它在用户的盘上跑，且可能跑很多次（输入时实时校验）。"""
    before = set(os.listdir(tmp_path))
    tr._probe_writable(str(tmp_path / "store"))
    assert set(os.listdir(tmp_path)) == before


def test_probe_skips_drive_root(tmp_path, monkeypatch):
    r"""最近的已存在祖先是盘根时不做探测。

    **这条是实测撞出来的**：`tempfile.TemporaryFile(dir="C:\\")` 在标准权限下 90 秒
    不返回（同一函数在 `E:\` 上 0.7 ms）。界面每敲一个字符校验一次，用户打出 `C:\`
    的瞬间请求就挂住了——在浏览器里看到"正在核对…"再也不消失才发现的。

    语义上也该跳过：要建的是盘根**下面**的目录，而"能否在盘根直接建文件"是另一回事。
    """
    drive = os.path.splitdrive(str(tmp_path))[0]
    called = []
    monkeypatch.setattr(tr.tempfile, "TemporaryFile",
                        lambda **kw: called.append(kw) or (_ for _ in ()).throw(
                            AssertionError("盘根不该被探测")))
    ok, why = tr._probe_writable(drive + "\\NoSuchDirHere")
    assert ok, why
    assert not called, "在盘根上做了写入探测"


def test_probe_has_a_timeout(tmp_path, monkeypatch):
    """探测挂住时按"可以用"放过，不能把界面卡死。

    写不进去的话执行阶段会如实报错并中止（那里有完整的回滚记录），代价可控；
    而输入框卡住会让用户以为整个程序死了。
    """
    import time

    monkeypatch.setattr(tr, "_PROBE_TIMEOUT_SEC", 0.25)

    class Hang:
        def __init__(self, **kw):
            time.sleep(30)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(tr.tempfile, "TemporaryFile", Hang)
    t0 = time.perf_counter()
    ok, _ = tr._probe_writable(str(tmp_path / "sub" / "deeper"))
    dt = time.perf_counter() - t0
    assert ok, "超时该按可以用处理"
    assert dt < 3, f"超时没起作用，等了 {dt:.1f}s"


def test_system_drive_path_validates_promptly():
    r"""系统盘上一个不存在的目录必须能**及时**校验完。

    这是那个挂住场景的回归判据，且在任何机器上都成立（不依赖本机 C:\ 会不会挂）：
    校验是输入时逐字符触发的，几秒不返回就等于界面卡住。
    """
    import time

    sysdrive = (os.environ.get("SystemDrive") or "C:").rstrip("\\")
    t0 = time.perf_counter()
    res = tr.validate(f"{sysdrive}\\LostPathStoreProbeTarget")
    dt = time.perf_counter() - t0
    assert dt < 3, f"校验用了 {dt:.1f}s，界面会卡住"
    # 顺带钉住结论：系统盘是"能用但腾不出空间"，不是错误
    assert res["ok"], res["errors"]
    assert "same_as_system_drive" in warn_codes(res)


def test_not_writable_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_probe_writable", lambda p: (False, "权限不足"))
    res = tr.validate(str(tmp_path / "store"))
    assert not res["ok"]
    assert "not_writable" in codes(res)


# ------------------------------------------------------------------ 持久化
def test_save_then_effective(tmp_path):
    target = str(tmp_path / "store")
    res = tr.save(target)
    assert res["ok"], res["errors"]
    assert tr.load_raw() == target
    assert tr.effective() == target


def test_save_rejects_and_does_not_write(tmp_path):
    """校验不过就不落盘。

    存一个用不了的值，下次启动只会在别处以更难懂的方式失败——而且用户会以为
    "我设过了"，拿着错误的预期去按执行。
    """
    tr.save(str(tmp_path / "good"))
    res = tr.save("X:relative")
    assert not res["ok"]
    assert tr.load_raw() == str(tmp_path / "good"), "坏值把好值冲掉了"


def test_clear_falls_back_to_auto(tmp_path):
    tr.save(str(tmp_path / "store"))
    res = tr.save(None)
    assert res["ok"]
    assert res["normalized"] is None
    assert tr.load_raw() is None
    assert tr.effective() is None


def test_clear_when_nothing_saved_is_ok():
    """没设过也能"清除"——幂等，界面上那个"恢复默认"按钮不该报错。"""
    assert tr.save(None)["ok"]


def test_effective_revalidates_on_every_read(tmp_path, monkeypatch):
    """存的时候合法不代表现在合法：盘会被拔、盘符会变。

    **这条是这个模块存在的主要理由之一。** 拿一个已失效的路径去搬几个 GiB 的数据，
    失败点会出现在复制到一半的时候，而那时源目录已经进了回收区。
    """
    target = str(tmp_path / "store")
    assert tr.save(target)["ok"]
    assert tr.effective() == target
    # 模拟"盘拔了"
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_NO_ROOT_DIR)
    assert tr.effective() is None, "失效的值仍被当成可用"
    assert tr.load_raw() == target, "load_raw 该原样返回用户设过什么"


def test_corrupt_config_is_treated_as_unset(tmp_path):
    """半个 JSON（写盘时断电）当作没设过，而不是抛异常让整个 /api/plan 挂掉。"""
    cfg = lp_paths.target_root_config()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{ not json", encoding="utf-8")
    assert tr.load_raw() is None
    assert tr.effective() is None


def test_save_is_atomic(tmp_path):
    """先写临时文件再 replace，不留 .tmp 残骸。"""
    tr.save(str(tmp_path / "store"))
    cfg = lp_paths.target_root_config()
    leftovers = [p for p in os.listdir(cfg.parent) if p.endswith(".tmp")]
    assert not leftovers, f"留下了临时文件 {leftovers}"
    assert json.loads(cfg.read_text(encoding="utf-8"))["target_root"] \
        == str(tmp_path / "store")


# ------------------------------------------------------------------ 与计划器接合
def test_effective_target_root_prefers_saved(tmp_path):
    target = str(tmp_path / "store")
    tr.save(target)
    assert planner.effective_target_root() == target


def test_effective_target_root_falls_back_to_auto():
    assert planner.effective_target_root() == planner.default_target_root()


def test_default_target_root_ignores_saved(tmp_path):
    """`default_target_root` 必须只做"自动挑"。

    让它去读配置，`test_default_target_root_is_absolute` 在设过配置的机器上就再也
    测不到自动挑那段代码了——正是本仓库反复踩的"靠巧合绿"那一族。
    """
    tr.save(str(tmp_path / "store"))
    auto = planner.default_target_root()
    assert auto != str(tmp_path / "store")
    assert os.path.isabs(auto)


def test_plan_all_uses_saved_target_root(tmp_path):
    """整份计划里的 target 要落在用户设的根下面。"""
    tr.save(str(tmp_path / "store"))
    src = tmp_path / "src" / "SomeCache"
    src.mkdir(parents=True)
    (src / "f.bin").write_bytes(b"x" * 1024)
    rec = {
        "path": str(src), "name": "SomeCache", "size": 800 * 1024 * 1024,
        "files": 10, "owner": "SomeApp", "owner_kind": "app",
        "cat": "可再生缓存", "confidence": 0.9,
    }
    out = planner.plan_all([rec], target_root=None)
    assert out["target_root"] == str(tmp_path / "store")


def test_explicit_target_root_overrides_saved(tmp_path):
    """显式传参只对本次生效，不写配置——试算一个位置不该改设置。"""
    tr.save(str(tmp_path / "saved"))
    out = planner.plan_all([], target_root=str(tmp_path / "once"))
    assert out["target_root"] == str(tmp_path / "once")
    assert tr.load_raw() == str(tmp_path / "saved")


# ── 逐项覆盖 ────────────────────────────────────────────────────────────────


def _junctionable(path) -> dict:
    """一条会走 junction 的记录（cat 不可清理、owner_kind=app、体积过门槛）。"""
    return {"path": str(path), "name": "SomeApp", "size": 3 * 2**30, "files": 10,
            "cat": "程序数据", "owner_kind": "app", "conf": 0.9,
            "redirect": None, "owner": "SomeApp", "why": "测试用"}


def _mk(p):
    p.mkdir(parents=True, exist_ok=True)
    (p / "blob").write_bytes(b"x" * 4096)
    return p


def test_override_beats_global_root(tmp_path, monkeypatch):
    """为某一项单独指定的根，优先于全局根。"""
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    src = _mk(tmp_path / "src" / "SomeApp")
    tr.save(str(tmp_path / "global"))
    assert tr.set_override(str(src), str(tmp_path / "special"))["ok"]

    t = planner.plan_for(_junctionable(src)).target
    assert t and t.startswith(str(tmp_path / "special")), \
        f"覆盖没生效，target={t}"


def test_override_beats_explicitly_passed_root(tmp_path, monkeypatch):
    """逐项覆盖也优先于调用方一次性传进来的根。

    它是用户针对这一条做的、已落盘的决定，比"请求顺带带上的全局值"更具体。
    """
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    src = _mk(tmp_path / "src" / "SomeApp")
    assert tr.set_override(str(src), str(tmp_path / "special"))["ok"]

    t = planner.plan_for(_junctionable(src),
                         target_root=str(tmp_path / "passed")).target
    assert t and t.startswith(str(tmp_path / "special")), \
        f"传参盖掉了覆盖，target={t}"


def test_plan_and_executor_resolve_the_same_target(tmp_path, monkeypatch):
    """**出计划与执行必须解析出同一个目标。**

    executor 会重新 plan_for 一遍。两边规则不一致就会出现"界面显示搬到 G、
    实际搬到 E"——比不灵活危险得多，而且用户无从察觉。
    """
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    src = _mk(tmp_path / "src" / "SomeApp")
    tr.save(str(tmp_path / "global"))
    tr.set_override(str(src), str(tmp_path / "special"))
    rec = _junctionable(src)

    shown = planner.plan_all([rec])["plans"][0]["target"]
    # executor 拿到的是同一条路径、同样调 plan_for（它内部就这么做）
    fresh = planner.plan_for(rec).target
    assert shown == fresh, f"展示 {shown} 与执行 {fresh} 不一致"


def test_clearing_override_falls_back_to_global(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    src = _mk(tmp_path / "src" / "SomeApp")
    tr.save(str(tmp_path / "global"))
    tr.set_override(str(src), str(tmp_path / "special"))
    assert tr.set_override(str(src), None)["ok"]

    t = planner.plan_for(_junctionable(src)).target
    assert t and t.startswith(str(tmp_path / "global"))
    assert tr.load_overrides() == {}


def test_saving_global_root_keeps_overrides(tmp_path, monkeypatch):
    """改全局根不能把逐项覆盖冲掉。

    `save()` 原先直接写 `{"target_root": ...}`，会整块覆盖配置文件——用户改一次
    全局根就丢掉所有逐项设置，而且无声无息。
    """
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    src = tmp_path / "src" / "SomeApp"
    tr.set_override(str(src), str(tmp_path / "special"))
    tr.save(str(tmp_path / "another"))

    assert tr.load_overrides().get(str(src).lower()) == str(tmp_path / "special"), \
        "改全局根把覆盖表冲掉了"


def test_override_keys_are_case_insensitive(tmp_path, monkeypatch):
    """Windows 路径不区分大小写，键必须统一，否则同一目录会存出两条互不可见的覆盖。"""
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    src = _mk(tmp_path / "src" / "SomeApp")
    tr.set_override(str(src).upper(), str(tmp_path / "special"))

    t = planner.plan_for(_junctionable(src)).target
    assert t and t.startswith(str(tmp_path / "special")), \
        f"大小写不同就找不到覆盖，target={t}"


def test_lookup_normalizes_keys_read_from_disk(tmp_path, monkeypatch):
    """**读取端**也要归一化大小写，不能只靠写入端。

    只测「set_override 大写、查小写」是不够的：写入端已经把键转小写了，所以
    读取端去掉归一化照样能找到——那条测试对读取端的变异是绿的（实测确认）。
    这里直接写一份大写键的配置，模拟手改过的、或旧版本留下的文件。
    """
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    src = _mk(tmp_path / "src" / "SomeApp")
    cfg = tr.config_file()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "target_root": str(tmp_path / "global"),
        "overrides": {str(src).upper(): str(tmp_path / "special")},
    }, ensure_ascii=False), encoding="utf-8")

    assert tr.load_overrides().get(str(src).lower()) == str(tmp_path / "special"), \
        "磁盘上的大写键没被归一化"
    t = planner.plan_for(_junctionable(src)).target
    assert t and t.startswith(str(tmp_path / "special")), \
        f"读取端没归一化，找不到覆盖，target={t}"


def test_invalid_override_is_not_saved(tmp_path, monkeypatch):
    """校验不过不落盘——存一个用不了的值只会在别处以更难懂的方式失败。"""
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    src = tmp_path / "src" / "SomeApp"
    res = tr.set_override(str(src), r"\\NAS\share\x")   # UNC，junction 不支持
    assert not res["ok"], "UNC 路径被接受了"
    assert tr.load_overrides() == {}


def test_broken_override_falls_back_silently(tmp_path, monkeypatch):
    """存过的覆盖失效了（盘拔了、盘符变了）就回落到全局根，不抛。

    出计划是只读操作，此刻报错没有用户能采取的动作。
    """
    monkeypatch.setattr(tr, "_drive_type", lambda root: tr.DRIVE_FIXED)
    src = _mk(tmp_path / "src" / "SomeApp")
    tr.save(str(tmp_path / "global"))
    tr.set_override(str(src), str(tmp_path / "special"))
    # 让覆盖指向的位置变成写不进去（盘拔了 / 目录没了）。
    # 打 _probe_writable 而不是 _drive_type：后者收的是**盘符**（"C:\\"），
    # 按路径名去判会永不匹配——第一版就是这么写的，测试假绿。
    monkeypatch.setattr(
        tr, "_probe_writable",
        lambda p: (False, "盘不见了") if "special" in str(p).lower() else (True, None))

    t = planner.plan_for(_junctionable(src)).target
    assert t and t.startswith(str(tmp_path / "global")), \
        f"失效的覆盖没有回落，target={t}"
