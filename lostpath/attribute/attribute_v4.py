r"""LostPath 归因引擎 v4（只读）。

v3 -> v4 修正，全部由 diag_v3 / probe_unregistered / collect_evidence 实测驱动：

R1 [根因] 角色与归属混用。v3 的 TOOLCHAIN 一张表既定性质又定归属，导致
   label 被当 owner：Local\Temp -> "临时目录"、apifox-updater ->
   "自动更新器下载缓存"。v4 拆成 ROLE_RULES（只定角色 + 归属策略）与
   OWNER_TOOLCHAIN（工具自身即归属），Temp 类走 system，*-updater 走词干。
   实测：剥后缀后 6/9 精确命中注册表。

R2 [容器缺失] Packages / Package Cache / Programs 本身不属于任何软件，
   v3 留作未归因。v4 标为 container 节点，体积由子目录承担。

R3 [新证据源] Package Cache 的 {GUID} 即 MSI ProductCode，与注册表卸载项
   键名精确相等，实测命中 10/12（拿到 "Universal CRT Headers"、
   ".NET Runtime 8.0.22" 等精确名）。与 Appx 家族名同级强度。

R4 [新证据源] 快捷方式目标 exe：187 条、97% 磁盘可验，126 条指向 G 盘。
   这是把"C 盘足迹"与"非 C 盘软件本体"连起来的唯一证据。解决注册表完全
   看不见的 Typora / pgAdmin 4 / GamePP / GameViewer。

R5 [避坑] rDNS 取末段会错：com.ccswitch.desktop -> "Docker Desktop"、
   com.lgdy88.codex-enhance.manager -> "Neat Download Manager"，是 v1 子串
   匹配错误的翻版。改取最长非停用词段。

R6 [避坑] Publisher 匹配不可直接定归属。Netease 名下注册表恰好只登记一个
   软件，整块判过去就是 Tencent 错误重演。命中 Publisher 一律转厂商节点下钻。

R7 [兄弟印证] Local\zdxlz-app 与 Local\zdxlz-app-updater、Roaming\X 与
   Local\X 属同一软件。已命名的成员可把名字传播给同词干的未命名成员。

R8 [诚实标注] 注册表查不到、但目录名像产品名且有 marker 族系证据的，标为
   app_unregistered 并以目录名为名，置信 0.55。给用户可用信息，同时不假装
   拿到了注册表证据。
"""
import os
import re
import time
from collections import defaultdict

from . import lostpath_kb as KB
from .. import sysdirs

MIN_SIZE = 20 * 1024 * 1024        # 顶层足迹目录上报门槛
CHILD_MIN_SIZE = 8 * 1024 * 1024   # 下钻子目录上报门槛
DRILL_SIZE = 1 * 2**30             # 超过此体积一律下钻（不论归属类型）

# 模块状态，由 build_indexes() 一次性填充。
# 刻意保持模块级全局而不包进类：这样 attribute() / score() / classify() /
# children_of() 的函数体与搬迁前逐字节相同，"搬迁不改算法"才是可证明的，
# 而不是靠人眼比对。代价是同一进程内一次只持有一份快照的索引。
apps: list = []
DIRS: dict = {}
SHORTCUTS: list = []
NAME_IDX: dict = {}
VENDOR_IDX: dict = {}
APPX_IDX: dict = {}
MSI_IDX: dict = {}
PART_IDX: dict = {}
SC_IDX: dict = {}
LOC_IDX: list = []
integration: dict = {}
JUNCTIONS: dict = {}


