"""软件台账：从注册表/Appx 枚举本机全部软件，推断本体位置，聚合碎片，关联 C 盘痕迹。

数据模型（DESIGN.md 2026-08-28 修订）：以软件实体为中心，本体（任意盘）+ 痕迹（v4 归因）。
全部只读；便携软件经 用户扫描→候选→确认 后写入用户目录的 config/portable.json。

数据位置：一律经 lostpath.storage.paths 取，不在仓库内。快照/图标/配置都是
"每台机器各不相同"的用户数据，随源码分发会把开发者的机器状态发给所有用户。
"""
import json
import os
import re
import sys
import tempfile
import time
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 允许以 engine/main.py 直接启动时找到 lostpath 包
    sys.path.insert(0, str(ROOT))

from lostpath.storage import paths as _paths  # noqa: E402

PORTABLE_FILE = _paths.portable_config()

UNINSTALL_HIVES = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM64"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM32"),
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU"),
]

APPX_PATH = (r"Software\Classes\Local Settings\Software\Microsoft"
             r"\Windows\CurrentVersion\AppModel\Repository\Packages")
APPX_FRAMEWORK_HINTS = ("vclib", "net.native", "framework", "runtime", "uwpdesktop")

# 命中即视为碎片组件（连同 SystemComponent=1），聚合进同发布商主程序或发布商桶
COMPONENT_HINTS = re.compile(
    r"sdk|runtime|redistributable|addon|plugin|module|\bdriver\b|组件|模块|语言包|运行库|"
    r"cu(dnn|fft|blas|sparse|rand)|nvjitlink|physx| texture|纹理|材质|directx|openal|"
    r"\bres:\b|setup|bootstrapper", re.I)

VENDOR_SUFFIXES = (
    " corporation", " corp", " inc", " ltd", " co", " llc", " gmbh", " networks",
    " technologies", " technology", " software", " systems", " interactive",
    " entertainment", " digital", " media", " studio", " labs", " limited",
)

PORTABLE_EXE_SKIP = re.compile(
    r"unins|uninstall|setup|install|helper|crash|updater?|report|patcher|配置|卸载|修复|激活", re.I)


def _str(sk, name: str) -> str | None:
    try:
        v, t = winreg.QueryValueEx(sk, name)
        return v if isinstance(v, str) and v.strip() else None
    except OSError:
        return None


def _dword(sk, name: str) -> int | None:
    try:
        v, t = winreg.QueryValueEx(sk, name)
        return v if isinstance(v, int) else None
    except OSError:
        return None


def norm_publisher(p: str | None) -> str:
    if not p:
        return ""
    s = p.strip().lower()
    for suf in VENDOR_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s)


def norm_token(s: str) -> str:
    """归一化用于名称/痕迹匹配：去版本号、括号注记、全部符号。"""
    s = s.lower()
    s = re.sub(r"\((user|x64|x86|64-bit|32-bit)\)", " ", s)
    s = re.sub(r"\d+(\.\d+)+", " ", s)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s)


