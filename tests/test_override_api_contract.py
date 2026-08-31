r"""逐项覆盖端点的结构性约束（`engine/main.py`）。

**为什么用结构性检查**：跟 `test_no_console_window.py` 同一个道理。端点的行为要
起真引擎子进程才能测（见 test_settings_api.py 那套），成本高；而这里真正怕的是
**代码形状**退回去：

  ① `source` 不再经过快照校验
     这个值会被**存下来**，之后每次出计划都一路进 os.path.join。不限制的话调用方
     可以为任意路径预置一条覆盖，等哪天那个路径进了快照就按它执行——把"现在无害
     的写入"变成"以后的任意目标"。而正常网络下这条路径永远不触发，跑测试发现不了。

  ② 界面自己拼镜像后缀
     那等于把 planner 的规则复制到前端，两份实现必然漂移，症状是"界面显示的目标
     和实际搬过去的位置不一样"。端点必须把算好的 target 回给界面。

存储层的行为由 test_target_root.py 覆盖（含"出计划与执行解析一致"那条）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

MAIN_PY = Path(__file__).resolve().parents[1] / "engine" / "main.py"


@pytest.fixture(scope="module")
def src() -> str:
    if not MAIN_PY.exists():
        pytest.skip(f"没有 {MAIN_PY}")
    return MAIN_PY.read_text("utf-8")


def _handler_body(text: str, name: str) -> str:
    """取出一个端点函数的源码（到下一个顶层 def/@app 之前）。"""
    m = re.search(rf"^def {re.escape(name)}\(", text, re.M)
    assert m, f"找不到端点函数 {name}——被改名或删了，这条测试失去意义"
    rest = text[m.end():]
    nxt = re.search(r"^(?:@app\.|def |class )", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def test_override_endpoint_validates_source_against_snapshot(src: str):
    """写覆盖之前必须确认 source 在当前快照里。"""
    body = _handler_body(src, "api_set_override")
    assert "_record_by_path(req.source)" in body, \
        "api_set_override 没有用 _record_by_path 校验 source"

    # 校验必须在写入之前。顺序反了的话坏值已经落盘，再拒绝也没用。
    i_check = body.index("_record_by_path(req.source)")
    i_write = body.index("set_override")
    assert i_check < i_write, \
        "source 校验出现在 set_override 之后——坏值已经落盘了"

    # 校验不过必须真的中断，不能只是记一笔然后继续
    seg = body[i_check:i_write]
    assert "return" in seg and ("404" in seg or "status_code" in seg), \
        "source 不在快照里时没有提前 return"


def test_override_endpoint_returns_resolved_target(src: str):
    """端点要回算好的 target，别让界面自己拼镜像后缀。"""
    body = _handler_body(src, "api_set_override")
    assert "planner.plan_for" in body, \
        "api_set_override 没有调 planner.plan_for 算目标"
    assert re.search(r'"target"\s*:', body), \
        "响应里没有 target 字段，界面只能自己拼——两份实现必然漂移"


def test_override_list_reports_validity(src: str):
    """列出覆盖时必须带上每条是否还有效。

    planner 对失效的覆盖是**静默回落**的。不显式告知的话，用户会以为还在用自己
    设的位置，然后拿着错误的预期按下执行。
    """
    body = _handler_body(src, "api_list_overrides")
    assert "validate(" in body, "api_list_overrides 没有重新校验每条覆盖"
    assert re.search(r'"valid"\s*:', body), "响应里没有 valid 字段"


def test_ui_does_not_reimplement_mirror_rule():
    r"""前端不能自己拼镜像后缀。

    规则只该有一份（planner.mirror_suffix）。前端复制一遍的话，改了后端忘了改
    前端，界面就会显示一个和实际不同的目标位置——而两边都"看起来对"。
    """
    ui = Path(__file__).resolve().parents[1] / "ui" / "src"
    if not ui.exists():
        pytest.skip("没有 ui/src")
    offenders = []
    for f in list(ui.rglob("*.ts")) + list(ui.rglob("*.tsx")):
        t = f.read_text("utf-8", errors="replace")
        # 前端若在拼目标路径，会出现"把源路径按分隔符切开再接到根后面"这类操作
        if re.search(r"AppData\\\\+(?:Local|Roaming)", t) and "target" in t.lower():
            offenders.append(f"{f.name}: 出现硬编码的 AppData 路径拼接")
    assert not offenders, (
        "前端疑似自己实现了镜像规则：\n  " + "\n  ".join(offenders)
        + "\n目标路径应当只由后端计算并通过响应返回。"
    )
