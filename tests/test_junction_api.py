r"""junction 端到端：走真 HTTP 端点，真建 junction，真回滚。**标记 integration。**

    python -m pytest -m integration -q

单元测试直接调 executor，绕过了端点里的动作分派——而那里恰好出过错（junction 掉进
else 分支走清理然后被拒，功能不可达但看起来"安全"）。这一层专门守分派与 dry-run 默认。

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
BIG = 600 * 2**20        # 过 JUNCTION_MIN_SIZE 门槛


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
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


@pytest.fixture
def engine(tmp_path):
    """起引擎，快照里放一个"不可清理但可搬"的合成目录。"""
    data_dir = tmp_path / "data"
    src = tmp_path / "AppProfile"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"hello" * 100)
    (src / "sub" / "b.bin").write_bytes(b"x" * 2048)
    target_root = tmp_path / "E"
    target_root.mkdir()

    snapdir = data_dir / "snapshots"
    snapdir.mkdir(parents=True)
    (snapdir / "latest.json").write_text(json.dumps({
        "schema_version": 2,
        "scanned_at": "2026-08-29T00:00:00+00:00",
        "machine": "TESTBOX",
        "items": [{
            "path": str(src), "name": "AppProfile", "size": BIG, "files": 2,
            "cat": "用户数据", "owner_kind": "app", "conf": 0.9,
            "owner": "测试软件", "why": "测试", "redirect": None, "children": [],
            "zone": "RoamingAppData",
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

    yield base, src, target_root
    proc.kill()
    proc.wait(timeout=10)


def test_plan_offers_junction(engine):
    base, src, target_root = engine
    st, body = call(f"{base}/api/plan?target_root={str(target_root)}")
    assert st == 200, body
    mine = [p for p in body["plans"] if p["path"] == str(src)]
    assert len(mine) == 1
    assert mine[0]["action"] == "junction", mine[0]
    assert mine[0]["executable"], mine[0]["blockers"]


def test_dry_run_is_default_and_changes_nothing(engine):
    """不显式传 dry_run 必须是预演——写操作的默认值只能是安全的那一侧。"""
    base, src, _ = engine
    st, body = call(f"{base}/api/act/execute", "POST", {"path": str(src)})
    assert st == 200, body
    assert body["status"] == "dry_run"

    # 预演之后：源目录还是真目录、内容在、没建链接、没留下任何已完成的操作记录
    assert os.path.isdir(src)
    assert (src / "a.txt").exists()
    assert getattr(os.lstat(src), "st_reparse_tag", 0) == 0, "预演竟然建了链接"
    _, ops = call(f"{base}/api/act/operations")
    assert not [o for o in ops["operations"] if o.get("status") == "done"], (
        f"预演留下了已完成的操作记录：{ops['operations']}")


def test_execute_then_rollback_via_api(engine):
    """真执行 -> 原位成 junction -> 回滚 -> 完全复原。全程走 HTTP。"""
    base, src, target_root = engine
    before = sorted(p.relative_to(src).as_posix()
                    for p in src.rglob("*") if p.is_file())

    st, op = call(f"{base}/api/act/execute", "POST", {
        "path": str(src), "dry_run": False, "target_root": str(target_root)})
    assert st == 200, op
    assert op["action"] == "junction", op
    assert op["status"] == "done", op

    # 原位应是重解析点，且透过它读到的内容与原来一致
    st2 = os.lstat(src)
    assert getattr(st2, "st_reparse_tag", 0) != 0, "原位不是重解析点"
    through = sorted(p.relative_to(src).as_posix()
                     for p in src.rglob("*") if p.is_file())
    assert through == before

    # 数据真的落在目标盘目录里
    assert os.path.isdir(op["junction_target"])

    st3, out = call(f"{base}/api/act/rollback", "POST", {"op_id": op["id"]})
    assert st3 == 200, out
    assert getattr(os.lstat(src), "st_reparse_tag", 0) == 0, "回滚后仍是链接"
    after = sorted(p.relative_to(src).as_posix()
                   for p in src.rglob("*") if p.is_file())
    assert after == before
    assert not os.path.exists(op["junction_target"]), "多余副本没清"


def test_path_outside_snapshot_still_refused(engine, tmp_path):
    """junction 也不能绕过"只认快照里的路径"这条。"""
    base, _, target_root = engine
    outsider = tmp_path / "not-in-snapshot"
    outsider.mkdir()
    (outsider / "keep.bin").write_bytes(b"keep")
    st, body = call(f"{base}/api/act/execute", "POST", {
        "path": str(outsider), "dry_run": False, "target_root": str(target_root)})
    assert st == 404, body
    assert (outsider / "keep.bin").exists()
