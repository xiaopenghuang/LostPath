r"""为 v1/v3 归因对比构建基准标签（只读）。

原则：只标注「可独立程序化验证」的条目，其余一律标 unverified 并排除出精度计算。
刻意不 import lostpath_kb，避免用归因引擎自己的推断规则去评判自己（循环论证）。

独立证据来源：
  T1  junction/符号链接目标 -> 已迁移到其它盘（客观事实，readlink 可验）
  T2  Packages 子目录名 == 已安装 Appx PackageFamilyName（精确相等，客观）
  T3  服务 / 启动项 exe 的安装路径分量 == 目录名（客观交叉引用）
  T4  本会话已逐条人工核验过的条目（标识文件、子目录清单实际看过）

输出 truth.json：{ 路径小写 : {kind, owner, cat, src} }
  kind: app / vendor / toolchain / container / system / unknown
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
BASE = os.path.dirname(os.path.abspath(__file__))
USER = os.path.expanduser("~")

inv = json.load(open(os.path.join(BASE, "inventory.json"), encoding="utf-8"))
scan = json.load(open(os.path.join(BASE, "scan_c.json"), encoding="utf-8"))
DIRS = {p: tuple(v) for p, v in scan["dirs"].items()}

apps = inv["apps"]


def nk(s):
    """归一化键：仅保留字母数字与中日韩字符，转小写。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (s or "").lower())


GENERIC = {
    nk(x) for x in [
        "Program Files", "Program Files (x86)", "Common Files", "bin", "AppData",
        "Local", "Roaming", "Users", "Windows", "System32", "app", "apps",
        "resources", "current", "Application", "x64", "x86", "lib",
    ]
}

truth = {}


def put(path, kind, owner, cat, src):
    """写入标签；已存在则保留更早（更强）的证据源，不覆盖。"""
    k = path.lower()
    if k not in truth:
        truth[k] = {"path": path, "kind": kind, "owner": owner,
                    "cat": cat, "src": src}


# ---------------------------------------------------------------- T1 junction
t1 = 0
for p in scan["reparse_points"]:
    tgt = None
    try:
        tgt = os.readlink(p)
    except (OSError, ValueError):
        tgt = None
    if not tgt:
        continue
    clean = tgt.replace("\\\\?\\", "").replace("\\??\\", "")
    m = re.match(r"^([A-Za-z]):", clean)
    if not m or m.group(1).upper() == "C":
        continue
    # 目标盘上找同根安装目录，确定归属软件
    owner = None
    for a in apps:
        loc = (a.get("installLocation") or "").strip().strip('"').rstrip("\\")
        if loc and clean.lower().startswith(loc.lower() + "\\"):
            owner = a["name"]
            break
    put(p, "app" if owner else "relocated", owner, "已迁移",
        f"T1 junction -> {clean}")
    t1 += 1

# ---------------------------------------------------------------- T2 Appx
fam_idx = {}
for pkg in inv["appx"]:
    fam = pkg.get("family")
    if fam:
        fam_idx[fam.lower()] = pkg
pkg_root = os.path.join(USER, r"AppData\Local\Packages")
t2 = 0
for p in DIRS:
    parent, name = os.path.split(p)
    if parent.lower() != pkg_root.lower():
        continue
    hit = fam_idx.get(name.lower())
    if hit:
        put(p, "app", hit["name"], "混合", f"T2 Appx family == {name}")
        t2 += 1
put(pkg_root, "container", None, "混合", "T2 Appx 容器目录（非单一软件）")

# ------------------------------------------------------- T3 服务/启动项交叉引用
exe_list = []
for s in inv["services"]:
    if s.get("exe"):
        exe_list.append(s["exe"])
for r in inv["startup"]:
    if r.get("exe"):
        exe_list.append(r["exe"])

part_owner = {}
for exe in exe_list:
    owner = None
    for a in apps:
        loc = (a.get("installLocation") or "").strip().strip('"').rstrip("\\")
        if loc and exe.lower().startswith(loc.lower() + "\\"):
            owner = a["name"]
            break
    for part in exe.split("\\")[1:-1]:
        key = nk(part)
        if key and key not in GENERIC:
            part_owner.setdefault(key, (part, owner))