def _clean_candidate(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if s.lower().startswith("msiexec"):
        return None
    if "," in s and re.search(r",\s*\d+\s*$", s):  # DisplayIcon 的 ",图标序号" 后缀
        s = s.split(",")[0].strip()
    if s.startswith('"') and '"' in s[1:]:
        s = s[1: s.index('"', 1)]
    else:
        s = s.split(" /")[0].strip()
    s = os.path.expandvars(s).strip().strip('"')
    if s.lower().endswith(".exe"):
        s = os.path.dirname(s)
    s = s.rstrip("\\/")
    if len(s) <= 2 or not re.match(r"^[a-zA-Z]:\\", s):
        return None
    return s


def infer_location(e: dict) -> tuple[str | None, str | None]:
    """返回 (路径, 依据)；优先 InstallLocation，其次 UninstallString / DisplayIcon。"""
    for field, basis in (("install_location", "InstallLocation"),
                         ("uninstall_string", "UninstallString 解析"),
                         ("display_icon", "DisplayIcon 解析")):
        cand = _clean_candidate(e.get(field))
        if cand:
            return cand, basis
    return None, None


def read_registry() -> list[dict]:
    out = []
    for hive, path, label in UNINSTALL_HIVES:
        try:
            with winreg.OpenKey(hive, path) as root:
                n_keys = winreg.QueryInfoKey(root)[0]
                for i in range(n_keys):
                    try:
                        key_name = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, key_name) as sk:
                            name = _str(sk, "DisplayName")
                            if not name or "${" in name:
                                continue
                            out.append({
                                "key": key_name,
                                "hive": label,
                                "name": name.strip(),
                                "version": _str(sk, "DisplayVersion"),
                                "publisher": _str(sk, "Publisher"),
                                "install_location": _str(sk, "InstallLocation"),
                                "uninstall_string": _str(sk, "UninstallString"),
                                "display_icon": _str(sk, "DisplayIcon"),
                                "estimated_size_kb": _dword(sk, "EstimatedSize"),
                                "system_component": _dword(sk, "SystemComponent") == 1,
                            })
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def read_appx() -> list[dict]:
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPX_PATH) as root:
            n = winreg.QueryInfoKey(root)[0]
            for i in range(n):
                try:
                    full = winreg.EnumKey(root, i)
                    name_part = full.split("_")[0]
                    low = name_part.lower()
                    if any(h in low for h in APPX_FRAMEWORK_HINTS):
                        continue  # 框架包属碎片，不入台账
                    display = None
                    try:
                        with winreg.OpenKey(root, full) as sk:
                            display = _str(sk, "DisplayName")
                    except OSError:
                        pass
                    out.append({
                        "name": display or name_part,
                        "version": None,
                        "publisher": None,
                        "location": None,  # WindowsApps 目录受 ACL 保护，M2 不强求
                        "source": "appx",
                    })
                except OSError:
                    continue
    except OSError:
        pass
    # 同名去重（多版本/多架构）
    seen, dedup = set(), []
    for e in out:
        if e["name"] in seen:
            continue
        seen.add(e["name"])
        dedup.append(e)
    return dedup


def load_portable() -> list[dict]:
    if not PORTABLE_FILE.exists():
        return []
    try:
        raw = json.loads(PORTABLE_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    # 坏配置不该阻止引擎启动。只保留 build_entities 能安全消费的条目，
    # 其余内容交给下一次确认覆盖。
    return [item for item in raw
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].strip()]


def raw_icon_path(e: dict, fields=("display_icon", "uninstall_string")) -> str | None:
    """取图标源文件路径（exe/ico）。默认 DisplayIcon 优先、UninstallString 兜底（多为卸载器图标）。"""
    for f in fields:
        raw = e.get(f)
        if not raw:
            continue
        s = raw.strip()
        if s.lower().startswith("msiexec"):
            continue
        if "," in s and re.search(r",\s*\d+\s*$", s):
            s = s.split(",")[0].strip()
        if s.startswith('"') and '"' in s[1:]:
            s = s[1: s.index('"', 1)]
        else:
            s = s.split(" /")[0].strip()
        s = os.path.expandvars(s).strip().strip('"')
        if s.lower().endswith((".exe", ".ico")) and os.path.isfile(s):
            return s
    return None


# 下探时跳过的子目录（安装缓存/资源/语言包，里面的 exe 不能代表软件本体）
EXE_DIR_SKIP = re.compile(
    r"^(setup files|installer|cache|temp|tmp|logs?|locales?|resources|"
    r"redist\w*|prerequisites|drivers?|uninstall)$", re.I)


def _exe_score(path: str, size: int, name_tokens: set[str], depth: int, in_bin: bool) -> tuple:
    """打分：名字对得上 > 在 bin/ 里 > 层级浅 > 体积大。

    纯按体积取最大会选错（JDK 取到 jaccessinspector、Acrobat 取到更新器），
    所以名字匹配必须压过体积。
    """
    stem = Path(path).stem.lower()
    if stem in name_tokens:
        name_hit = 3                                             # java.exe ← Java(TM) SE...
    elif any(t.startswith(stem) or stem.startswith(t) for t in name_tokens if len(t) >= 4):
        name_hit = 2                                             # mysqld.exe ← MySQL Server
    else:
        name_hit = 0
    return (name_hit, 1 if in_bin else 0, -depth, size)


