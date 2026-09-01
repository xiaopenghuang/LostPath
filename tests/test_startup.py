import threading
import time

import lostpath.startup as startup
from lostpath.startup import normalize_inventory


def test_normalize_inventory_keeps_structured_fields_and_matches_owner():
    entities = [{
        "name": "Demo Editor",
        "location": r"G:\Apps\Demo Editor",
        "exe_path": r"G:\Apps\Demo Editor\demo.exe",
    }]
    raw = {
        "startup": [{
            "name": "DemoEditor",
            "key": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
            "exe": r"G:\Apps\Demo Editor\demo.exe",
            "raw": r'"G:\Apps\Demo Editor\demo.exe" --token=secret',
        }],
        "services": [{
            "name": "DemoSvc",
            "display": "Demo Service",
            "exe": r"C:\Windows\System32\svchost.exe",
            "state": "Running",
            "start": "Auto",
        }],
        "tasks": [{
            "name": "Demo Update",
            "path": "\\Demo\\",
            "actions": [r"G:\Apps\Demo Editor\updater.exe", "--silent"],
        }],
    }

    items = normalize_inventory(raw, entities)

    assert {item["kind"] for item in items} == {"startup", "service", "task"}
    startup = next(item for item in items if item["kind"] == "startup")
    task = next(item for item in items if item["kind"] == "task")
    assert startup["owner"] == "Demo Editor"
    assert task["owner"] == "Demo Editor"
    assert startup["exe"].endswith(r"demo.exe")
    assert "raw" not in startup
    assert "token" not in str(startup)
    assert "silent" not in str(task)


def test_normalize_inventory_flags_unresolved_download_startup():
    raw = {
        "startup": [{"name": "Unknown", "key": "HKCU", "exe": r"C:\Users\dev\Downloads\run.exe"}],
        "services": [{"name": "Stopped", "display": "Stopped", "exe": None, "state": "Stopped", "start": "Manual"}],
    }

    items = normalize_inventory(raw)

    unknown = next(item for item in items if item["name"] == "Unknown")
    stopped = next(item for item in items if item["name"] == "Stopped")
    assert unknown["risk"] == "attention"
    assert "下载目录" in unknown["risk_reason"]
    assert normalize_inventory({"startup": [{"name": "User App", "exe": r"G:\Apps\User\app.exe"}]})[0]["risk"] == "normal"
    assert stopped["risk"] in {"normal", "system"}
    assert "没有解析到执行文件" in stopped["risk_reason"]


def test_system_risk_is_not_hardcoded_to_c_drive(monkeypatch):
    monkeypatch.setenv("SystemDrive", "D:")
    monkeypatch.setenv("SystemRoot", r"D:\Windows")
    item = normalize_inventory({
        "services": [{
            "name": "Core", "display": "Core",
            "exe": r"D:\Windows\System32\svchost.exe",
            "state": "Running", "start": "Auto",
        }],
    })[0]

    assert item["risk"] == "system"
    assert "系统/程序目录" in item["risk_reason"]


def test_request_scan_does_not_start_a_second_worker(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def fake_export(path):
        started.set()
        release.wait(1)
        return {"startup": [{"name": "One", "exe": r"G:\Apps\one.exe"}]}

    monkeypatch.setattr(startup, "export_inventory", fake_export)
    with startup._lock:
        startup._state.update(state="idle", items=[], scanned_at=None, error=None)
        startup._worker = None

    first = startup.request_scan([])
    assert first["queued"] is True
    assert started.wait(1)
    second = startup.request_scan([], force=True)
    assert second["queued"] is False
    release.set()
    for _ in range(100):
        if startup.status()["state"] == "ready":
            break
        time.sleep(0.01)
    assert startup.status()["state"] == "ready"
    assert startup.status()["summary"]["total"] == 1


def test_wait_for_report_does_not_capture_loading_as_empty(monkeypatch):
    monkeypatch.setattr(
        startup, "export_inventory",
        lambda _path: {"startup": [{"name": "One", "exe": r"G:\Apps\one.exe"}]},
    )
    with startup._lock:
        startup._state.update(state="idle", items=[], scanned_at=None, error=None)
        startup._worker = None

    result = startup.wait_for_report([], timeout=1)

    assert result["state"] == "ready"
    assert result["summary"]["total"] == 1


def test_normalize_reads_operation_history_once(monkeypatch):
    calls = 0

    def fake_actions():
        nonlocal calls
        calls += 1
        return {}

    monkeypatch.setattr(startup, "_startup_actions", fake_actions)
    raw = {
        "startup": [{"name": f"Run {i}", "key": "HKCU", "exe": rf"G:\Apps\{i}.exe"}
                    for i in range(20)],
        "services": [{"name": f"Svc {i}", "exe": rf"G:\Svc\{i}.exe"}
                     for i in range(20)],
        "tasks": [{"name": f"Task {i}", "actions": [rf"G:\Task\{i}.exe"]}
                  for i in range(20)],
    }

    assert len(normalize_inventory(raw)) == 60
    assert calls == 1, "操作历史不得为每条启动记录重复读盘"
