"""扫描管道回归。守的是 P2 手工验过、但当时没有自动化的四件事：

1. 取消后快照一字未动（红线：默认只读，写操作要有回滚记录）
2. 同时只允许一个任务（并发会互相拖慢且抢着写同一个快照文件）
3. done 必须意味着数据已就绪（这个竞态犯过一次，表现为"扫完了但没变化"）
4. 覆盖快照前先归档留底

用注入的假采集器跑，不真扫盘：真扫十几秒且结果随机器变化，而这四件事跟扫描内容
无关。归因正确性由 test_attribution_baseline.py 负责，两边不重叠。
"""
import json
import time

import pytest

from conftest import FIXTURE_PROGRAM_DATA, FIXTURE_USER_HOME
from lostpath.scan import runner
from lostpath.scan.scan_dirs import ScanCancelled
from lostpath.storage import paths, snapshots


def run(job, fake, **kw):
    return runner.run_pipeline(
        job, scan_fn=fake["scan_fn"], inventory_fn=fake["inventory_fn"],
        shortcuts_fn=fake["shortcuts_fn"], user_home=FIXTURE_USER_HOME,
        program_data=FIXTURE_PROGRAM_DATA, **kw)


# ------------------------------------------------------------ 正常路径
def test_pipeline_writes_snapshot_and_reports(fake_collectors):
    job = runner.ScanJob()
    result = run(job, fake_collectors)

    assert result["entries"] > 0, "假采集器喂的是真 fixtures，不该归因出 0 条"
    snap = paths.latest_snapshot()
    assert snap.is_file(), "管道跑完必须留下快照"

    items, meta = snapshots.load_latest()
    assert len(items) == result["entries"]
    assert meta["present"] is True
    assert meta["schema_version"] == snapshots.SCHEMA_VERSION
    # scan_stats 是进度自校准与盲区明示的数据来源，缺了这两个功能会静默退化
    assert meta["scan_stats"]["total_dirs"] > 0
    assert "denied_count" in meta["scan_stats"]
    # elevated：**记的是"产出这份数据时有没有管理员权限"**，与"此刻的进程权限"
    # 是两件事。界面靠两者的差判断"该不该重扫"——少了它就只能用
    # `denied_count > 0` 凑，而那个在管理员下也恒为真（系统保护目录始终读不到），
    # 于是"待重扫"的提示永远消不掉。用户实测撞到过：扫了很多次仍提示待重扫。
    assert "elevated" in meta["scan_stats"], "少了它，界面判不出快照该不该重扫"
    assert isinstance(meta["scan_stats"]["elevated"], bool)


def test_all_phases_run_in_order(fake_collectors):
    job = runner.ScanJob()
    run(job, fake_collectors)
    c = fake_collectors["calls"]
    assert (c["scan"], c["inventory"], c["shortcuts"]) == (1, 1, 1), \
        "每个采集器应恰好跑一次"
    assert c["progress"], "扫描阶段必须回报进度，否则 UI 没有反馈"


def test_phase_weights_sum_to_100():
    """权重和不是 100 时进度条会到不了顶或提前撞顶。"""
    assert sum(w for _k, _l, w in runner.PHASES) == 100


# --------------------------------------------------- 取消：绝不能动快照
def test_cancel_leaves_snapshot_untouched(fake_collectors):
    """红线相关：扫描默认只读，中断不得留下半份数据。"""
    # 先造一份既有快照，模拟"用户已经扫过一次"
    snapshots.save_latest([{"path": "C:\\old", "size": 1, "owner": "旧"}],
                          scan_stats={"total_dirs": 123})
    snap = paths.latest_snapshot()
    before = (snap.read_bytes(), snap.stat().st_size)

    job = runner.ScanJob()
    job.cancel()  # 起跑前就取消，第一个检查点即命中
    with pytest.raises(ScanCancelled):
        run(job, fake_collectors)

    after = (snap.read_bytes(), snap.stat().st_size)
    assert before == after, "取消后快照被改动了"
    # 也不该留下归档副本——什么都没写，就没有可归档的动作
    archives = [p for p in paths.snapshots_dir().glob("*.json")
                if p.name != "latest.json"]
    assert not archives, f"取消却产生了归档：{archives}"


