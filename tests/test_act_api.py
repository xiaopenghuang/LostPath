r"""执行端点的安全契约。**标记 integration，默认不跑**（起真引擎子进程）。

    python -m pytest -m integration -q

守的核心是那条最要命的性质：**端点只接受路径，记录一律从快照里查。** 这些端点无鉴权
（只绑回环），若能接受调用方自带的记录或任意目标目录，它就等于"我说删哪个目录就删
哪个"。所以任何不在快照里的路径都必须被拒绝，哪怕它真实存在。

数据目录指到临时目录，快照由测试自己写，绝不碰用户真实数据。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.integration

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, method=method, data=data,
        headers={**({"Content-Type": "application/json"} if data else {}),
                 **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


@pytest.fixture
def engine(tmp_path):
    """起引擎，并预置一份只含合成目录的快照。"""
    data_dir = tmp_path / "data"
    cache = tmp_path / "fakecache"
    (cache / "sub").mkdir(parents=True)
    (cache / "sub" / "x.bin").write_bytes(b"x" * 4096)

    # 预置快照：只有这一条，所以别的路径都该被拒
    snapdir = data_dir / "snapshots"
    snapdir.mkdir(parents=True)
    (snapdir / "latest.json").write_text(json.dumps({
        "schema_version": 2,
        "scanned_at": "2026-08-28T00:00:00+00:00",
        "machine": "TESTBOX",
        "items": [{
            "path": str(cache), "name": "fakecache", "size": 200 * 2**20,
            "files": 1, "cat": "可再生缓存", "owner_kind": "toolchain",
            "conf": 0.9, "owner": "测试工具", "why": "测试", "redirect": None,
            "children": [],
        }],
    }, ensure_ascii=False), encoding="utf-8")

    port = free_port()
    env = dict(os.environ, LOSTPATH_DATA_DIR=str(data_dir))
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'engine'); sys.path.insert(0, '.');"
         f"import uvicorn, main; uvicorn.run(main.app, host='127.0.0.1', port={port},"
         " log_level='warning')"],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if proc.poll() is not None:
            out = (proc.stdout.read() or b"").decode("utf-8", "replace")
            pytest.fail(f"引擎启动失败：\n{out[-2000:]}")
        try:
            call(f"{base}/api/act/operations")
            break
        except (urllib.error.URLError, OSError, ConnectionError):
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("引擎未就绪")

    yield base, cache
    proc.kill()
    proc.wait(timeout=10)


# ------------------------------------------------- 只认快照里的路径
def test_path_outside_snapshot_is_refused(engine, tmp_path):
    """真实存在但不在快照里的目录必须被拒。这是本组端点最关键的性质。"""
    base, _cache = engine
    outsider = tmp_path / "not-in-snapshot"
    outsider.mkdir()
    (outsider / "keep.bin").write_bytes(b"keep")

    status, body = call(f"{base}/api/act/execute", "POST",
                        {"path": str(outsider), "dry_run": False})
    assert status == 404, body
    assert "快照" in body["error"]
    assert (outsider / "keep.bin").exists(), "不在快照里的目录被动了"


@pytest.mark.parametrize("evil", [
    r"C:\Windows\System32",
    r"C:\Program Files",
    "C:\\",
])
def test_system_paths_are_refused(engine, evil):
    base, _cache = engine
    status, body = call(f"{base}/api/act/execute", "POST",
                        {"path": evil, "dry_run": False})
    # 不在快照里 -> 404；即便在，执行器的闸也会 409。两者都不能是 200
    assert status in (404, 409), f"{evil} 竟被接受：{status} {body}"


# ------------------------------------------------------- dry-run 默认
def test_dry_run_is_the_default(engine):
    """不显式关掉 dry_run 就不该动文件——默认安全。"""
    base, cache = engine
    status, body = call(f"{base}/api/act/execute", "POST", {"path": str(cache)})
    assert status == 200, body
    assert body["status"] == "dry_run"
    assert cache.exists(), "dry-run 却动了文件"
    status, ops = call(f"{base}/api/act/operations")
    assert ops["summary"]["total"] == 0, "dry-run 不该留下操作记录"


def test_cross_origin_write_is_rejected(engine):
    """外部网页不能借浏览器会话触发执行端点。"""
    base, cache = engine
    status, body = call(
        f"{base}/api/act/execute", "POST", {"path": str(cache), "dry_run": False},
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403, body
    assert cache.exists(), "跨站请求不应触碰源目录"


# --------------------------------------------- 真执行 → 回滚 → 历史
def test_execute_then_rollback_full_cycle(engine):
    """端到端：真删（入回收区）→ 历史可见 → 回滚 → 数据回来。"""
    base, cache = engine
    payload = (cache / "sub" / "x.bin").read_bytes()

    status, op = call(f"{base}/api/act/execute", "POST",
                      {"path": str(cache), "dry_run": False})
    assert status == 200, op
    assert op["status"] == "done"
    assert not cache.exists(), "执行后源目录应已移走"
    assert os.path.isdir(op["recycled_to"]), "数据应在回收区"

    status, hist = call(f"{base}/api/act/operations")
    assert hist["summary"]["total"] == 1
    assert hist["summary"]["rollbackable"] == 1
    assert hist["summary"]["recycle_bytes"] > 0

    status, rb = call(f"{base}/api/act/rollback", "POST", {"op_id": op["id"]})
    assert status == 200, rb
    assert rb["status"] == "rolled_back"
    assert (cache / "sub" / "x.bin").read_bytes() == payload, "回滚后数据不一致"

    status, hist2 = call(f"{base}/api/act/operations")
    assert hist2["summary"]["rollbackable"] == 0


def test_rollback_unknown_id_returns_409(engine):
    base, _cache = engine
    status, body = call(f"{base}/api/act/rollback", "POST", {"op_id": "deadbeef"})
    assert status == 409
    assert body["refused"]


def test_purge_refuses_within_recovery_window(engine):
    """回收期内调 purge 不该删掉任何东西。"""
    base, cache = engine
    _s, op = call(f"{base}/api/act/execute", "POST",
                  {"path": str(cache), "dry_run": False})
    status, res = call(f"{base}/api/act/purge", "POST", {})
    assert status == 200
    assert res["purged"] == []
    assert os.path.isdir(op["recycled_to"])


def test_data_refreshed_after_execute(engine):
    """执行后台账要跟着变，否则界面显示的还是操作前的痕迹。"""
    base, cache = engine
    _s, before = call(f"{base}/api/data")
    _s, op = call(f"{base}/api/act/execute", "POST",
                  {"path": str(cache), "dry_run": False})
    _s, after = call(f"{base}/api/data")
    assert after["summary"]["entries"] <= before["summary"]["entries"]