def footprint_roots(user_home=None, program_data=None):
    r"""C 盘足迹的四个枚举根。

    user_home 参数化的原因：脱敏基准 fixtures 里的用户名是 devuser，而
    os.path.expanduser("~") 给的是本机用户，硬编码则基准根本跑不起来。

    program_data 同理，而它原先是写死的 `C:\ProgramData`——因为没有任何东西逼它
    参数化。后果有两层：① 系统装 D 盘的机器上整个 ProgramData 足迹根**扫不到**
    （不是扫错，是静默漏掉）；② 若改成只读 `%ProgramData%`，在那种机器上跑基准会
    连 fixtures 里的 ProgramData 记录一起丢，让条数/体积/准确率取决于跑测试的人是
    谁——本项目明令禁止的那种指标。所以它必须既能从环境变量取默认值、又能被显式传入。

    这两个参数的对比本身是个教训：**已经发生的泛化，恰好等于测试逼出来的那部分泛化。**
    """
    user = user_home or os.path.expanduser("~")
    pdata = program_data or sysdirs.program_data_dir()
    return [
        (os.path.join(user, r"AppData\Local"), "LocalAppData"),
        (os.path.join(user, r"AppData\Roaming"), "RoamingAppData"),
        (os.path.join(user, r"AppData\LocalLow"), "LocalLow"),
        (pdata, "ProgramData"),
    ]


def app_by_exe(exe):
    if not exe:
        return None
    e = exe.strip().strip('"').lower()
    for loc, a in LOC_IDX:
        if e.startswith(loc + "\\"):
            return a
    return None


# ---------------------------------------------------------------- 索引构建
def build_indexes(scan, inventory, shortcuts=None):
    """从三份输入建全部索引。scan/inventory/shortcuts 都是已解析的 JSON 对象。

    搬迁前这段是模块顶层的裸语句（import 即执行、路径写死在 probe/ 目录），
    因此引擎无法把它当库调用——这正是 P1 要解决的。此处只是把同样的语句包进
    函数并接受参数，赋值目标与顺序一律不动。
    """
    global apps, DIRS, SHORTCUTS, NAME_IDX, VENDOR_IDX, APPX_IDX
    global MSI_IDX, PART_IDX, SC_IDX, LOC_IDX, integration, JUNCTIONS

    inv = inventory
    apps = inv["apps"]
    DIRS = {p: tuple(v) for p, v in scan["dirs"].items()}
    SHORTCUTS = shortcuts or []

    for a in apps:
        a["_name"] = a.get("name") or ""
        a["_key"] = KB.norm_key(a["_name"])
        a["_stripped"] = KB.strip_version(a["_name"])
        a["_skey"] = KB.norm_key(a["_stripped"])
        loc = (a.get("installLocation") or "").strip().strip('"').rstrip("\\")
        a["_loc"] = loc if re.match(r"^[A-Za-z]:\\", loc) else ""
        a["_vendor"] = KB.vendor_key(a.get("publisher"))

    NAME_IDX = defaultdict(list)
    for a in apps:
        if a["_key"]:
            NAME_IDX[a["_key"]].append(a)
        if a["_skey"] and a["_skey"] != a["_key"]:
            NAME_IDX[a["_skey"]].append(a)

    VENDOR_IDX = defaultdict(list)
    for a in apps:
        if a["_vendor"]:
            VENDOR_IDX[a["_vendor"]].append(a)
    for dk, vn in KB.VENDOR_ALIASES.items():
        vk = KB.vendor_key(vn)
        if vk and vk not in VENDOR_IDX:
            VENDOR_IDX[vk] = list(VENDOR_IDX.get(KB.norm_key(vn), []))

    # 字段名两种都收：export_inventory.ps1 写的是 family，早期手写样本用过
    # familyName。只读一个就出过 bug（见 index_health 注释）。
    APPX_IDX = {}
    for x in inv["appx"]:
        fn = (x.get("family") or x.get("familyName") or "").lower()
        if fn:
            APPX_IDX[fn] = x

    # R3: MSI ProductCode 索引 —— 注册表卸载项键名即 {GUID}
    MSI_IDX = {}
    for a in apps:
        kb = os.path.basename((a.get("key") or "").rstrip("\\"))
        m = re.match(r"^(\{[0-9A-Fa-f\-]{36}\})", kb)
        if m:
            MSI_IDX[m.group(1).upper()] = a

    # 安装路径分量索引（P5，v3 沿用）
    PART_IDX = defaultdict(list)
    for a in apps:
        if not a["_loc"]:
            continue
        for part in a["_loc"].split("\\")[1:]:
            pk = KB.norm_key(part)
            if pk and not KB.is_generic_part(part):
                PART_IDX[pk].append(a)

    # R4: 快捷方式索引 —— 目录名 -> (label, target)
    SC_IDX = defaultdict(list)
    for s in SHORTCUTS:
        tgt = s.get("target") or ""
        if not tgt:
            continue
        stem = os.path.splitext(os.path.basename(tgt))[0]
        folder = os.path.basename(os.path.dirname(tgt))
        for cand in {KB.norm_key(stem), KB.norm_key(folder),
                     KB.norm_key(s.get("label") or "")}:
            if cand and len(cand) >= 3:
                SC_IDX[cand].append(s)

    LOC_IDX = sorted([(a["_loc"].lower(), a) for a in apps if a["_loc"]],
                     key=lambda x: -len(x[0]))

    integration = defaultdict(list)
    for s in inv["services"]:
        a = app_by_exe(s.get("exe"))
        if a:
            integration[a["_name"]].append(("service", s["name"], s.get("exe")))
    for r in inv["startup"]:
        a = app_by_exe(r.get("exe"))
        if a:
            integration[a["_name"]].append(("startup", r.get("name"), r.get("exe")))
    for p in inv["appPaths"]:
        a = app_by_exe(p.get("exe"))
        if a:
            integration[a["_name"]].append(("appPath", p.get("name"), p.get("exe")))

    # junction 目标要 readlink 真实路径，故只有在被扫机器上才解得出。
    # 跑脱敏 fixtures 时这些路径不存在，JUNCTIONS 为空——与 probe_markers()
    # 同理，属预期差异，不是错误。
    JUNCTIONS = {}
    for p in scan["reparse_points"]:
        try:
            t = os.readlink(p)
        except (OSError, ValueError):
            continue
        JUNCTIONS[p.lower()] = t.replace("\\\\?\\", "")

    return index_health(inv)


