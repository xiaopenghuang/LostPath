r"""启动时自动清理过期回收区。**标记 integration**（起真引擎子进程）。

    python -m pytest -m integration -q

在这之前 purge_expired 只在用户点击时跑，没人点就一直占盘——"30 天可恢复"实际等于
"永远留着直到你想起来"。现在每次启动引擎清一次。

这是**自动执行的不可逆删除**，所以守两条：
1. 回收期内的一个都不许动；
2. 删了什么要有审计痕迹（logs/purge.log + manifest 上的 purged_at）。

数据目录指到临时目录，回收区内容由测试自己造，绝不碰用户真实数据。
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def iso(dt):
    return dt.isoformat(timespec="seconds")


def make_op(data_dir, op_id, *, expired: bool):
    """造一条已完成的操作 + 它在回收区里的数据。"""
    ops = data_dir / "operations"
    ops.mkdir(parents=True, exist_ok=True)
    recycled = data_dir / "recycle" / op_id / "cachedir"
    recycled.mkdir(parents=True, exist_ok=True)
    (recycled / "blob.bin").write_bytes(b"x" * 512)

    now = datetime.now(timezone.utc)
    until = now - timedelta(days=1) if expired else now + timedelta(days=15)
    created = iso(now - timedelta(days=40 if expired else 1))
    op = {
        "schema_version": 1,
        "id": op_id,
        "action": "cleanup",
        "status": "done",
        "created_at": created,
        "recoverable_until": iso(until),
        "source_path": str(data_dir / "fake-src" / op_id),
        "recycled_to": str(recycled),
        "steps_done": [],
    }
    # 文件名必须与 manifest.path_for 一致（created_at + id）。手写成 <id>.json 的话
    # 引擎那边 mark() 会另存一个正确命名的文件，测试读的还是旧的那份，于是
    # "数据删了但 purged_at 没记"——第一次就这么误判过。
    name = created.replace(":", "-").replace("+00-00", "Z")
    (ops / f"{name}-{op_id}.json").write_text(
        json.dumps(op, ensure_ascii=False), encoding="utf-8")
    return recycled


def read_op(data_dir, op_id):
    """按 id 找操作记录，不假设文件名。"""
    for p in (data_dir / "operations").glob("*.json"):
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        if d.get("id") == op_id:
            return d
    raise AssertionError(f"找不到操作记录 {op_id}")


def boot_engine_once(data_dir):
    """只导入 main（触发启动逻辑），不真起 HTTP 服务。"""
    env = dict(os.environ, LOSTPATH_DATA_DIR=str(data_dir))
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'engine'); sys.path.insert(0, '.');"
         "import main; print('booted')"],
        cwd=REPO, env=env, capture_output=True, timeout=300)
    assert r.returncode == 0, (
        f"引擎启动失败：\n{r.stdout.decode('utf-8', 'replace')}\n"
        f"{r.stderr.decode('utf-8', 'replace')}")
    return r


def test_expired_data_purged_on_startup(tmp_path):
    data_dir = tmp_path / "data"
    gone = make_op(data_dir, "expired01", expired=True)
    assert gone.is_dir()

    boot_engine_once(data_dir)

    assert not gone.exists(), "过期数据启动后仍在"
    op = read_op(data_dir, "expired01")
    assert op.get("purged_at"), "没记 purged_at，界面会以为还能回滚"
    assert op.get("recycled_to") is None


def test_in_period_data_survives_startup(tmp_path):
    """回收期内的数据一个字节都不许动——这是"30 天可撤销"的全部含义。"""
    data_dir = tmp_path / "data"
    keep = make_op(data_dir, "fresh01", expired=False)

    boot_engine_once(data_dir)

    assert keep.is_dir(), "回收期内的数据被删了"
    assert (keep / "blob.bin").exists()
    op = read_op(data_dir, "fresh01")
    assert not op.get("purged_at")


def test_mixed_only_expired_goes(tmp_path):
    data_dir = tmp_path / "data"
    gone = make_op(data_dir, "old01", expired=True)
    keep = make_op(data_dir, "new01", expired=False)

    boot_engine_once(data_dir)

    assert not gone.exists()
    assert keep.is_dir()


def test_purge_is_logged(tmp_path):
    """自动删除必须留审计痕迹，否则用户发现数据没了却查不到是谁删的。"""
    data_dir = tmp_path / "data"
    make_op(data_dir, "expired02", expired=True)

    boot_engine_once(data_dir)

    log = data_dir / "logs" / "purge.log"
    assert log.is_file(), "没写 purge.log"
    text = log.read_text(encoding="utf-8")
    assert "expired02" in text
    assert "永久删除" in text


def test_startup_survives_locked_recycle_dir(tmp_path):
    """回收区里有删不掉的东西时，引擎必须照样起来。

    不能因为一个目录被占用就让整个应用打不开——那是拿可用性换整洁。
    """
    data_dir = tmp_path / "data"
    recycled = make_op(data_dir, "locked01", expired=True)
    # 占住里面的文件，让 rmtree 失败
    f = open(recycled / "blob.bin", "rb")
    try:
        boot_engine_once(data_dir)          # 不抛异常即通过
    finally:
        f.close()