def test_cancel_midway_still_writes_nothing(fake_collectors):
    """取消发生在扫描阶段中途（假采集器在每次进度回调前查检查点）。"""
    job = runner.ScanJob()

    original = fake_collectors["scan_fn"]

    def scan_then_cancel(root, progress=None, should_cancel=None):
        # 第一次回调后就请求取消，模拟用户扫了一半点取消
        def wrapped(n, p):
            progress(n, p)
            job.cancel()
        return original(root, progress=wrapped, should_cancel=should_cancel)

    with pytest.raises(ScanCancelled):
        runner.run_pipeline(job, scan_fn=scan_then_cancel,
                            inventory_fn=fake_collectors["inventory_fn"],
                            shortcuts_fn=fake_collectors["shortcuts_fn"],
                            user_home=FIXTURE_USER_HOME,
                            program_data=FIXTURE_PROGRAM_DATA)
    assert not paths.latest_snapshot().exists(), "取消却写了快照"
    # 后续阶段不该被执行
    assert fake_collectors["calls"]["inventory"] == 0


@pytest.mark.parametrize("cancel_after", ["scan", "inventory", "shortcuts"])
def test_cancel_between_phases_is_caught(fake_collectors, cancel_after):
    """阶段之间的取消检查点必须真的生效。

    为什么单独测：采集器自己会查 should_cancel，所以"取消"看起来总是有效的。
    但用户在清单导出阶段（PowerShell 要跑 5 秒）点取消时，能拦住它的只有阶段
    之间的 check()。变异测试证实过——把 check() 全改成 pass，其余取消测试仍然
    全绿，这个缺口就是那时发现的。
    """
    job = runner.ScanJob()
    order = ["scan", "inventory", "shortcuts"]

    def wrap(name, fn):
        def inner(*a, **kw):
            out = fn(*a, **kw)
            if name == cancel_after:
                job.cancel()   # 采集器已正常返回，只有阶段间检查点能拦住
            return out
        return inner

    fns = {n: wrap(n, fake_collectors[f"{n}_fn"]) for n in order}
    with pytest.raises(ScanCancelled):
        runner.run_pipeline(job, scan_fn=fns["scan"],
                            inventory_fn=fns["inventory"],
                            shortcuts_fn=fns["shortcuts"],
                            user_home=FIXTURE_USER_HOME,
                            program_data=FIXTURE_PROGRAM_DATA)

    assert not paths.latest_snapshot().exists(), "取消却写了快照"
    # 被取消的阶段之后的采集器不该再跑
    later = order[order.index(cancel_after) + 1:]
    for n in later:
        assert fake_collectors["calls"][n] == 0, \
            f"{cancel_after} 后取消，但 {n} 仍被执行"


def test_cancel_before_save_still_writes_nothing(fake_collectors):
    """归因跑完、写快照之前取消——这是最后一个检查点，漏了就会写盘。"""
    job = runner.ScanJob()
    from lostpath.attribute import attribute_v4

    real = attribute_v4.attribute_footprint

    def attribute_then_cancel(*a, **kw):
        out = real(*a, **kw)
        job.cancel()
        return out

    runner.attribute_footprint = attribute_then_cancel
    try:
        with pytest.raises(ScanCancelled):
            run(job, fake_collectors)
    finally:
        runner.attribute_footprint = real
    assert not paths.latest_snapshot().exists(), "归因后取消却仍写了快照"


def test_cancelled_job_reports_cancelled_state(fake_collectors, monkeypatch):
    """走 start_scan 的完整路径：状态应落在 cancelled，且带明确说明。"""
    monkeypatch.setattr(runner, "scan_tree", fake_collectors["scan_fn"])
    monkeypatch.setattr(runner, "export_inventory", fake_collectors["inventory_fn"])
    monkeypatch.setattr(runner, "collect_shortcuts", fake_collectors["shortcuts_fn"])
    runner._current = None

    job = runner.start_scan()
    job.cancel()
    for _ in range(100):
        if job.state not in ("pending", "running"):
            break
        time.sleep(0.05)
    assert job.state == "cancelled", f"实际状态 {job.state}"
    assert "未改动" in job.snapshot()["detail"]