def first_exe(location: str | None, display_name: str | None = None) -> str | None:
    """本体目录里最能代表软件的 exe（跳过卸载器/更新器），作图标兜底源。

    顶层没有 exe 时下探两层：MySQL/JDK/CMake 这类是 <根>/bin/*.exe 布局，
    只扫顶层会一个都拿不到（实测 6 个实体因此丢图标）。
    """
    if not location or not os.path.isdir(location):
        return None
    name_tokens = {t for t in re.split(r"[^a-z0-9]+", (display_name or "").lower()) if t}

    cands: list[tuple[tuple, str]] = []

    def collect(d: str, depth: int, in_bin: bool) -> None:
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        subdirs = []
        for f in entries:
            try:
                if f.is_dir(follow_symlinks=False):
                    subdirs.append(f)
                    continue
                if not f.name.lower().endswith(".exe") or PORTABLE_EXE_SKIP.search(f.name):
                    continue
                sz = f.stat(follow_symlinks=False).st_size
                cands.append((_exe_score(f.path, sz, name_tokens, depth, in_bin), f.path))
            except OSError:
                continue
        if depth >= 2:
            return
        for sd in subdirs:
            if EXE_DIR_SKIP.search(sd.name):
                continue
            collect(sd.path, depth + 1, in_bin or sd.name.lower() == "bin")

    collect(location, 0, False)
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


def slug_of(entity_id: str) -> str:
    import hashlib

    return hashlib.md5(entity_id.encode()).hexdigest()[:10]


ICONS_DIR = _paths.icons_dir()


def icon_url(entity_id: str) -> str | None:
    """已提取出 PNG 才给 URL。否则返回 None，UI 直接走首字母，不白发 404。

    提取目标路径由 icon_slug 单独承载（见 extract_icons.missing_jobs），
    所以这里置 None 不会让后台提取漏掉该实体。
    """
    name = slug_of(entity_id) + ".png"
    return f"/icons/{name}" if (ICONS_DIR / name).is_file() else None


