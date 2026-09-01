r"""扫描任务编排：采集 → 归因 → 存快照。P2 的核心。

在此之前引擎只会读 `%LOCALAPPDATA%` 里的快照，而快照只能靠开发者手工跑脚本产出，
所以别人装上后痕迹永远是空的（或是开发机的旧数据）。这个模块把 P1 搬进来的三件套
串成一条能被 HTTP 触发的管道，是发布阻塞的最后一环。

五个阶段（耗时按本机实测标注）：
  1. scan     全盘目录体积       ~18s   最慢，进度回报主要来自这里
  2. inventory 注册表/服务/Appx   ~5s    PowerShell 子进程
  3. shortcuts 快捷方式目标 exe   ~1s
  4. attribute 归因               <1s
  5. save     写快照（原子）      <1s

设计约束：
- **扫描根不接受调用方传入**，由 `sysdirs.system_drive_root()` 从环境变量算出。
  参数化会引入路径注入面（UNC 路径能把扫描指到网络共享），所以调用方无从干预；但
  **也不能写死 `C:\\`**——Windows 不一定装在 C，写死就在系统装 D 盘的机器上扫了数据盘。
  两者不冲突：`sysdirs` 只读环境变量且用 `[A-Za-z]:` 严格校验，UNC 一律进不来。
- **全程只读**，唯一写动作是最后 `snapshots.save_latest()` 的原子写入，且写之前
  先 `archive_latest()` 留底。符合"默认只读 + 写操作要有回滚记录"的红线。
- **同时只允许一个任务**：扫描吃满一个核且要读整块磁盘，并发跑两个只会互相拖慢，
  还会两份结果抢着写同一个快照文件。
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

from .. import sysdirs, winproc
from ..attribute import attribute_footprint
from ..storage import paths, snapshots
from .collect_evidence import collect_shortcuts
from .inventory_export import export_inventory
from .scan_dirs import ScanCancelled, scan_tree


def scan_root() -> str:
    r"""要扫的盘根，即系统盘。

    **刻意是函数而不是模块常量。** 常量在 import 时求值，之后环境再变也不会跟着改；
    更要紧的是那样写没法测——本机系统盘恰好是 C，`SCAN_ROOT == system_drive_root()`
    两边都是 `C:\`，断言永远绿，等于拿开发机当验收标准。
    """
    return sysdirs.system_drive_root()

# 各阶段占总进度的权重，按实测耗时分配。全盘扫描占大头，所以进度条不会在
# 前 90% 卡着不动——UI 上"看起来假"的进度条比没有进度条更糟。
PHASES = [
    ("scan", "扫描系统盘目录体积", 70),
    ("inventory", "读取软件台账（注册表 / 服务 / 商店应用）", 15),
    ("shortcuts", "采集快捷方式目标", 4),
    ("attribute", "归因：把足迹判给具体软件", 6),
    ("save", "写入快照", 5),
]
PHASE_LABEL = {k: label for k, label, _ in PHASES}


class ScanJob:
    """一次扫描任务的状态。线程安全：状态读写都过锁。"""

    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.state = "pending"      # pending/running/done/failed/cancelled
        self.phase = None
        self.phase_label = None
        self.percent = 0
        self.detail = ""
        self.started_at = time.time()
        self.finished_at = None
        self.error = None
        self.result = None          # 完成后的摘要
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._seq = 0               # 事件序号，供 SSE 判断有无新进展

    # ---------------------------------------------------------- 状态更新
    def _set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self._seq += 1

    def enter_phase(self, key, base_percent):
        self._set(phase=key, phase_label=PHASE_LABEL[key],
                  percent=base_percent, detail="")

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def snapshot(self):
        """给 HTTP 层用的状态快照（含序号，SSE 据此去重）。

        cancel_requested 与 state 是两件事：取消请求只是置了标志位，真正翻成
        cancelled 要等工作线程跑到下一个检查点。少了这个字段，用户点完取消会
        看到界面仍显示"扫描中"，像是没生效。
        """
        with self._lock:
            return {
                "job_id": self.id,
                "state": self.state,
                "cancel_requested": self._cancel.is_set(),
                "phase": self.phase,
                "phase_label": self.phase_label,
                "percent": self.percent,
                "detail": self.detail,
                "elapsed_sec": round((self.finished_at or time.time())
                                     - self.started_at, 1),
                "error": self.error,
                "result": self.result,
                "seq": self._seq,
            }


# ------------------------------------------------------------ 单例任务注册表
_current: ScanJob | None = None
_registry_lock = threading.Lock()


def current_job() -> ScanJob | None:
    with _registry_lock:
        return _current


def get_job(job_id: str) -> ScanJob | None:
    with _registry_lock:
        if _current and _current.id == job_id:
            return _current
    return None


class ScanAlreadyRunning(RuntimeError):
    def __init__(self, job: ScanJob):
        super().__init__("已有扫描任务在跑")
        self.job = job


# 首次扫描时的目录数兜底值。取自本机实测 15.5 万的量级——写小了进度条会提前
# 撞顶后长时间不动，写大了则一直偏慢，偏慢比撞顶好看。
FALLBACK_DIR_COUNT = 150000


def estimate_dir_count() -> int:
    """按上次扫描的目录数估这次的量。没有历史就用兜底值。"""
    try:
        _items, meta = snapshots.load_latest()
    except Exception:
        return FALLBACK_DIR_COUNT
    stats = (meta or {}).get("scan_stats") or {}
    n = stats.get("total_dirs")
    return int(n) if isinstance(n, (int, float)) and n > 1000 else FALLBACK_DIR_COUNT


def start_scan(on_done=None) -> ScanJob:
    """起一个后台扫描任务，立刻返回 job。已有任务在跑则抛 ScanAlreadyRunning。

    on_done(job) 在任务结束后（无论成败）调用，供调用方刷新缓存数据。
    """
    global _current
    with _registry_lock:
        if _current and _current.state in ("pending", "running"):
            raise ScanAlreadyRunning(_current)
        job = ScanJob()
        _current = job

    t = threading.Thread(target=_run, args=(job, on_done),
                         name=f"lostpath-scan-{job.id}", daemon=True)
    t.start()
    return job


def _run(job: ScanJob, on_done=None):
    try:
        result = run_pipeline(job)
    except ScanCancelled:
        job._set(state="cancelled", finished_at=time.time(),
                 detail="已取消，快照未改动")
    except Exception as e:
        # 后台线程里 try/except-pass 会把 NameError 吞成"功能没效果"（M3 图标
        # 全丢就是这么藏住的），所以这里既记类型与消息，也把 traceback 落盘。
        job._set(state="failed", finished_at=time.time(),
                 error=f"{type(e).__name__}: {e}")
        _log_failure(job, e)
    else:
        # 重建缓存必须发生在翻成 done 之前。前端见到 done 就会去拉 /api/data，
        # 若那时还在重建，拿到的是扫描前的旧数据——实测表现为"扫完了，但占用
        # 大户是空的、实体数没变"。done 必须意味着全部就绪。
        if on_done:
            job._set(phase="refresh", phase_label="整理台账", percent=99,
                     detail="重新聚合软件实体与痕迹挂接")
            try:
                on_done(job)
            except Exception as e:
                _log_failure(job, e)
        job._set(state="done", percent=100, finished_at=time.time(),
                 result=result, detail="完成")


def _log_failure(job, exc):
    try:
        d = paths.logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(d / "scan.log", "a", encoding="utf-8") as f:
            f.write(f"\n===== {stamp} job={job.id} =====\n")
            f.write(f"{type(exc).__name__}: {exc}\n")
            f.write(traceback.format_exc())
    except OSError:
        pass


def run_pipeline(job: ScanJob, scan_fn=None, inventory_fn=None,
                 shortcuts_fn=None, user_home=None, program_data=None) -> dict:
    """同步跑完五个阶段，返回摘要。可被测试直接调用，不必起线程。

    三个采集器与 user_home / program_data 可注入，默认全用真实实现 / 真实位置。
    这不是为了扩展性——是为了能测：真扫一次要十几秒且结果随机器变化，而管道自身的
    逻辑（阶段推进、取消检查点、写快照前先归档）本来就不该靠全盘扫描来验。
    注入范围**刻意不含扫描根**，那是机器的事实而非调用方的选择（见模块 docstring）。
    """
    scan_fn = scan_fn or scan_tree
    inventory_fn = inventory_fn or export_inventory
    shortcuts_fn = shortcuts_fn or collect_shortcuts
    job._set(state="running")
    base = {}
    acc = 0
    for key, _label, weight in PHASES:
        base[key] = acc
        acc += weight

    def check():
        if job.cancelled:
            raise ScanCancelled()

    # ---- 1. 全盘目录体积 ----
    job.enter_phase("scan", base["scan"])
    scan_weight = dict((k, w) for k, _l, w in PHASES)["scan"]
    expected_dirs = estimate_dir_count()

    def on_dirs(dirs_done, path):
        # 目录总数事先不知道（要知道就得先扫一遍），所以按上次扫描的目录数估。
        # 首次扫描退回常数。第一版写死 6 万，实测本机 15.5 万，进度条在 68% 上
        # 卡了大半个阶段——这正是"进度条看起来假比没有更糟"的那种情形。
        frac = min(0.98, dirs_done / float(expected_dirs))
        job._set(percent=int(base["scan"] + scan_weight * frac),
                 detail=f"{dirs_done} 个目录 · {path[:80]}")

    report = scan_fn(scan_root(), progress=on_dirs,
                     should_cancel=lambda: job.cancelled)
    check()

    # ---- 2. 软件台账清单 ----
    job.enter_phase("inventory", base["inventory"])
    inventory = inventory_fn()
    check()

    # ---- 3. 快捷方式 ----
    job.enter_phase("shortcuts", base["shortcuts"])
    shortcuts = shortcuts_fn()
    check()

    # ---- 4. 归因 ----
    job.enter_phase("attribute", base["attribute"])
    records, stats = attribute_footprint(report, inventory, shortcuts,
                                         user_home=user_home,
                                         program_data=program_data)
    check()

    # ---- 5. 存快照（唯一写动作）----
    job.enter_phase("save", base["save"])
    archived = snapshots.archive_latest()   # 覆盖前留底
    scan_stats = {
        "total_dirs": report["total_dirs"],
        "total_files": report["total_files"],
        "total_bytes": report["total_bytes"],
        "elapsed_sec": report["elapsed_sec"],
        "denied_count": report["denied_count"],
        # 被拒的路径要带上，不能只留个条数。扫描器本来就收集了（denied_sample），
        # 此前在这里被丢掉，于是界面只能说"96 个目录拒绝访问"——用户既不知道漏了
        # 什么，也无从判断要不要提权重扫。盲区必须具体到路径才算真的明示。
        "denied_sample": report.get("denied_sample") or [],
        "reparse_count": report["reparse_count"],
        "hardlink_dedup_bytes": report["hardlink_dedup_bytes"],
        # **扫描当时有没有管理员权限。**
        #
        # 少了这一条，界面判断不出"当前快照该不该重扫"：`denied_count > 0` 不能当
        # 判据——**以管理员扫完照样有读不到的目录**（系统保护目录、正被独占打开的
        # 文件，实测提权后仍有 17 个）。用 denied>0 判的话会永远提示"待重扫"，
        # 用户扫多少次都甩不掉那句提示。
        #
        # 有了它，判据变成"进程现在提权了、而快照是非提权时扫的"——那才是真的该重扫。
        "elevated": winproc.is_elevated(),
    }
    target = snapshots.save_latest(records, scan_stats=scan_stats)

    return {
        "entries": stats["entries"],
        "total_size": stats["total_size"],
        "unknown_size": stats["unknown_size"],
        "scanned_files": report["total_files"],
        "scanned_dirs": report["total_dirs"],
        "denied_count": report["denied_count"],
        "reparse_count": report["reparse_count"],
        "registry_apps": len(inventory.get("apps") or []),
        "appx": len(inventory.get("appx") or []),
        "shortcuts": len(shortcuts),
        "scan_elapsed_sec": report["elapsed_sec"],
        "snapshot_path": str(target),
        "archived_previous": str(archived) if archived else None,
        # 拒绝访问的目录是非管理员盲区（实测约 17 GiB / 95 个目录），得让 UI
        # 能明示，不然用户会以为工具算错了。
        "evidence_hits": stats["evidence_hits"],
        "index_warnings": stats["index_warnings"],
    }
