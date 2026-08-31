"""扫描端点的 HTTP 契约。**标记为 integration，默认不跑。**

    python -m pytest tests/ -m integration

为什么起真子进程而不用 TestClient：本环境的 starlette 要额外依赖才有 TestClient，
而 `engine/main.py` 在 import 时就会跑 `build_data()`（枚举注册表）并起图标提取
线程——为了让它能被 import 而改启动路径，代价大于收益。起子进程虽然慢（约 20s），
但测的是真东西，含路由与序列化。

数据目录指到临时目录，绝不碰用户真实快照。
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

from lostpath.storage import snapshots

pytestmark = pytest.mark.integration

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_TIMEOUT_SEC = int(os.getenv("LOSTPATH_INTEGRATION_SCAN_TIMEOUT_SEC", "480"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(url, method="GET"):
    """返回 (status, body_dict)。4xx 也要拿到 body——冲突提示就在里面。"""
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    """起一个引擎子进程，数据目录隔离。模块级复用，避免反复付启动代价。"""
    data_dir = tmp_path_factory.mktemp("engine-data")
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
            call(f"{base}/api/scan/status")
            break
        except (urllib.error.URLError, OSError, ConnectionError):
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("引擎 60s 内未就绪")

    yield base
    proc.kill()
    proc.wait(timeout=10)


def wait_until_idle(base, timeout=SCAN_TIMEOUT_SEC):
    """等当前任务跑完，返回终态。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _s, body = call(f"{base}/api/scan/status")
        last = body
        if body.get("state") in ("idle", "done", "failed", "cancelled"):
            return body
        time.sleep(0.5)
    pytest.fail(f"等待任务结束超时（{timeout}s），最后状态：{last}")


def test_status_is_idle_before_any_scan(engine):
    status, body = call(f"{engine}/api/scan/status")
    assert status == 200
    assert body["state"] == "idle"


def test_cancel_without_running_job_returns_409(engine):
    status, body = call(f"{engine}/api/scan/cancel", method="POST")
    assert status == 409
    assert body["conflict"], "冲突原因必须放在 conflict 而不是 error"


def test_start_then_concurrent_start_returns_409_with_reason(engine):
    """409 的响应体必须真的带上冲突原因。

    这条守一个实际犯过的 bug：原先返回 {"error": "已有扫描任务在进行中", **snapshot}，
    而 snapshot() 自带 error 键（值 None，表示任务本身没出错），展开在后把提示
    覆盖成了 null。改用 conflict 键，两者不再互相踩。
    """
    status, first = call(f"{engine}/api/scan", method="POST")
    assert status == 200
    assert first["state"] in ("pending", "running")
    try:
        status2, body2 = call(f"{engine}/api/scan", method="POST")
        assert status2 == 409
        assert body2.get("conflict"), f"409 未带冲突原因：{body2}"
        assert body2.get("job_id") == first["job_id"], "应带上在跑任务的 id"
        assert body2.get("error") is None, "任务本身没出错，error 应为 None"
    finally:
        call(f"{engine}/api/scan/cancel", method="POST")
        wait_until_idle(engine)


def test_cancel_reports_cancel_requested_immediately(engine):
    """取消是异步生效的，响应必须能让 UI 立刻反馈，否则界面像没反应。"""
    call(f"{engine}/api/scan", method="POST")
    try:
        status, body = call(f"{engine}/api/scan/cancel", method="POST")
        assert status == 200
        assert body["cancel_requested"] is True
    finally:
        final = wait_until_idle(engine)
        assert final["state"] == "cancelled"


def test_full_scan_completes_and_data_is_refreshed(engine):
    """整条真路径：扫完后 /api/data 必须已经是新数据。

    这是那个竞态的端到端回归——曾经 done 时服务端还在重建缓存，前端拉到旧数据。
    """
    _s, before = call(f"{engine}/api/data")
    assert before["snapshot"]["present"] is False, "隔离的数据目录应无快照"
    assert before["summary"]["entries"] == 0

    status, job = call(f"{engine}/api/scan", method="POST")
    assert status == 200
    final = wait_until_idle(engine)
    assert final["state"] == "done", f"扫描未成功：{final.get('error')}"

    r = final["result"]
    assert r["entries"] > 0
    assert r["scanned_dirs"] > 1000, "全盘扫描的目录数不该这么少"
    assert r["index_warnings"] == [], f"索引告警：{r['index_warnings']}"

    _s, after = call(f"{engine}/api/data")
    assert after["snapshot"]["present"] is True, "扫完了但 /api/data 仍说没有快照"
    # 别写字面量：这个数随 schema 演进变，写死的话每次升版本都要来改一处无关的断言
    # （v3 的语义变更——size 排除硬链接——就撞上过这条）。
    assert after["snapshot"]["schema_version"] == snapshots.SCHEMA_VERSION
    # 刚扫出来的快照必须是"体积已去重"的，否则界面会一直挂着"重扫更准"的横幅
    assert after["snapshot"].get("sizes_inflated") is False, \
        "本版扫出来的快照不该被标成体积虚高"
    assert after["summary"]["entries"] == r["entries"], \
        "done 时数据还没刷新——竞态回归了"
    assert after["summary"]["entities"] > before["summary"]["entities"], \
        "合成实体没有出现，痕迹挂接可能没跑"


def test_scan_after_completion_is_allowed(engine):
    """跑完一次后应能再起一次，单例锁不能把自己锁死。"""
    status, _job = call(f"{engine}/api/scan", method="POST")
    assert status == 200
    call(f"{engine}/api/scan/cancel", method="POST")
    wait_until_idle(engine)