# ------------------------------------------------------- 归档：覆盖前留底
def test_existing_snapshot_is_archived_before_overwrite(fake_collectors):
    snapshots.save_latest([{"path": "C:\\old", "size": 1}])
    old_bytes = paths.latest_snapshot().read_bytes()

    job = runner.ScanJob()
    result = run(job, fake_collectors)

    assert result["archived_previous"], "覆盖快照前必须归档，这是回滚记录"
    archived = paths.snapshots_dir() / \
        result["archived_previous"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    assert archived.is_file()
    assert archived.read_bytes() == old_bytes, "归档内容与被覆盖的快照不一致"


def test_first_scan_has_nothing_to_archive(fake_collectors):
    """首次扫描（无既有快照）不该假装归档了什么。"""
    job = runner.ScanJob()
    result = run(job, fake_collectors)
    assert result["archived_previous"] is None


# ------------------------------------------------ 单例锁：同时只允许一个
def test_second_scan_is_rejected_while_running(monkeypatch):
    slow = {"release": False}

    def slow_scan(root, progress=None, should_cancel=None):
        while not slow["release"]:
            time.sleep(0.02)
        return {"root": root, "dirs": {}, "reparse_points": [], "total_dirs": 1,
                "total_files": 1, "total_bytes": 1, "elapsed_sec": 0.1,
                "denied_count": 0, "reparse_count": 0,
                "hardlink_dedup_bytes": 0}

    monkeypatch.setattr(runner, "scan_tree", slow_scan)
    runner._current = None
    first = runner.start_scan()
    try:
        for _ in range(100):
            if first.state == "running":
                break
            time.sleep(0.02)
        with pytest.raises(runner.ScanAlreadyRunning) as e:
            runner.start_scan()
        assert e.value.job.id == first.id, "冲突异常应带上正在跑的那个任务"
    finally:
        slow["release"] = True
        first.cancel()
        for _ in range(100):
            if first.state not in ("pending", "running"):
                break
            time.sleep(0.05)


def test_new_scan_allowed_after_previous_finished(fake_collectors, monkeypatch):
    monkeypatch.setattr(runner, "scan_tree", fake_collectors["scan_fn"])
    monkeypatch.setattr(runner, "export_inventory", fake_collectors["inventory_fn"])
    monkeypatch.setattr(runner, "collect_shortcuts", fake_collectors["shortcuts_fn"])
    runner._current = None

    first = runner.start_scan()
    for _ in range(200):
        if first.state not in ("pending", "running"):
            break
        time.sleep(0.05)
    assert first.state in ("done", "failed"), f"首个任务卡在 {first.state}"
    second = runner.start_scan()      # 不该抛
    second.cancel()


# --------------------------- done 必须意味着数据就绪（曾经的竞态）
def test_on_done_runs_before_state_becomes_done(fake_collectors, monkeypatch):
    """回调必须在翻 done 之前跑完。

    这是 P2 修过的竞态：原先 SSE 报 done 时服务端还在重建缓存，前端立刻拉
    /api/data 拿到的是扫描前的旧数据——界面显示"扫描完成，归因出 110 处足迹"，
    而摘要还是旧实体数、占用大户一片空白。
    """
    monkeypatch.setattr(runner, "scan_tree", fake_collectors["scan_fn"])
    monkeypatch.setattr(runner, "export_inventory", fake_collectors["inventory_fn"])
    monkeypatch.setattr(runner, "collect_shortcuts", fake_collectors["shortcuts_fn"])
    runner._current = None

    observed = {}

    def on_done(job):
        # 回调执行时任务不能已经是 done，否则前端可能已经拉过数据了
        observed["state_during_callback"] = job.state
        observed["percent"] = job.snapshot()["percent"]

    job = runner.start_scan(on_done=on_done)
    for _ in range(200):
        if job.state not in ("pending", "running"):
            break
        time.sleep(0.05)

    assert job.state == "done", f"任务未成功：{job.state} / {job.error}"
    assert observed["state_during_callback"] == "running", (
        "on_done 在 state 已翻 done 之后才跑——竞态回归了")
    assert observed["percent"] < 100, "翻 done 前进度不该已是 100"


def test_on_done_failure_does_not_fail_the_scan(fake_collectors, monkeypatch):
    """缓存重建失败不该把扫描本身判成失败——快照已经写成功了。"""
    monkeypatch.setattr(runner, "scan_tree", fake_collectors["scan_fn"])
    monkeypatch.setattr(runner, "export_inventory", fake_collectors["inventory_fn"])
    monkeypatch.setattr(runner, "collect_shortcuts", fake_collectors["shortcuts_fn"])
    runner._current = None

    def boom(job):
        raise RuntimeError("重建炸了")

    job = runner.start_scan(on_done=boom)
    for _ in range(200):
        if job.state not in ("pending", "running"):
            break
        time.sleep(0.05)
    assert job.state == "done"
    # 但失败要留痕，不能静默（M3 图标全丢就是被 except-pass 藏住的）
    log = paths.logs_dir() / "scan.log"
    assert log.is_file() and "重建炸了" in log.read_text(encoding="utf-8")


# ------------------------------------------------------------ 失败路径
def test_collector_failure_marks_failed_and_logs(fake_collectors, monkeypatch):
    def bad_inventory():
        raise RuntimeError("清单导出失败")

    monkeypatch.setattr(runner, "scan_tree", fake_collectors["scan_fn"])
    monkeypatch.setattr(runner, "export_inventory", bad_inventory)
    monkeypatch.setattr(runner, "collect_shortcuts", fake_collectors["shortcuts_fn"])
    runner._current = None

    job = runner.start_scan()
    for _ in range(200):
        if job.state not in ("pending", "running"):
            break
        time.sleep(0.05)

    assert job.state == "failed"
    assert "清单导出失败" in (job.error or ""), job.error
    assert not paths.latest_snapshot().exists(), "失败却写了快照"
    log = paths.logs_dir() / "scan.log"
    assert log.is_file(), "失败必须落盘，否则排障无从下手"


# -------------------------------------------------------- 进度估算自校准
def test_dir_count_estimate_uses_previous_scan(fake_collectors):
    """进度按上次的目录数估。写死常数会让进度条卡住（P2 实测卡在 68%）。"""
    assert runner.estimate_dir_count() == runner.FALLBACK_DIR_COUNT, \
        "无历史快照时应退回兜底值"

    snapshots.save_latest([], scan_stats={"total_dirs": 222333})
    assert runner.estimate_dir_count() == 222333

    # 明显不合理的历史值不可采信，否则进度会瞬间撞顶
    snapshots.save_latest([], scan_stats={"total_dirs": 3})
    assert runner.estimate_dir_count() == runner.FALLBACK_DIR_COUNT


def test_scan_root_is_not_caller_controlled(monkeypatch):
    """扫描根不接受调用方传入，但跟着系统盘走，不是字面 C。

    **不能再断言 `SCAN_ROOT == "C:\\"` 了**：那等于说"本机系统盘是 C 所以这样写
    是对的"。而且常量改函数后那条断言的两边在 import 时就已算定，系统盘恰好是 C 的
    机器上它永远绿——本项目明令禁止的"指标取决于跑测试的人是谁"。这里 monkeypatch
    环境变量，两条都验：注入面仍被堵死，盘符跟随系统。
    """
    import inspect

    sig = inspect.signature(runner.run_pipeline)
    assert "root" not in sig.parameters
    assert not inspect.signature(runner.scan_root).parameters

    monkeypatch.setenv("SystemDrive", "D:")
    assert runner.scan_root() == "D:\\"