FOOTPRINT_ROOTS = [
    (os.path.join(USER, r"AppData\Local"), "LocalAppData"),
    (os.path.join(USER, r"AppData\Roaming"), "RoamingAppData"),
    (os.path.join(USER, r"AppData\LocalLow"), "LocalLow"),
    (r"C:\ProgramData", "ProgramData"),
]
t3 = 0
for root, _zone in FOOTPRINT_ROOTS:
    try:
        with os.scandir(root) as it:
            kids = [e for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        continue
    for e in kids:
        if DIRS.get(e.path, (0, 0))[0] < 20 * 1024 * 1024:
            continue
        hit = part_owner.get(nk(e.name))
        if hit and hit[1]:
            put(e.path, "app", hit[1], "混合",
                f"T3 服务/启动项 exe 路径含分量 {hit[0]}")
            t3 += 1

# ---------------------------------------------------------------- T4 人工核验
L = os.path.join(USER, "AppData", "Local")
R = os.path.join(USER, "AppData", "Roaming")
MANUAL = [
    # (路径, kind, owner, cat, 核验依据)
    (os.path.join(R, "Code"), "app", "Microsoft Visual Studio Code (User)",
     "混合", "T4 实测 User\\settings.json 存在；安装于 G:\\VSCODE"),
    (os.path.join(R, "Cursor"), "app", "Cursor (User)", "混合",
     "T4 注册表 Cursor (User) 安装于 G:\\Cursor"),
    (os.path.join(R, "Trae"), "app", "Trae (User)", "混合",
     "T4 注册表 Trae (User) 安装于 G:\\Trae"),
    (os.path.join(R, "Windsurf"), "app", "Windsurf (User)", "混合",
     "T4 注册表 Windsurf (User) 安装于 G:\\Windsurf"),
    (os.path.join(R, "Kiro"), "app", "Kiro (User)", "混合",
     "T4 注册表 Kiro (User) 安装于 G:\\Kiro"),
    (os.path.join(R, "Tencent"), "vendor", "腾讯", "混合",
     "T4 实测下含 xwechat/WeChat/WeMeet/WeGame/QQMusic 等多产品"),
    (os.path.join(L, "Microsoft"), "vendor", "Microsoft", "混合",
     "T4 注册表 Microsoft 名下 121 个软件"),
    (os.path.join(L, "NVIDIA"), "vendor", "NVIDIA", "混合",
     "T4 注册表 NVIDIA 名下 77 个软件"),
    (os.path.join(L, "NVIDIA Corporation"), "vendor", "NVIDIA", "混合",
     "T4 NVIDIA 变体目录"),
    (os.path.join(L, "ms-playwright"), "toolchain", "Playwright 浏览器二进制",
     "可再生缓存", "T4 实测 2.23 GiB 浏览器二进制"),
    (os.path.join(L, "ms-playwright-mcp"), "toolchain", "Playwright MCP 缓存",
     "可再生缓存", "T4 实测 0.47 GiB"),
    (os.path.join(L, "uv"), "toolchain", "uv (Python) 缓存", "可再生缓存",
     "T4 实测 1.57 GiB uv 缓存"),
    (os.path.join(L, "Temp"), "system", "Windows 用户临时目录", "可清理",
     "T4 用户 TEMP 环境变量指向此处"),
    (os.path.join(L, "D3DSCache"), "system", "D3D 着色器缓存", "可再生缓存",
     "T4 系统着色器缓存"),
    (os.path.join(L, "ForzaHorizon4"), "app", None, "用户数据",
     "T4 实测为游戏存档，注册表无对应条目（Xbox/MS Store 游戏）"),
]
t4 = 0
for path, kind, owner, cat, src in MANUAL:
    if path in DIRS:
        put(path, kind, owner, cat, src)
        t4 += 1

# ---------------------------------------------------------------- 输出
json.dump(truth, open(os.path.join(BASE, "truth.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

print("=" * 96)
print("基准标签构建结果")
print("=" * 96)
print(f"  T1 junction 已迁移      : {t1}")
print(f"  T2 Appx 家族精确匹配    : {t2}")
print(f"  T3 服务/启动项交叉引用  : {t3}")
print(f"  T4 人工核验             : {t4}")
print(f"  合计标签                : {len(truth)}")

by_kind = {}
for v in truth.values():
    by_kind[v["kind"]] = by_kind.get(v["kind"], 0) + 1
print("\n  按类型：")
for k, c in sorted(by_kind.items(), key=lambda x: -x[1]):
    print(f"    {k:<12} {c}")

# 只统计落在足迹根下的标签（这些才参与 v1/v3 对比）
roots = [r.lower() for r, _ in FOOTPRINT_ROOTS]
top = []
for v in truth.values():
    p = v["path"]
    parent = os.path.dirname(p).lower()
    if parent in roots:
        sz = DIRS.get(p, (0, 0))[0]
        top.append((sz, v))
top.sort(key=lambda x: -x[0])
print(f"\n  其中足迹顶层目录标签: {len(top)} 个, "
      f"合计 {sum(s for s, _ in top) / 2**30:.2f} GiB")
print(f"\n  {'GiB':>7}  {'目录':<28} {'kind':<10} {'owner':<34} src")
print("  " + "-" * 92)
for sz, v in top[:26]:
    print(f"  {sz / 2**30:7.2f}  {os.path.basename(v['path'])[:28]:<28} "
          f"{v['kind']:<10} {str(v['owner'])[:34]:<34} {v['src'][:40]}")
print("\n written: truth.json")
