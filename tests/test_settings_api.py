r"""设置端点的契约。**标记 integration，默认不跑**（起真引擎子进程）。

    python -m pytest -m integration -q

守两件事：

1. **只读。** 这一页展示的是数据根目录等敏感位置。一旦它能写，界面就能指挥服务往任意
   路径写盘——那是本来不存在的攻击面。所以非 GET 必须被拒。
2. **数字取自实际来源，不是前端猜的常量。** 回收期若前后端各写一份，改了一处就会
   出现"界面说 30 天、实际 14 天"这种最难发现的偏差。

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

from lostpath.act import manifest

pytestmark = pytest.mark.integration

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(url, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, method=method, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


@pytest.fixture
def engine(tmp_path):
    data_dir = tmp_path / "data"
    snapdir = data_dir / "snapshots"
    snapdir.mkdir(parents=True)
    (snapdir / "latest.json").write_text(json.dumps({
        "schema_version": 2,
        "scanned_at": "2026-08-28T00:00:00+00:00",
        "machine": "TESTBOX",
        "scan_stats": {
            "total_dirs": 1234, "total_files": 5678, "total_bytes": 9012,
            "elapsed_sec": 3.5, "denied_count": 7, "reparse_count": 2,
        },
        "items": [],
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
            call(f"{base}/api/settings")
            break
        except (urllib.error.URLError, OSError, ConnectionError):
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("引擎未就绪")

    yield base, data_dir
    proc.kill()
    proc.wait(timeout=10)


def test_reports_configured_data_dir(engine):
    """必须报当前生效的数据目录，而不是默认位置。"""
    base, data_dir = engine
    status, raw = call(f"{base}/api/settings")
    assert status == 200, raw
    body = json.loads(raw)
    assert os.path.normcase(body["paths"]["data_root"]) == os.path.normcase(str(data_dir))
    assert body["paths"]["override_active"] is True, "走了 LOSTPATH_DATA_DIR 却没标出来"


def test_exposes_scan_blind_spots(engine):
    """盲区必须能看见：非管理员跑会漏目录，只写在文档里等于用户不知道。"""
    base, _ = engine
    _, raw = call(f"{base}/api/settings")
    snap = json.loads(raw)["snapshot"]
    assert snap["present"] is True
    assert snap["denied_count"] == 7
    assert snap["total_dirs"] == 1234


def test_recoverable_days_comes_from_manifest(engine):
    """回收期取自 manifest，不许前端另写一份常量。"""
    base, _ = engine
    _, raw = call(f"{base}/api/settings")
    assert json.loads(raw)["recycle"]["recoverable_days"] == manifest.RECOVERABLE_DAYS


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_settings_is_read_only(engine, method):
    """非 GET 必须被拒。放开写入等于让界面指挥服务往任意路径写盘。"""
    base, _ = engine
    status, raw = call(f"{base}/api/settings", method, {"data_root": "E:\\evil"})
    assert status in (404, 405), f"{method} 竟然被接受了：{status} {raw[:200]}"