def build_entities() -> tuple[list[dict], dict]:
    """返回 (实体列表, 统计)。实体含主程序、发布商组件桶、便携软件。"""
    raw = read_registry()
    stats = {"registry_raw": len(raw)}
    mains, components = [], []
    for e in raw:
        loc, basis = infer_location(e)
        sz_kb = e["estimated_size_kb"] or 0
        est = sz_kb * 1024 if 0 < sz_kb * 1024 <= 64 * 2 ** 30 else None  # >64GiB 视为安装器写错单位
        item = {
            "name": e["name"], "version": e["version"], "publisher": e["publisher"],
            "pub_norm": norm_publisher(e["publisher"]),
            "location": loc, "location_basis": basis,
            "estimated_size": est,
            "icon_src": raw_icon_path(e, ("display_icon",)),
            "icon_src2": raw_icon_path(e, ("uninstall_string",)),
            "system_component": e["system_component"],
            "is_component": bool(e["system_component"] or COMPONENT_HINTS.search(e["name"])),
            "source": "registry",
        }
        (components if item["is_component"] else mains).append(item)

    # 主程序去重：同 发布商+归一化名 合并，保留信息最全的一条
    merged: dict[tuple, dict] = {}
    for m in mains:
        k = (m["pub_norm"], norm_token(m["name"]))
        if k in merged:
            old = merged[k]
            for f in ("location", "version", "estimated_size"):
                if not old.get(f):
                    old[f] = m.get(f)
        else:
            merged[k] = m

    entities = []
    for m in merged.values():
        # 图标源优先级：DisplayIcon → 本体目录里最大的 exe → UninstallString（多为卸载器，最后兜底）
        icon_src = m.get("icon_src") or first_exe(m["location"], m["name"]) or m.get("icon_src2")
        # 名称不是实体身份：不同发布商可以安装同名软件。发布商和名称都用
        # 与合并键一致的归一化值，保证同一实体稳定、不同实体不撞 id。
        eid = "r:" + (m["pub_norm"] or "unknown") + ":" + norm_token(m["name"])
        entities.append({
            "id": eid,
            "name": m["name"], "version": m["version"], "publisher": m["publisher"],
            "source": "registry", "location": m["location"],
            "location_basis": m["location_basis"],
            "location_exists": bool(m["location"] and os.path.isdir(m["location"])),
            "estimated_size": m["estimated_size"],
            "icon": icon_url(eid), "icon_slug": slug_of(eid), "icon_src": icon_src,
            "fragments": [], "portable": False,
        })

    # 碎片聚合：优先挂到 同发布商+本体同盘同根目录 的主程序，否则进发布商桶
    pub_buckets: dict[str, dict] = {}
    for c in components:
        loc = c["location"]
        host = None
        if loc and c["pub_norm"]:
            for m in entities:
                if m["source"] != "registry":
                    continue
                ml = m.get("location")
                if ml and norm_publisher(m["publisher"]) == c["pub_norm"] \
                        and ml[:3].lower() == loc[:3].lower() \
                        and os.path.dirname(ml).rstrip("\\/").lower() == os.path.dirname(loc).rstrip("\\/").lower():
                    host = m
                    break
        if host:
            host["fragments"].append(c["name"])
            if c["estimated_size"]:
                host["estimated_size"] = (host.get("estimated_size") or 0) + c["estimated_size"]
            continue
        bucket = pub_buckets.get(c["pub_norm"])
        if bucket is None:
            label = (c["publisher"] or "未知发布商") + "（组件聚合）"
            bucket = pub_buckets[c["pub_norm"]] = {
                "id": "p:" + (c["pub_norm"] or "unknown"), "name": label,
                "publisher": c["publisher"], "source": "publisher-bucket",
                "location": None, "location_basis": None, "location_exists": False,
                "estimated_size": 0, "fragments": [], "portable": False,
            }
        bucket["fragments"].append(c["name"] + (f" {c['version']}" if c["version"] else ""))
        bucket["estimated_size"] += c["estimated_size"] or 0
    entities.extend(pub_buckets.values())

    # Appx
    for a in read_appx():
        entities.append({
            "id": "a:" + a["name"], "name": a["name"], "version": None,
            "publisher": a["publisher"], "source": "appx",
            "location": a["location"], "location_basis": None,
            "location_exists": False, "estimated_size": None,
            "fragments": [], "portable": False,
        })

    # 便携软件
    for p in load_portable():
        eid = "u:" + p["name"]
        entities.append({
            "id": eid, "name": p["name"], "version": None,
            "publisher": None, "source": "portable",
            "location": p.get("dir"), "location_basis": "便携软件（用户确认）",
            "location_exists": bool(p.get("dir") and os.path.isdir(p["dir"])),
            "estimated_size": None, "exe_path": p.get("exe"),
            "icon": icon_url(eid), "icon_slug": slug_of(eid), "icon_src": p.get("exe"),
            "fragments": [], "portable": True,
        })

    entities.sort(key=lambda x: (-(x.get("estimated_size") or 0), x["name"].lower()))
    stats["entities"] = len(entities)
    stats["located"] = sum(1 for x in entities if x.get("location"))
    stats["portable"] = sum(1 for x in entities if x.get("portable"))
    stats["components"] = len(components)
    return entities, stats


