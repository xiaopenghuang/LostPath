"""LostPath M2 本地服务：以软件台账为中心，只读。

数据模型（DESIGN.md 2026-08-28 修订）：台账实体（注册表/Appx/便携）为主，
v4 的 C 盘归因结果作为"痕迹"挂接到实体。启动：conda run -n lostpath python engine/main.py
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 源码运行时 ROOT 是仓库根；PyInstaller 打包后 __file__ 指向临时解包目录，
# 仓库结构不存在，资源统一在 sys._MEIPASS 下。两种形态都要能找到 ui/dist。
FROZEN = getattr(sys, "frozen", False)
ROOT = Path(getattr(sys, "_MEIPASS", "")) if FROZEN else Path(__file__).resolve().parent.parent
if not FROZEN and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import inventory  # noqa: E402
from lostpath.act import (context_menu, environment, executor, manifest, planner,
                          registry_health, rules, uninstall_audit, uninstaller)  # noqa: E402
from lostpath.act import target_root as target_root_mod  # noqa: E402
from lostpath.scan import runner  # noqa: E402
from lostpath import (inspection, integration_reports, parent_watch, residues,
                      startup, sysdirs)  # noqa: E402
from lostpath.storage import paths as lp_paths, snapshots  # noqa: E402

UI_DIST = ROOT / "ui" / "dist"


def build_data() -> dict:
    entities, stats = inventory.build_entities()
    # 缺快照是正常状态（首次启动/换机器），此时台账仍可用，只是没有 C 盘痕迹
    trace_items, snap_meta = snapshots.load_latest()
    trace_items.sort(key=lambda t: t.get("size") or 0, reverse=True)
    _, unlinked = inventory.link_traces(entities, trace_items)
    # 工具链/未注册应用等归因成功但注册表无条目的 owner，补成实体（见 synth_entities）
    synthetic, unlinked = inventory.synth_entities(unlinked)
    entities.extend(synthetic)
    # 台账排序：痕迹占用优先，其次登记大小
    entities.sort(key=lambda x: (-(x.get("traces_size") or 0),
                                 -(x.get("estimated_size") or 0),
                                 x["name"].lower()))
    return {
        "built_from": "软件台账（注册表+Appx+便携）× 系统盘痕迹快照",
        "system_drive": sysdirs.system_drive(),
        "items": trace_items,      # C 盘全景页数据源（不变）
        "software": entities,      # 台账实体（含已挂接 traces）
        "unlinked_traces": unlinked,
        "snapshot": snap_meta,     # present=False 时 UI 走"尚未扫描"引导
        "summary": {
            "entries": len(trace_items),
            "total_size": sum(t.get("size") or 0 for t in trace_items),
            "unknown_size": sum(t.get("size") or 0 for t in trace_items if not t.get("owner")),
            "entities": len(entities),
            "located": stats["located"],
            "portable": stats["portable"],
            "registry_raw": stats["registry_raw"],
            "components": stats["components"],
            "unlinked_size": sum(t.get("size") or 0 for t in unlinked),
            "linked_entities": sum(1 for x in entities if x.get("traces")),
            "synthetic_entities": len(synthetic),
            "synthetic_size": sum(x.get("traces_size") or 0 for x in synthetic),
        },
    }


def _auto_purge_expired() -> None:
    """启动时清掉真正过期的回收区数据。

    为什么放在启动而不是后台定时器：这个服务的生命周期就是"用户开着应用的时候"，
    定时器多出一条线程却换不来更及时——真正的过期判定按天计，开一次应用清一次足够。

    这是**自动执行的不可逆删除**，所以必须留审计痕迹：清了哪些操作记进日志，且
    manifest 上留 purged_at（界面据此拒绝对它回滚，并显示"已永久删除"）。
    只清 is_expired 为真的，回收期内的一个都不动。

    刻意不让失败冒泡：回收区里某个目录被占用而删不掉，不该让整个引擎起不来。
    """
    try:
        res = executor.purge_expired()
    except Exception:
        import traceback
        try:
            lp_paths.logs_dir().mkdir(parents=True, exist_ok=True)
            (lp_paths.logs_dir() / "purge.log").open("a", encoding="utf-8").write(
                f"\n===== {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
                f"自动清理异常 =====\n" + traceback.format_exc())
        except OSError:
            pass
        return
    if not res["purged"] and not res["skipped"]:
        return
    try:
        lp_paths.logs_dir().mkdir(parents=True, exist_ok=True)
        with (lp_paths.logs_dir() / "purge.log").open("a", encoding="utf-8") as f:
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            f.write(f"\n===== {stamp} 启动时自动清理 =====\n")
            for op_id in res["purged"]:
                f.write(f"已永久删除（超过 {manifest.RECOVERABLE_DAYS} 天）：{op_id}\n")
            for s in res["skipped"]:
                f.write(f"跳过 {s['id']}：{s['reason']}\n")
    except OSError:
        pass


# 源码桌面壳经 conda.bat 启动时，中间有 cmd/conda 两层。终端被强制关闭后这些
# 包装进程可能先死，Python 被重新挂到别的父进程并继续占着 8321。壳传入自己的
# PID 后由引擎兜底观察，壳消失就退出；直接运行引擎时没有该变量，行为不变。
parent_watch.start_from_environment()
DATA = build_data()
_auto_purge_expired()
app = FastAPI(title="LostPath M2")


@app.middleware("http")
async def reject_cross_origin_requests(request, call_next):
    """Reject browser requests whose Origin is not the local application.

    Requests from the Electron ``file://`` page and non-browser local clients do
    not carry a useful Origin header, so those remain supported. A loopback
    Origin is sufficient for the embedded UI and the Vite development server;
    arbitrary websites must not be able to trigger a write endpoint by CSRF.
    """
    origin = request.headers.get("origin")
    if origin and origin.lower() != "null":
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in {
            "127.0.0.1", "localhost", "::1",
        }:
            return JSONResponse(
                {"detail": "只允许来自本机 LostPath 界面的请求"}, status_code=403)
    return await call_next(request)

# 图标目录：后台线程补齐缺失图标（PowerShell ExtractAssociatedIcon，不阻塞启动）
lp_paths.ensure_dirs()
ICONS_DIR = lp_paths.icons_dir()


def _extract_icons_async() -> None:
    import threading

    def _run():
        try:
            import extract_icons

            jobs = extract_icons.missing_jobs(DATA.get("software", []))
            if jobs:
                extract_icons.run_extraction(jobs)
        except Exception:
            # 图标是装饰性的，失败不该影响服务；但静默会让排障无从下手
            # （M3 那次图标全丢就是被这个 pass 藏住的），故落盘到用户日志目录。
            import traceback

            try:
                lp_paths.logs_dir().mkdir(parents=True, exist_ok=True)
                (lp_paths.logs_dir() / "icons.log").write_text(
                    traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass

    threading.Thread(target=_run, daemon=True).start()


_extract_icons_async()

from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/icons", StaticFiles(directory=ICONS_DIR), name="icons")


@app.get("/api/drives")
def api_drives():
    """本地固定磁盘的容量信息（Dashboard 存储拓扑用）。只读。"""
    import ctypes

    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if not (bitmask >> i) & 1:
            continue
        letter = chr(ord("A") + i) + ":"
        if letter in ("A:", "B:"):
            continue
        if ctypes.windll.kernel32.GetDriveTypeW(letter + "\\") != 3:  # DRIVE_FIXED
            continue
        total = ctypes.c_ulonglong()
        free = ctypes.c_ulonglong()
        if ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            letter + "\\", None, ctypes.byref(total), ctypes.byref(free)
        ):
            drives.append({"letter": letter, "total": total.value, "free": free.value})
    return drives


@app.get("/api/data")
def api_data():
    return DATA


@app.get("/api/software/{entity_id}/integrations")
def api_software_integrations(entity_id: str):
    """汇总某个软件在环境变量、Uninstall 登记和启动链路中的关系。"""
    entities = DATA.get("software", [])
    entity = next((item for item in entities if item.get("id") == entity_id), None)
    if not entity:
        return JSONResponse(status_code=404, content={"detail": "软件实体不存在"})

    environment_report, registry_report, context_menu_report = integration_reports.get(
        entities, environment.report, registry_health.report, context_menu.report)

    environment_items = []
    for item in environment_report.get("items", []):
        relation = next((row for row in item.get("relations", [])
                         if row.get("entity_id") == entity_id), None)
        if relation:
            environment_items.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "scope": item.get("scope"),
                "masked": item.get("masked"),
                "reason": relation.get("reason"),
                "confidence": relation.get("confidence"),
            })

    registry_items = []
    for item in registry_report.get("items", []):
        relation = item.get("entity") or {}
        if relation.get("entity_id") != entity_id:
            continue
        registry_items.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "hive": item.get("hive"),
            "scope": item.get("scope"),
            "registry_path": item.get("registry_path"),
            "status": item.get("status"),
            "reason": relation.get("reason"),
            "confidence": relation.get("confidence"),
        })

    startup_report = startup.report(entities)
    startup_items = [{
        "id": item.get("id"),
        "name": item.get("name"),
        "kind": item.get("kind"),
        "source": item.get("source"),
        "risk": item.get("risk"),
        "manage": item.get("manage"),
        "reason": item.get("owner_reason"),
        "confidence": item.get("owner_confidence"),
    } for item in startup_report.get("items", [])
        if item.get("owner_id") == entity_id]

    context_menu_items = []
    for item in context_menu_report.get("items", []):
        relation = item.get("entity") or {}
        if relation.get("entity_id") != entity_id:
            continue
        context_menu_items.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "kind": item.get("kind"),
            "scope": item.get("scope"),
            "surfaces": item.get("surfaces"),
            "manage": item.get("manage"),
            "reason": relation.get("reason"),
            "confidence": relation.get("confidence"),
        })

    return {
        "entity": {
            "entity_id": entity.get("id"),
            "name": entity.get("name"),
            "publisher": entity.get("publisher"),
            "icon": entity.get("icon"),
            "reason": "当前软件台账实体",
            "confidence": 1.0,
        },
        "environment": environment_items,
        "registry": registry_items,
        "startup": startup_items,
        "context_menu": context_menu_items,
        "startup_state": startup_report.get("state"),
        "summary": {
            "environment": len(environment_items),
            "registry": len(registry_items),
            "startup": len(startup_items),
            "context_menu": len(context_menu_items),
            "total": (len(environment_items) + len(registry_items) + len(startup_items)
                      + len(context_menu_items)),
        },
    }


@app.get("/api/history")
def api_history(limit: int = 12):
    """扫描历史与最近一次目录变化。历史文件损坏时由存储层跳过，不影响当前数据。"""
    return snapshots.history_report(limit)


@app.get("/api/residues")
def api_residues():
    """疑似卸载残留或未登记应用。只返回仍存在且达到体积门槛的应用类目录。"""
    return residues.detect(DATA)


@app.get("/api/startup")
def api_startup():
    """登录启动项、自动服务和计划任务的只读分析。"""
    return startup.report(DATA.get("software", []))


@app.post("/api/startup/refresh")
def api_startup_refresh():
    """重新采集系统集成点。采集在后台进行，不阻塞其它接口。"""
    return startup.request_scan(DATA.get("software", []), force=True)


class StartupActionReq(BaseModel):
    item_id: str
    dry_run: bool = True


@app.post("/api/startup/disable")
def api_startup_disable(req: StartupActionReq):
    """禁用当前用户登录启动项。默认 dry-run，必须显式确认才修改注册表。"""
    try:
        return startup.disable(req.item_id, dry_run=req.dry_run)
    except startup.StartupActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


class StartupRestoreReq(BaseModel):
    operation_id: str


@app.post("/api/startup/restore")
def api_startup_restore(req: StartupRestoreReq):
    """恢复一条由 LostPath 禁用的当前用户登录启动项。"""
    try:
        return startup.restore(req.operation_id)
    except startup.StartupActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


# -------------------------------------------------------- 环境变量管理
@app.get("/api/environment")
def api_environment():
    return environment.report(DATA.get("software", []))


class EnvironmentSetReq(BaseModel):
    name: str
    value: str
    expected_fingerprint: str | None = None
    dry_run: bool = True


@app.post("/api/environment/set")
def api_environment_set(req: EnvironmentSetReq):
    try:
        result = environment.set_value(
            req.name, req.value, req.expected_fingerprint, dry_run=req.dry_run)
        if not req.dry_run:
            integration_reports.invalidate()
        return result
    except environment.EnvironmentActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


class EnvironmentDeleteReq(BaseModel):
    name: str
    expected_fingerprint: str
    dry_run: bool = True


@app.post("/api/environment/delete")
def api_environment_delete(req: EnvironmentDeleteReq):
    try:
        result = environment.delete_value(
            req.name, req.expected_fingerprint, dry_run=req.dry_run)
        if not req.dry_run:
            integration_reports.invalidate()
        return result
    except environment.EnvironmentActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


class OperationIdReq(BaseModel):
    operation_id: str


@app.post("/api/environment/restore")
def api_environment_restore(req: OperationIdReq):
    try:
        result = environment.restore(req.operation_id)
        integration_reports.invalidate()
        return result
    except environment.EnvironmentActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


# -------------------------------------------------------- 注册表巡检
@app.get("/api/registry-health")
def api_registry_health():
    return registry_health.report(DATA.get("software", []))


class RegistryCleanupReq(BaseModel):
    item_id: str
    dry_run: bool = True


@app.post("/api/registry-health/cleanup")
def api_registry_cleanup(req: RegistryCleanupReq):
    try:
        result = registry_health.cleanup(req.item_id, dry_run=req.dry_run)
        if not req.dry_run:
            integration_reports.invalidate()
        return result
    except registry_health.RegistryActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


@app.post("/api/registry-health/restore")
def api_registry_restore(req: OperationIdReq):
    try:
        result = registry_health.restore(req.operation_id)
        integration_reports.invalidate()
        return result
    except registry_health.RegistryActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


# -------------------------------------------------------- 右键菜单管理
@app.get("/api/context-menu")
def api_context_menu(refresh: bool = False):
    result = context_menu.report(DATA.get("software", []), force=refresh)
    if refresh:
        integration_reports.invalidate()
    return result


class ContextMenuActionReq(BaseModel):
    item_id: str
    dry_run: bool = True


class ContextMenuCreateReq(BaseModel):
    name: str
    executable: str
    surfaces: list[str]
    dry_run: bool = True


@app.post("/api/context-menu/create")
def api_context_menu_create(req: ContextMenuCreateReq):
    try:
        result = context_menu.create_custom(
            req.name, req.executable, req.surfaces, dry_run=req.dry_run)
        if not req.dry_run:
            integration_reports.invalidate()
        return result
    except context_menu.ContextMenuActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


@app.post("/api/context-menu/disable")
def api_context_menu_disable(req: ContextMenuActionReq):
    try:
        result = context_menu.disable(req.item_id, dry_run=req.dry_run)
        if not req.dry_run:
            integration_reports.invalidate()
        return result
    except context_menu.ContextMenuActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


@app.post("/api/context-menu/delete")
def api_context_menu_delete(req: ContextMenuActionReq):
    try:
        result = context_menu.remove_custom(req.item_id, dry_run=req.dry_run)
        if not req.dry_run:
            integration_reports.invalidate()
        return result
    except context_menu.ContextMenuActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


@app.post("/api/context-menu/restore")
def api_context_menu_restore(req: OperationIdReq):
    try:
        result = context_menu.restore(req.operation_id)
        integration_reports.invalidate()
        return result
    except context_menu.ContextMenuActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


# -------------------------------------------------------- 软件卸载
@app.get("/api/uninstall")
def api_uninstall():
    return uninstaller.report(DATA.get("software", []))


class UninstallLaunchReq(BaseModel):
    item_id: str
    dry_run: bool = True


@app.post("/api/uninstall/launch")
def api_uninstall_launch(req: UninstallLaunchReq):
    try:
        return uninstaller.launch(
            req.item_id, dry_run=req.dry_run, entities=DATA.get("software", []))
    except uninstaller.UninstallActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


@app.post("/api/uninstall/verify")
def api_uninstall_verify(req: OperationIdReq):
    try:
        result = uninstaller.verify(req.operation_id)
    except uninstaller.UninstallActionError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})
    global DATA
    DATA = build_data()
    audit = None
    audit_error = None
    if result.get("verified_removed"):
        try:
            audit = uninstall_audit.build_audit(req.operation_id, DATA)
        except uninstall_audit.UninstallAuditError as exc:
            audit_error = str(exc)
    return {
        "result": result,
        "catalog": uninstaller.report(DATA.get("software", [])),
        "residues": residues.detect(DATA),
        "audit": audit,
        "audit_error": audit_error,
    }


@app.get("/api/uninstall/audit/{operation_id}")
def api_uninstall_audit(operation_id: str):
    try:
        return uninstall_audit.build_audit(operation_id, DATA)
    except uninstall_audit.UninstallAuditError as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})


class UninstallDeepCleanupReq(BaseModel):
    operation_id: str
    candidate_ids: list[str]
    dry_run: bool = True


@app.post("/api/uninstall/deep-clean")
def api_uninstall_deep_cleanup(req: UninstallDeepCleanupReq):
    global DATA
    try:
        result = uninstall_audit.execute_cleanup(
            req.operation_id, req.candidate_ids, DATA, dry_run=req.dry_run)
    except (uninstall_audit.UninstallAuditError, executor.ExecutionRefused,
            executor.ExecutionFailed, environment.EnvironmentActionError,
            registry_health.RegistryActionError, startup.StartupActionError) as exc:
        return JSONResponse(status_code=409, content={"refused": str(exc)})
    if not req.dry_run:
        DATA = build_data()
        result["audit"] = uninstall_audit.build_audit(req.operation_id, DATA)
    return result


@app.get("/api/inspection")
def api_inspection():
    """自动巡检配置与是否到期。"""
    return inspection.status()


class InspectionReq(BaseModel):
    enabled: bool
    interval_hours: int


@app.put("/api/inspection")
def api_set_inspection(req: InspectionReq):
    try:
        inspection.save_config(req.enabled, req.interval_hours)
        return inspection.status()
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


# ---------------------------------------------------------------- 深度扫描
# 这三个端点是 P2：在此之前引擎只会读快照，而快照只能靠手工跑脚本产出，别人装上
# 后痕迹永远是空的。扫描全程只读，唯一写动作是最后原子写快照，且覆盖前先归档留底。
#
# 服务仍只绑 127.0.0.1，扫描根不接受入参、由 sysdirs 从环境变量取出并用
# [A-Za-z]: 严格校验（UNC 进不来），没有路径注入面。中间件还会拒绝非回环 Origin，
# 防止外部网页借浏览器会话触发写端点。无 Origin 的本地 CLI 仍可调用；若以后监听
# 非回环地址，必须再加真正的请求令牌鉴权。


def _rebuild_after_scan(job) -> None:
    """扫描成功后重建缓存。失败要留痕，不能静默——否则表现为"扫完没变化"。

    只在成功路径被调用，且调用时任务仍是 running（见 runner._run 的说明：done
    必须意味着数据已就绪）。所以这里不要再判 job.state == "done"。
    """
    global DATA
    try:
        DATA = build_data()
    except Exception:
        import traceback
        try:
            lp_paths.logs_dir().mkdir(parents=True, exist_ok=True)
            (lp_paths.logs_dir() / "scan.log").open("a", encoding="utf-8").write(
                "\n===== rebuild after scan failed =====\n"
                + traceback.format_exc())
        except OSError:
            pass


def _scheduled_scan() -> None:
    """自动巡检复用同一个扫描单例，不会与用户手动扫描并发。"""
    try:
        runner.start_scan(on_done=_rebuild_after_scan)
    except runner.ScanAlreadyRunning:
        return


@app.post("/api/scan")
def api_scan_start():
    """起一次深度扫描。已有任务在跑时返回 409 并带上那个任务的状态。"""
    try:
        job = runner.start_scan(on_done=_rebuild_after_scan)
    except runner.ScanAlreadyRunning as e:
        # 注意 snapshot() 自带 error 键（值为 None，指任务本身没出错），所以
        # 冲突提示不能也叫 error——展开在后会把它覆盖成 null。这里用 conflict。
        return JSONResponse(status_code=409,
                            content={"conflict": "已有扫描任务在进行中",
                                     **e.job.snapshot()})
    return job.snapshot()


@app.get("/api/scan/status")
def api_scan_status():
    """轮询兜底：SSE 断了或代理不支持时用这个。"""
    job = runner.current_job()
    if not job:
        return {"state": "idle"}
    return job.snapshot()


@app.post("/api/scan/cancel")
def api_scan_cancel():
    job = runner.current_job()
    if not job or job.state not in ("pending", "running"):
        return JSONResponse(status_code=409,
                            content={"conflict": "当前没有正在跑的扫描任务"})
    job.cancel()
    return job.snapshot()


@app.get("/api/scan/events")
async def api_scan_events():
    """SSE 推进度。只在状态序号变化时发，避免每秒刷同一帧。"""
    import asyncio
    import json as _json

    async def gen():
        last_seq = -1
        idle_ticks = 0
        while True:
            job = runner.current_job()
            if not job:
                # 没有任务就别把连接吊死：告知 idle 后结束，由前端决定要不要重连
                yield f"data: {_json.dumps({'state': 'idle'})}\n\n"
                return
            snap = job.snapshot()
            if snap["seq"] != last_seq:
                last_seq = snap["seq"]
                idle_ticks = 0
                yield f"data: {_json.dumps(snap, ensure_ascii=False)}\n\n"
            else:
                idle_ticks += 1
                if idle_ticks % 20 == 0:
                    # 心跳注释帧：防中间层因空闲掐断连接，前端会忽略它
                    yield ": keepalive\n\n"
            if snap["state"] in ("done", "failed", "cancelled"):
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        # 关掉反向代理缓冲，否则进度会攒着一次性吐出来
        "X-Accel-Buffering": "no",
    })


@app.get("/api/body-tree")
def api_body_tree(path: str):
    return inventory.body_tree(path)


# ---------------------------------------------------------------- 行动计划
# M4 第一步：只读地算出"能做什么、拦阻是什么"。**这里没有任何执行能力**——执行器
# 是独立的下一步，且必须由用户在界面上逐项确认。把"决定"与"动手"分成两个端点，
# 就不会出现"点一下就把文件搬了"的可能。


# ------------------------------------------------------------------ 目标位置
# 默认由 planner 自动挑（非系统盘里剩余最大的那个），用户可以改。**这是本服务唯一
# 一个可写的配置项**——设置页刻意全只读（见 api_settings），而这一项不得不可写：
# 自动挑的盘不一定是用户想放数据的地方，而没有入口时他只能去改环境变量。
#
# 可写的代价用校验来抵：target_root.validate() 拦掉盘符相对路径、UNC、可移动盘、
# 网络盘、不可写位置和 LostPath 自己的数据目录。写盘的内容只有一个字符串，不是
# 任意路径写文件——服务往哪写数据仍然只由 LOSTPATH_DATA_DIR 决定。


def _validated_target_root(raw: str | None) -> tuple[str | None, dict | None]:
    r"""把请求里带来的 target_root 过一遍校验。返回 (可用值, 错误详情)。

    本文件开头那段安全要点写着"绝不接受调用方自带的记录或任意目标目录"，而
    `ExecuteReq.target_root` 从一开始就是调用方自带的任意目录、且零校验就进了
    `os.path.join`。参数保留是因为集成测试靠它指向 tmp_path，但必须先过校验：
    实测 `os.path.join("E:", "x")` 得到 `"E:x"`，落点是进程在 E: 上的当前目录。
    """
    if raw is None or not str(raw).strip():
        return None, None
    res = target_root_mod.validate(raw)
    if not res["ok"]:
        return None, res
    return res["normalized"], None


@app.get("/api/target-root")
def api_target_root():
    """当前生效的迁移目标位置，以及它是自动挑的还是用户设的。"""
    raw = target_root_mod.load_raw()
    checked = target_root_mod.validate(raw) if raw else None
    custom_ok = bool(checked and checked["ok"])
    return {
        "effective": planner.effective_target_root(),
        "auto": planner.default_target_root(),
        "saved": raw,
        "source": "custom" if custom_ok else "auto",
        # 界面在盘符下拉里要把系统盘标出来。不标的话用户看见"C: 剩余 20 GB"很可能就
        # 选了它，然后要等到保存时才被警告——而那时他已经做完了选择。
        "system_drive": sysdirs.system_drive(),
        # 存过但现在校验不过（盘拔了、盘符变了、目录被别的程序删了）。**必须显式
        # 告知**：planner 此时静默回落到自动挑的盘，用户若以为还在用自己设的位置，
        # 就会拿着错误的预期按下执行。
        "saved_invalid": bool(raw) and not custom_ok,
        "errors": (checked or {}).get("errors", []),
        "warnings": (checked or {}).get("warnings", []),
    }


class TargetRootReq(BaseModel):
    # None 或空串 = 清除自定义、回落到自动挑
    path: str | None = None


@app.post("/api/target-root/check")
def api_target_root_check(req: TargetRootReq):
    """只校验不保存，供界面在用户输入时给即时反馈。

    用 POST 而不是 GET：校验里有一次真实的写入探测（在候选路径最近的已存在祖先里
    建一个临时文件再删）。它不留痕迹，但"发请求会往磁盘写点东西"这件事不该藏在
    GET 后面。而探测不能省——权限不足是最常见的失败原因，只看路径形状看不出来。
    """
    return target_root_mod.validate(req.path)


@app.put("/api/target-root")
def api_set_target_root(req: TargetRootReq):
    """保存目标位置。校验不过返回 400 且不落盘。"""
    res = target_root_mod.save(req.path)
    if not res["ok"]:
        return JSONResponse(status_code=400, content=res)
    return {**res,
            "effective": planner.effective_target_root(),
            "auto": planner.default_target_root()}


class OverrideReq(BaseModel):
    # 要覆盖哪个源目录
    source: str
    # None 或空串 = 清掉这一条、回落到全局根
    path: str | None = None


@app.put("/api/target-root/override")
def api_set_override(req: OverrideReq):
    r"""给单个源目录指定专属的目标根。校验不过返回 400 且不落盘。

    **source 必须在当前快照里**，理由与 /api/act/execute 相同：这个值会被存下来、
    之后一路进 os.path.join。不限制的话调用方可以为任意路径预置一条覆盖，等哪天
    那个路径进了快照就按它执行——把"现在无害的写入"变成"以后的任意目标"。
    """
    match = _record_by_path(req.source)
    if not match:
        return JSONResponse(status_code=404, content={
            "error": "该路径不在当前快照里。只能为扫描过的目录设置目标位置"})
    res = target_root_mod.set_override(req.source, req.path)
    if not res["ok"]:
        return JSONResponse(status_code=400, content=res)
    # 回一个"这条现在会搬到哪"，免得界面自己再拼一遍镜像后缀——那等于把
    # planner 的规则复制到前端，两份实现必然漂移。
    record, as_child = match
    plan = planner.plan_for(record, as_child=as_child)
    return {**res, "target": plan.target, "action": plan.action}


@app.get("/api/target-root/overrides")
def api_list_overrides():
    """所有逐项覆盖，以及每条现在是否还有效。

    带上 valid 是因为 planner 对失效的覆盖是**静默回落**的——不显式告知的话，
    用户会以为还在用自己设的位置，然后拿着错误的预期按下执行。
    """
    out = []
    for key, raw in sorted(target_root_mod.load_overrides().items()):
        checked = target_root_mod.validate(raw)
        out.append({"source": key, "root": raw, "valid": checked["ok"],
                    "errors": checked["errors"]})
    return {"overrides": out}


@app.get("/api/plan")
def api_plan(target_root: str | None = None):
    """为当前快照里的每条痕迹出计划。只读，不碰文件。

    不传 target_root 时用用户保存的设置，没设过才自动挑（planner.effective_target_root）。
    传了则**只对本次请求生效**，不写入配置——出计划是只读操作，试算一个位置不该改设置。
    """
    root, bad = _validated_target_root(target_root)
    if bad:
        return JSONResponse(status_code=400, content={
            "error": "指定的目标位置不能用", "detail": bad})
    items, meta = snapshots.load_latest()
    if not meta.get("present"):
        return {"snapshot": meta, "target_root": None, "plans": [],
                "summary": {"total_candidates": 0, "executable": 0,
                            "reclaimable": 0, "by_action": {}, "blocked": 0,
                            "blocker_counts": {}},
                "hint": "尚未扫描本机，先做一次深度扫描才有计划可出"}
    out = planner.plan_all(items, target_root=root)
    out["snapshot"] = meta
    return out


# ------------------------------------------------------------------ 执行
# **安全要点：这些端点只接受路径，记录一律从快照里查。** 绝不接受调用方自带的记录
# 或任意目标目录——否则这个 HTTP 接口就等于"我说删哪个目录就删哪个"，而它无鉴权。
# 路径必须在快照中存在，才可能被执行；执行器内部还会拿磁盘实况重新出计划并二次校验。


def _record_by_path(path: str) -> tuple[dict, bool] | None:
    """返回权威快照记录及其是否为子目录计划输入。"""
    items, meta = snapshots.load_latest()
    if not meta.get("present"):
        return None
    low = (path or "").lower().rstrip("\\")
    for r in items:
        if (r.get("path") or "").lower().rstrip("\\") == low:
            return r, False
        for c in r.get("children") or []:
            if (c.get("path") or "").lower().rstrip("\\") == low:
                return planner.child_record_with_parent_context(c, r), True
    return None


class ExecuteReq(BaseModel):
    path: str
    dry_run: bool = True          # 默认 dry-run：要动手必须显式关掉
    target_root: str | None = None


@app.post("/api/act/execute")
def api_act_execute(req: ExecuteReq):
    # 校验放在查记录之前：这个值会一路进 os.path.join，坏值造成的后果（数据落到
    # 意想不到的地方）比"路径不在快照里"严重得多，该最先挡下。
    root, bad = _validated_target_root(req.target_root)
    if bad:
        return JSONResponse(status_code=400, content={
            "error": "指定的目标位置不能用", "detail": bad})
    match = _record_by_path(req.path)
    if not match:
        return JSONResponse(status_code=404, content={
            "error": "该路径不在当前快照里。只能处理扫描过的目录；"
                     "若刚装了新软件请先重新扫描"})
    record, as_child = match
    try:
        # 按动作分派。target_root 必须一起传进去重算：junction 的容量检查要看目标盘，
        # 不传的话这里算出的动作可能与界面上展示的那条计划不是同一个。
        action = planner.plan_for(
            record, target_root=root, as_child=as_child).action
        if action == "redirect":
            op = executor.execute_redirect(record, target_root=root,
                                           dry_run=req.dry_run,
                                           as_child=as_child)
        elif action == "junction":
            op = executor.execute_junction(record, target_root=root,
                                           dry_run=req.dry_run,
                                           as_child=as_child)
        else:
            op = executor.execute_cleanup(
                record, dry_run=req.dry_run, as_child=as_child)
    except executor.ExecutionRefused as e:
        return JSONResponse(status_code=409, content={"refused": str(e)})
    except executor.ExecutionFailed as e:
        return JSONResponse(status_code=500, content={
            "error": str(e),
            "hint": "操作记录已落盘，可在操作历史里查看做到哪一步、并尝试回滚"})
    global DATA
    if not req.dry_run:
        DATA = build_data()       # 痕迹变了，台账要跟着更新
    return op


class RollbackReq(BaseModel):
    op_id: str


@app.post("/api/act/rollback")
def api_act_rollback(req: RollbackReq):
    try:
        recorded = manifest.find(req.op_id)
        if recorded and recorded.get("action") == "startup_disable":
            op = startup.restore(req.op_id)
        elif recorded and recorded.get("action") in {
            "context_menu_disable", "context_menu_create", "context_menu_delete",
        }:
            op = context_menu.restore(req.op_id)
        elif recorded and recorded.get("action") in {"env_set", "env_delete"}:
            op = environment.restore(req.op_id)
        elif recorded and recorded.get("action") == "registry_cleanup":
            op = registry_health.restore(req.op_id)
        else:
            op = executor.rollback(req.op_id)
    except executor.ExecutionRefused as e:
        return JSONResponse(status_code=409, content={"refused": str(e)})
    except startup.StartupActionError as e:
        return JSONResponse(status_code=409, content={"refused": str(e)})
    except context_menu.ContextMenuActionError as e:
        return JSONResponse(status_code=409, content={"refused": str(e)})
    except environment.EnvironmentActionError as e:
        return JSONResponse(status_code=409, content={"refused": str(e)})
    except registry_health.RegistryActionError as e:
        return JSONResponse(status_code=409, content={"refused": str(e)})
    integration_reports.invalidate()
    global DATA
    DATA = build_data()
    return op


def _operation_recovery_state(op: dict) -> tuple[bool, str]:
    """Return a live, action-specific recovery decision for the history page."""
    action = op.get("action")
    try:
        if action == "startup_disable":
            return startup.recovery_state(op)
        if action in {"env_set", "env_delete"}:
            return environment.recovery_state(op)
        if action == "registry_cleanup":
            return registry_health.recovery_state(op)
        if action in {
            "context_menu_disable", "context_menu_create", "context_menu_delete",
        }:
            return context_menu.recovery_state(op)
        return executor.recovery_state(op)
    except Exception as exc:
        # History must stay readable even when one recovery probe hits a locked key/path.
        return False, f"恢复状态核对失败：{exc}"


@app.get("/api/act/operations")
def api_act_operations():
    """操作历史。用户要能随时看见"我做过什么、还能不能撤"。"""
    ops = []
    recovery_attention = 0
    for recorded in manifest.list_operations():
        can_rollback, recovery_reason = _operation_recovery_state(recorded)
        public = manifest.public_operation(recorded)
        public["can_rollback"] = can_rollback
        public["recovery_reason"] = recovery_reason
        if recorded.get("status") in {"planned", "failed"} and can_rollback:
            recovery_attention += 1
        ops.append(public)
    entries = manifest.recycle_entries()
    return {
        "operations": ops,
        "summary": {
            "total": len(ops),
            "rollbackable": sum(bool(op["can_rollback"]) for op in ops),
            "recovery_attention": recovery_attention,
            "recycle_bytes": sum(e["size"] for e in entries),
        },
    }


@app.get("/api/act/recycle")
def api_act_recycle():
    """回收区清单。

    这个接口存在的理由：用户点了"清理缓存"、界面说腾出 2.23 GiB，但 C 盘可用空间
    一点没变——数据还在回收区。不给他看见里面有什么、也不给腾空的入口，那个"已腾出"
    就是句空话。
    """
    entries = manifest.recycle_entries()
    total = sum(e["size"] for e in entries)
    expired = [e for e in entries if e["expired"]]
    return {
        "entries": entries,
        "summary": {
            "count": len(entries),
            "total_size": total,
            "expired_count": len(expired),
            "expired_size": sum(e["size"] for e in expired),
            "recoverable_days": manifest.RECOVERABLE_DAYS,
            "recycle_root": str(manifest.recycle_dir()),
        },
    }


class PurgeReq(BaseModel):
    """force_ids 为空时只清过期项；点名 id 才立刻永久删除。"""
    force_ids: list[str] | None = None


@app.post("/api/act/purge")
def api_act_purge(req: PurgeReq):
    return executor.purge_expired(force_ids=req.force_ids)


def _is_elevated() -> bool:
    """当前进程是否提权。**实现在 `lostpath/winproc.py`，这里只是转发。**

    扫描管道要把同一件事写进快照信封（`scan_stats.elevated`），两处各写一份实现
    早晚走偏——本项目已有过"同一个概念两处实现只修了一处"的事故（见 HANDOVER
    §7 第 13 条的执行器安全闸）。
    """
    from lostpath import winproc
    return winproc.is_elevated()


@app.get("/api/settings")
def api_settings():
    """只读的运行事实：数据在哪、上次扫描何时、回收期多长、服务的权限边界。

    刻意全只读。数据位置由 LOSTPATH_DATA_DIR 决定（见 storage/paths.py），开成可写
    入口等于让界面指挥服务往任意路径写盘，凭空多一个攻击面。换位置改环境变量、重启生效。
    """
    snap = DATA.get("snapshot", {}) if isinstance(DATA, dict) else {}
    stats = snap.get("scan_stats") or {}
    # describe() 的 override_active 是 False | "LOSTPATH_DATA_DIR" 的联合类型（排障
    # 时想直接看到变量名，test_snapshots 也钉着这个形状）。不改它的契约，在这里归一
    # 化成布尔 + 变量名两个字段，别把联合类型丢给前端。
    desc = dict(lp_paths.describe())
    override = desc.pop("override_active", False)
    return {
        "paths": {
            **desc,
            "override_active": bool(override),
            "override_var": override or None,
        },
        "snapshot": {
            "present": snap.get("present", False),
            "scanned_at": snap.get("scanned_at"),
            "machine": snap.get("machine"),
            "schema_version": snap.get("schema_version"),
            # v3 以下的 size 没排除硬链接，uv / pnpm 这类共用内容的缓存会被高报数倍。
            # 界面据此引导重扫——不说的话，修好的代码对着旧数据照样给出虚高的结论。
            "sizes_inflated": snap.get("sizes_inflated", False),
            "sizes_reason": snap.get("sizes_reason"),
            "total_dirs": stats.get("total_dirs"),
            "total_files": stats.get("total_files"),
            "total_bytes": stats.get("total_bytes"),
            "elapsed_sec": stats.get("elapsed_sec"),
            # 盲区来源：非管理员进不去部分目录，denied 条数是它的直接度量。
            # 同时给出具体路径（扫描时最多留 120 条），只报数字等于没说。
            "denied_count": stats.get("denied_count"),
            "denied_sample": (stats.get("denied_sample") or [])[:40],
            "reparse_count": stats.get("reparse_count"),
            # 扫描当时是否提权。**与 engine.elevated 是两件事**：后者是此刻的进程
            # 权限，这个是产出这份数据时的权限。界面靠两者的差判断"该不该重扫"。
            # 旧快照没这个字段，取 None——界面把 None 当"不知道"，不当作 False，
            # 否则老快照会被误报成"需要重扫"。
            "elevated": stats.get("elevated"),
        },
        "recycle": {
            "recoverable_days": manifest.RECOVERABLE_DAYS,
            "recycle_root": str(manifest.recycle_dir()),
            # 每次启动引擎时清一次过期项。界面据此说明"会自动清"而不是"要手动腾"
            "auto_purge": "startup",
        },
        "engine": {
            "python": sys.version.split()[0],
            "bind": "127.0.0.1:8321",
            "elevated": _is_elevated(),
            "scan_root": runner.scan_root(),
        },
    }


class IgnoreRuleReq(BaseModel):
    path: str
    reason: str | None = None


@app.get("/api/rules")
def api_rules():
    """用户规则只收紧计划，不会直接碰文件。"""
    entries = rules.list_ignored()
    return {
        "ignored_paths": entries,
        "count": len(entries),
        "config_path": str(lp_paths.rules_config()),
    }


@app.put("/api/rules/ignore")
def api_add_ignore_rule(req: IgnoreRuleReq):
    try:
        entry = rules.add_ignored(req.path, req.reason)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": f"保存规则失败：{e}"})
    return {"rule": entry, "ignored_paths": rules.list_ignored()}


@app.delete("/api/rules/ignore")
def api_remove_ignore_rule(req: IgnoreRuleReq):
    try:
        removed = rules.remove_ignored(req.path)
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": f"删除规则失败：{e}"})
    if not removed:
        return JSONResponse(status_code=404, content={"error": "没有找到这条规则"})
    return {"removed": req.path, "ignored_paths": rules.list_ignored()}


class ScanReq(BaseModel):
    path: str


@app.post("/api/portable/scan")
def api_portable_scan(req: ScanReq):
    return inventory.scan_portable(req.path)


class ConfirmItem(BaseModel):
    name: str
    dir: str | None = None
    exe: str | None = None


class ConfirmReq(BaseModel):
    items: list[ConfirmItem]


@app.post("/api/portable/confirm")
def api_portable_confirm(req: ConfirmReq):
    n = inventory.save_portable([i.model_dump() for i in req.items])
    global DATA
    DATA = build_data()
    return {"ok": True, "total_portable": n}


if UI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")

if __name__ == "__main__":
    inspection.Scheduler(_scheduled_scan).start()
    uvicorn.run(app, host="127.0.0.1", port=8321, log_level="warning")