def index_health(inv):
    """输入非空但索引为空 = 字段名对不上，必须吵出来。

    本项目已在同一个坑里摔过三次：v1 读 a["loc"]（实际 installLocation）让最强
    证据源全程失效却仍跑出 71.4% 覆盖率；inventory.py 写 InstallLocation（实际
    install_location）；APPX_IDX 读 familyName（实际 family），使 conf 0.98 的
    appx_family 一次都没触发，而 Packages 下 4 个子目录（678 MiB）一直显示未归因。

    三次都不是逻辑写错，是"索引空了但汇总指标看起来正常"。所以判据不能是人记得
    去核对，而应是"有输入却建不出索引就报警"。
    """
    warnings = []
    for label, src, idx in (
        ("appx", inv.get("appx"), APPX_IDX),
        ("apps", inv.get("apps"), NAME_IDX),
        ("services/startup/appPaths", inv.get("services"), integration),
    ):
        if src and not idx:
            warnings.append(
                f"{label}：输入 {len(src)} 条但索引为空，疑似字段名对不上")
    return warnings


def ev(src, conf, detail):
    return {"source": src, "conf": round(conf, 3), "detail": detail}


def find_app(k):
    """按归一化键查软件，返回最短名命中项。"""
    hits = NAME_IDX.get(k, [])
    return sorted(hits, key=lambda a: len(a["_name"]))[0] if hits else None


