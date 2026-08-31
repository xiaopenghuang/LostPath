"""基准判定规则。**刻意不 import lostpath.attribute**。

为什么单独一个模块：判定规则一旦跟着被评判的引擎一起演化，基准就变成自证。
本文件的 judge/norm/same_owner 与 probe/compare_v3_v4.py 逐字相同（P1 搬迁时
原样带过来），改动它等于改动评分标准，必须单独讨论。

judge 三态的意义（v1 教训）：
  correct / wrong / missing 必须分开统计。v1 曾以 71.4% 的"覆盖率"掩盖了
  错判集中在最大目录上的事实——把 Code 9.93 GiB 判给 Codex Account Switch。
  只看汇总覆盖率是发现不了引擎已坏的。
"""
import re

CORP = re.compile(
    r"(corporation|corporate|company|technologies|technology|software|inc|ltd|"
    r"llc|gmbh|co|限公司|有限|科技|股份|网络|信息|软件)", re.I)
VER = re.compile(r"\s*v?\d+(\.\d+)+.*$")


def norm(s):
    if not s:
        return ""
    s = VER.sub("", str(s))
    s = CORP.sub("", s.lower())
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)


def same_owner(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return (len(na) >= 4 and len(nb) >= 4) and (na in nb or nb in na)


def judge(pred_owner, pred_kind, t):
    """把一条预测判成 correct / wrong / missing。

    kind=container/system 时只要求预测落在容器族类型内，不要求 owner 字面相等
    （容器的 label 是"Appx 应用数据容器"这类描述，不是软件名）。
    """
    tk, to = t["kind"], t.get("owner")
    if not pred_owner:
        return "missing"
    if tk in ("container", "relocated", "system"):
        if pred_kind in ("vendor", "appx", "container", "system", "relocated"):
            return "correct"
        return "wrong"
    if tk == "vendor":
        if pred_kind == "vendor" and (to is None or same_owner(pred_owner, to)):
            return "correct"
        return "wrong"
    if tk == "toolchain":
        if pred_kind == "toolchain" and (to is None or same_owner(pred_owner, to)):
            return "correct"
        return "wrong"
    if to is None:
        return "correct" if pred_kind in ("app", "vendor", "unknown") else "wrong"
    return "correct" if same_owner(pred_owner, to) else "wrong"


def tally(records, truth):
    """按 truth 逐条判定，返回 (计数 dict, 明细 list)。

    只统计 truth 标注过且出现在结果中的条目——truth 有 41 条，其中一部分在
    MIN_SIZE 门槛外或不在四个足迹根下，本就不该进分母。
    """
    idx = {r["path"].lower(): r for r in records}
    counts = {"correct": 0, "wrong": 0, "missing": 0}
    detail = []
    for path, t in truth.items():
        r = idx.get(path)
        if not r:
            continue
        verdict = judge(r.get("owner"), r.get("owner_kind"), t)
        counts[verdict] += 1
        detail.append({
            "name": r["name"], "path": path, "size": r["size"],
            "verdict": verdict, "truth_kind": t["kind"],
            "truth_owner": t.get("owner"),
            "pred_owner": r.get("owner"), "pred_kind": r.get("owner_kind"),
        })
    detail.sort(key=lambda d: -d["size"])
    return counts, detail