def link_traces(entities: list[dict], trace_items: list[dict]) -> tuple[dict, list[dict]]:
    """把 v4 的 C 盘归因痕迹挂到台账实体；按归一化名 / 发布商匹配。"""
    # 只使用唯一键。重复名称/发布商时任意挑一个会把痕迹静默挂错实体，
    # 这种情况下宁可留在未挂接堆，也不把数据归给最后枚举到的那一项。
    names: dict[str, list[dict]] = {}
    by_pub_groups: dict[str, list[dict]] = {}
    for x in entities:
        name = norm_token(x.get("name", ""))
        if name:
            names.setdefault(name, []).append(x)
        pub = norm_publisher(x.get("publisher"))
        if pub:
            by_pub_groups.setdefault(pub, []).append(x)
    by_name = {k: v[0] for k, v in names.items() if len(v) == 1}
    by_pub = {k: v[0] for k, v in by_pub_groups.items() if len(v) == 1}

    unlinked = []
    for t in trace_items:
        owner = t.get("owner")
        target = None
        if owner:
            target = by_name.get(norm_token(owner)) or by_pub.get(norm_publisher(owner))
        if target is not None:
            target.setdefault("traces", []).append(t)
        else:
            unlinked.append(t)
    for x in entities:
        trs = x.get("traces", [])
        trs.sort(key=lambda t: t.get("size") or 0, reverse=True)
        x["traces_size"] = sum(t.get("size") or 0 for t in trs)
    return {x["id"]: x for x in entities}, unlinked


# 合成实体的 owner_kind 白名单：这些是真实软件，只是从不走"安装"流程，
# 注册表里永远没有条目（uv/Playwright/Electron 自建缓存目录）。
# 故意排除 container —— 容器体积由子目录承担，收进台账会重复计数。
SYNTH_KINDS = {"toolchain", "app_unregistered", "vendor", "app", "system"}


def synth_entities(unlinked: list[dict]) -> tuple[list[dict], list[dict]]:
    """把"归因成功但台账无实体"的痕迹合成为实体，返回 (合成实体, 仍未挂接)。

    这类 owner 挂不上不是匹配算法不够聪明：uv / Playwright / Electron 这些
    工具链从不注册卸载项，改 link_traces 的匹配规则也挂不上，只能补实体。

    vendor 单独标记：厂商目录（如 Roaming\\Tencent 下有 8 个不同产品）不等于
    单个软件，合成只为让体积在台账可见，不代表可整块处理。同一 owner 的痕迹里只要
    有一条是 vendor，整个实体就按 vendor 保守标记。

    **一个 owner 恰好一个实体**，见下面分组处那段注释——那是 id 唯一性的依据，
    回归测试在 `tests/test_link_traces.py`。
    """
    # 按 owner 分组，**不带 kind**。曾经按 (owner, kind) 分，而 id 只拼 "t:" + owner，
    # 于是同一个 owner 带两种 owner_kind 就产出两个 id 相同的实体——归因数据里这是真会
    # 发生的（`NVIDIA` 同时是 app 与 vendor），只是本机那条挂上了注册表实体、没走到合成。
    # 前端 `find(g => g.id === owner)` 先到先得，第二行点开看到的是第一行的数据。
    # 收成 owner 一个键之后，id 唯一性是**结构上**成立的，不靠"记得把 kind 拼进 id"。
    groups: dict[str, list[dict]] = {}
    rest = []
    for t in unlinked:
        owner, kind = t.get("owner"), t.get("owner_kind")
        if owner and kind in SYNTH_KINDS:
            groups.setdefault(owner, []).append(t)
        else:
            rest.append(t)

    out = []
    for owner, trs in groups.items():
        trs.sort(key=lambda t: t.get("size") or 0, reverse=True)
        redirects = [t["redirect"] for t in trs if t.get("redirect")]
        out.append({
            "id": "t:" + owner, "name": owner, "version": None,
            "publisher": None, "source": "trace",
            # 代表 kind 取体积最大那条痕迹的（trs 已降序）——那条最能说明这个实体是什么。
            # 痕迹各自的 owner_kind 一个字节没丢，计划器读的一直是痕迹自己那份。
            "owner_kind": trs[0].get("owner_kind"),
            "location": None, "location_basis": None, "location_exists": False,
            "estimated_size": None, "fragments": [], "portable": False,
            "icon": None, "icon_slug": None, "icon_src": None,
            "traces": trs,
            "traces_size": sum(t.get("size") or 0 for t in trs),
            # 官方重定向变量（UV_CACHE_DIR 等）存在时，M4 可改环境变量而非 junction
            "redirects": sorted(set(redirects)) or None,
            # 混进一条 vendor 就整体保守标记：厂商目录不等于单个软件，不能因为体积最大
            # 那条是 app 就宣称整块可处理。
            "shared_vendor": any(t.get("owner_kind") == "vendor" for t in trs),
        })
    out.sort(key=lambda x: -(x["traces_size"] or 0))
    return out, rest