# ---------------------------------------------------------------- 归因主逻辑
def attribute(name, path, zone, parent_owner=None, parent_kind=None):
    """返回 (evidence[], owner, owner_kind, family, role)。

    owner_kind: app / app_unregistered / vendor / toolchain / appx
                / container / system / unknown
    """
    evs = []
    key = KB.norm_key(name)
    owner = None
    kind = None
    role = None

    # --- 容器节点（R2）：容器自身不属于任何软件 ---
    con = KB.container_of(path)
    if con:
        label, by = con
        evs.append(ev("container", 0.90, f"{label}（子目录按{by}独立归因）"))
        return evs, label, "container", None, None

    # --- 强证据 1: Appx 家族名 ---
    fam_pkg = APPX_IDX.get(name.lower())
    if fam_pkg:
        evs.append(ev("appx_family", 0.98,
                      f"Appx 家族名精确匹配 -> {fam_pkg.get('name')}"))
        owner, kind = fam_pkg.get("name"), "appx"

    # --- 强证据 2: MSI ProductCode（R3）---
    mg = re.match(r"^(\{[0-9A-Fa-f\-]{36}\})", name)
    if mg and owner is None:
        ma = MSI_IDX.get(mg.group(1).upper())
        if ma:
            evs.append(ev("msi_productcode", 0.95,
                          f"MSI ProductCode 匹配注册表卸载项 -> {ma['_name']}"))
            owner, kind = ma["_name"], "app"

    # --- 强证据 3: junction 目标 ---
    jt = JUNCTIONS.get(path.lower())
    if jt:
        evs.append(ev("junction_target", 0.95,
                      f"重解析点已指向 {jt}（体积不计在 C 盘）"))

    # --- 角色规则（R1）：先定角色，归属另算 ---
    r = KB.role_of(name)
    if r:
        label, cat, policy, redir = r
        role = {"label": label, "cat": cat, "redirect": redir}
        evs.append(ev("role_rule", 0.30, f"角色规则：{label}（归属策略={policy}）"))
        if owner is None:
            if policy == "system":
                evs.append(ev("system_owned", 0.80,
                              f"{label} 为系统共享位置，不属于任何单个软件"))
                owner, kind = "Windows 系统", "system"
            elif policy.startswith("fixed:"):
                fx = policy.split(":", 1)[1]
                evs.append(ev("role_fixed", 0.85, f"{label} 固定归属 -> {fx}"))
                owner, kind = fx, "app"
            elif policy == "stem":
                stem = KB.role_stem(name)
                h = find_app(KB.norm_key(stem))
                if h:
                    evs.append(ev("role_stem", 0.90,
                                  f"剥离角色后缀 {name} -> {stem}，"
                                  f"匹配软件 {h['_name']}"))
                    owner, kind = h["_name"], "app"
                elif KB.looks_like_product(stem):
                    evs.append(ev("role_stem_unreg", 0.60,
                                  f"剥离角色后缀 -> {stem}，注册表无条目"
                                  f"（未注册软件）"))
                    owner, kind = stem, "app_unregistered"
            elif policy == "inherit" and parent_owner:
                evs.append(ev("role_inherit", 0.75,
                              f"{label} 继承父目录归属 -> {parent_owner}"))
                owner, kind = parent_owner, parent_kind or "app"

    # --- 工具链（工具自身即归属）---
    if owner is None:
        tc = KB.lookup_owner_toolchain(name, path)
        if tc:
            # 此表中 label 即所有者本身（npm-cache 属于 npm），与 ROLE_RULES 不同
            evs.append(ev("kb_toolchain", 0.85, f"工具链规则 -> {tc['label']}"))
            owner, kind = tc["label"], "toolchain"
            # role 标签优先用规则自带的：并非每个工具链目录都是缓存。zone 限定规则
            # （如 Roaming\uv 装的是解释器与已装工具）会给出自己的说法，硬拼
            # "缓存/数据" 会让 cat 改对了而理由里还写着"缓存"。
            role = role or {"label": tc.get("role") or f"{tc['label']} 缓存/数据",
                            "cat": tc.get("cat"), "redirect": tc.get("redirect")}

    # --- 厂商目录（R6）---
    vname = KB.lookup_vendor_alias(name)
    vkey = KB.vendor_key(vname) if vname else key
    vapps = VENDOR_IDX.get(vkey, [])
    is_vendor = bool(vname) or len(vapps) >= 1 and not NAME_IDX.get(key)
    if is_vendor and owner is None:
        detail = f"名下 {len(vapps)} 个软件" if vapps else "注册表未匹配到该厂商条目"
        pub = vname or (vapps[0].get("publisher") if vapps else None) or name
        evs.append(ev("vendor_dir", 0.82 if vapps else 0.70,
                      f"厂商目录 -> {pub}（{detail}，需逐子目录归因）"))
        owner, kind = pub, "vendor"

    # --- 名称精确匹配 ---
    if owner is None or kind == "vendor":
        h = find_app(key)
        if h:
            exact = KB.norm_key(h["_name"]) == key
            evs.append(ev("exact_name" if exact else "stripped_name",
                          0.92 if exact else 0.90,
                          f"目录名=={'软件名' if exact else '软件名(去版本)'}"
                          f" -> {h['_name']}"))
            owner, kind = h["_name"], "app"

    # --- KB 别名表 ---
    if owner is None:
        akw, anote = KB.lookup_alias(name)
        if akw:
            cand = [a for a in apps
                    if any(kw.lower() in a["_name"].lower() for kw in akw)]
            if cand:
                cand.sort(key=lambda a: len(a["_name"]))
                evs.append(ev("kb_alias", 0.90,
                              f"别名表 {name} -> {cand[0]['_name']}（{anote}）"))
                owner, kind = cand[0]["_name"], "app"
            else:
                evs.append(ev("kb_alias_noapp", 0.50,
                              f"别名表命中（{anote}）但注册表无对应软件"))

    # --- 安装路径分量匹配 ---
    if owner is None:
        parts = PART_IDX.get(key, [])
        if parts:
            parts = sorted(parts, key=lambda a: len(a["_name"]))
            evs.append(ev("install_path_part", 0.88,
                          f"匹配安装路径分量 -> {parts[0]['_name']}"
                          f"（{parts[0]['_loc']}）"))
            owner, kind = parts[0]["_name"], "app"

    # --- 快捷方式（R4）---
    if owner is None:
        scs = SC_IDX.get(key, [])
        if scs:
            s = sorted(scs, key=lambda x: not x.get("verified"))[0]
            vtag = "目标已验存在" if s.get("verified") else "目标未通过校验"
            evs.append(ev("shortcut", 0.85,
                          f"快捷方式[{s['label']}] -> {s['target']}（{vtag}）"))
            owner, kind = s["label"], "app"

    # --- rDNS（R5）---
    if owner is None:
        pick = KB.rdns_pick(name)
        if pick:
            h = find_app(KB.norm_key(pick))
            if h:
                evs.append(ev("rdns_segment", 0.88,
                              f"反向DNS取最长非通用段 {pick} -> {h['_name']}"))
                owner, kind = h["_name"], "app"
            else:
                evs.append(ev("rdns_unreg", 0.55,
                              f"反向DNS取段 -> {pick}，注册表无条目（未注册软件）"))
                owner, kind = pick, "app_unregistered"

    # --- marker 只标族系，不构成归属 ---
    fam, marker = KB.probe_markers(path)
    if fam:
        if owner is not None:
            evs.append(ev("kb_marker_boost", 0.05,
                          f"标识文件 {marker} 确认族系={fam}（佐证）"))
        else:
            evs.append(ev("kb_marker_only", 0.30,
                          f"标识文件 {marker} 仅确认族系={fam}，"
                          f"无法确定具体软件"))

    # --- 未注册软件兜底（R8）---
    if owner is None and fam and KB.looks_like_product(name):
        evs.append(ev("unregistered_app", 0.55,
                      f"目录名形似产品名且有族系证据({fam})，"
                      f"但注册表/快捷方式均无条目"))
        owner, kind = name, "app_unregistered"

    if kind == "app" and owner in integration:
        kinds = sorted({x[0] for x in integration[owner]})
        evs.append(ev("integration", 0.06, f"存活集成点{kinds}"))

    return evs, owner, kind, fam, role