# ---------- 便携软件发现 ----------

def scan_portable(root_path: str, max_depth: int = 2) -> list[dict]:
    """扫描目录下的 exe（限深度），按所在目录聚成候选。只读。"""
    candidates: dict[str, dict] = {}
    count = 0
    for cur, dirs, files in os.walk(root_path):
        depth = cur[len(root_path):].count(os.sep)
        if depth >= max_depth:
            dirs[:] = []
        # 剔除垃圾目录：保留"非垃圾"的
        junk_prefixes = (".", "$")
        junk_names = ("node_modules", "__pycache__", "windows", "system32")
        dirs[:] = [d for d in dirs
                   if not d.startswith(junk_prefixes) and d.lower() not in junk_names]
        for f in files:
            if not f.lower().endswith(".exe"):
                continue
            if PORTABLE_EXE_SKIP.search(f):
                continue
            full = os.path.join(cur, f)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            c = candidates.setdefault(cur, {"dir": cur, "size": 0, "exes": []})
            c["size"] += size
            c["exes"].append({"exe": full, "size": size})
            count += 1
            if count >= 500:
                dirs[:] = []
                break
        if count >= 500:
            break
    out = []
    for c in candidates.values():
        c["exes"].sort(key=lambda x: -x["size"])
        biggest = c["exes"][0]
        out.append({
            "name": os.path.basename(c["dir"].rstrip("\\/")) or biggest["exe"],
            "dir": c["dir"], "exe": biggest["exe"], "exe_count": len(c["exes"]),
            "size": c["size"],
        })
    out.sort(key=lambda x: -x["size"])
    return out


def save_portable(items: list[dict]) -> int:
    """确认入库：整体重写 portable.json（含去重）。写用户目录，非仓库。"""
    PORTABLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    merged = {p["name"]: p for p in load_portable()}
    for it in items:
        name = (it.get("name") or "").strip()
        if name and name not in merged:
            merged[name] = {"name": name, "dir": it.get("dir"), "exe": it.get("exe")}
    # 和快照/台账一样原子替换。直接 write_text 在进程中断时会留下半个 JSON，
    # 下一次启动就会把便携软件整块读坏。
    fd, tmp = tempfile.mkstemp(dir=str(PORTABLE_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(list(merged.values()), f, ensure_ascii=False, indent=1)
        os.replace(tmp, PORTABLE_FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return len(merged)


# ---------- 本体目录树（只读，带预算） ----------

def body_tree(root_path: str, max_entries: int = 4000, budget_s: float = 8.0):
    if not root_path or not os.path.isdir(root_path):
        return None
    deadline = time.time() + budget_s
    truncated = False

    def is_reparse(path: str) -> bool:
        if os.path.islink(path):
            return True
        try:
            return bool(os.lstat(path).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return False

    def walk(path: str, depth: int) -> dict:
        nonlocal truncated
        node = {"name": os.path.basename(path.rstrip("\\/")) or path, "path": path,
                "size": 0, "files": 0, "children": []}
        if depth > 8 or truncated or time.time() > deadline:
            truncated = truncated or time.time() > deadline
            return node
        try:
            entries = list(os.scandir(path))
        except OSError:
            return node
        for e in entries:
            try:
                if e.is_file(follow_symlinks=False):
                    if is_reparse(e.path):
                        continue
                    node["size"] += e.stat(follow_symlinks=False).st_size
                    node["files"] += 1
                elif e.is_dir(follow_symlinks=False):
                    if is_reparse(e.path):
                        continue
                    if len(node["children"]) < 60 and not truncated:
                        node["children"].append(walk(e.path, depth + 1))
                    else:
                        truncated = truncated or len(node["children"]) >= 60
            except OSError:
                continue
        node["children"].sort(key=lambda x: -x["size"])
        for ch in node["children"]:
            node["size"] += ch["size"]
            node["files"] += ch["files"]
        return node

    return walk(root_path, 0)