def score(evs):
    pos = [e["conf"] for e in evs if e["conf"] > 0]
    if not pos:
        return 0.0
    base = max(pos)
    extra = sum(min(e["conf"], 0.06) for e in evs if 0 < e["conf"] < base) * 0.5
    penalty = sum(e["conf"] for e in evs if e["conf"] < 0)
    return max(0.0, min(0.99, base + extra + penalty))


def classify(name, path, role=None, kind=None):
    """定性：角色规则优先（更具体），其次名称特征，其次类型默认。"""
    if role and role.get("cat"):
        return role["cat"], f"角色规则：{role['label']}"
    hr = KB.high_risk(path)
    if hr:
        return "不可动", hr
    cat, by = KB.classify_name(name)
    if cat:
        return cat, by
    if kind == "container":
        return "容器", "本身不含数据，体积由子目录承担"
    if kind == "system":
        return "系统所有", "系统共享位置"
    return "未定性", "无名称特征"


def children_of(path):
    prefix = path + "\\"
    out = []
    for p, (sz, fc) in DIRS.items():
        if p.startswith(prefix) and "\\" not in p[len(prefix):]:
            out.append((os.path.basename(p), p, sz, fc))
    return sorted(out, key=lambda x: -x[2])


# ---------------------------------------------------------------- 主流程
def attribute_footprint(scan, inventory, shortcuts=None, user_home=None,
                        program_data=None):
    """对一次扫描结果做归因，返回 (records, stats)。

    这是本模块唯一的对外入口。搬迁前这段是模块顶层裸语句，import 即跑完整
    流程并往 probe/ 写文件，engine 无法调用；现在包成函数，赋值与顺序不动。
    """
    warnings = build_indexes(scan, inventory, shortcuts)
    t0 = time.time()
    results = []
    for root, zone in footprint_roots(user_home, program_data):
        for name, path, sz, fc in children_of(root):
            if sz < MIN_SIZE:
                continue
            evs, owner, kind, fam, role = attribute(name, path, zone)
            conf = score(evs)
            cat, why = classify(name, path, role, kind)
            rec = {
                "zone": zone, "name": name, "path": path, "size": sz,
                "files": fc, "owner": owner, "owner_kind": kind or "unknown",
                "family": fam, "conf": round(conf, 3), "cat": cat, "why": why,
                "role": role["label"] if role else None,
                "redirect": role.get("redirect") if role else None,
                "evidence": evs, "children": [],
            }
            # 容器 / 厂商 / 大目录下钻
            if kind in ("container", "vendor") or sz > DRILL_SIZE:
                for cn, cp, csz, cfc in children_of(path):
                    if csz < CHILD_MIN_SIZE:
                        continue
                    cevs, cow, ckind, cfam, crole = attribute(
                        cn, cp, zone, parent_owner=owner, parent_kind=kind)
                    inherited = False
                    if cow is None and kind not in ("container",):
                        cow, ckind, inherited = owner, kind, True
                    ccat, cwhy = classify(cn, cp, crole, ckind)
                    rec["children"].append({
                        "name": cn, "path": cp, "size": csz, "files": cfc,
                        "owner": cow, "owner_kind": ckind or "unknown",
                        "conf": round(score(cevs), 3), "inherited": inherited,
                        "cat": ccat, "why": cwhy,
                        "role": crole["label"] if crole else None,
                        "evidence": cevs,
                    })
            results.append(rec)

    results.sort(key=lambda r: -r["size"])
    promoted = confirm_siblings(results)
    evidence_hits = defaultdict(int)
    for r in results:
        for rec in (r, *r["children"]):
            for e in rec["evidence"]:
                evidence_hits[e["source"]] += 1
    return results, {
        "elapsed_sec": round(time.time() - t0, 2),
        "entries": len(results),
        "total_size": sum(r["size"] for r in results),
        "unknown_size": sum(r["size"] for r in results if not r["owner"]),
        "sibling_promoted": promoted,
        # 每个证据源命中几次。全为 0 的证据源就是死代码——appx_family 曾长期
        # 是 0 而无人发现，因为汇总精度看起来是满分。
        "evidence_hits": dict(sorted(evidence_hits.items(),
                                     key=lambda x: -x[1])),
        "index_warnings": warnings,
    }


def confirm_siblings(results):
    """兄弟印证后处理（R7）。原地改 results，返回提升条数。"""
    by_stem = defaultdict(list)
    for r in results:
        stem = KB.norm_key(KB.role_stem(r["name"]))
        if stem and len(stem) >= 4:
            by_stem[stem].append(r)

    promoted = 0
    for stem, group in by_stem.items():
        if len(group) < 2:
            continue
        named = [r for r in group
                 if r["owner_kind"] in ("app", "appx", "toolchain")]
        unnamed = [r for r in group if r["owner"] is None]
        if named and unnamed:
            src = max(named, key=lambda r: r["conf"])
            for u in unnamed:
                u["owner"] = src["owner"]
                u["owner_kind"] = src["owner_kind"]
                u["evidence"].append(ev("sibling", 0.70,
                                        f"同词干兄弟目录 {src['name']} 已归因"
                                        f"（{src['owner']}），交叉印证"))
                u["conf"] = round(score(u["evidence"]), 3)
                promoted += 1
    return promoted
