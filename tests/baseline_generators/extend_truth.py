r"""扩充 truth.json：为 v4 新增的三类目录补基准标签（只读，除写 truth.json）。

背景：原基准集 37 条完全没覆盖 Temp / Packages / Package Cache / *-updater，
用它评判 v4 等于自欺（v4 的改动点全在基准集盲区）。

铁律不变：
  - 故意不 import lostpath_kb，基准集必须独立于被评判的引擎。
  - 只收「可独立程序化验证」的条目，拿不到硬证据的一律不标。
  - 只读取 attribution_v4.json 的 path / name / size 字段作为目录清单，
    绝不读它的 owner / kind（那是被评判对象的输出）。

新增证据来源：
  T5  TMP/TEMP 环境变量指向该目录       -> kind=system（系统临时区）
  T6  子目录形态证明该目录是多产品容器   -> kind=container
      （Appx 家族名 Publisher 后缀 / MSI ProductCode GUID / 每子目录自带 exe）
  T7  目录内安装包文件名含产品名         -> kind=app + owner=该产品名
"""
import io
import json
import os
import re
import sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
BASE = os.path.dirname(os.path.abspath(__file__))

TRUTH_PATH = os.path.join(BASE, "truth.json")
truth = json.load(open(TRUTH_PATH, encoding="utf-8"))
before = len(truth)

inv = json.load(open(os.path.join(BASE, "inventory.json"), encoding="utf-8"))

# 目录清单：只取 path / name / size 三个中性字段
dirs = []
for r in json.load(open(os.path.join(BASE, "attribution_v4.json"), encoding="utf-8")):
    p = r.get("path") or ""
    if p:
        dirs.append({"path": p, "name": os.path.basename(p), "size": r.get("size", 0)})

gib = lambda b: b / 2**30  # noqa: E731
added = []


def put(path, kind, owner, src, why):
    """写入基准。已存在的条目不覆盖（旧标注优先，避免自我修正污染）。"""
    k = path.lower()
    if k in truth:
        return False
    truth[k] = {"kind": kind, "owner": owner, "src": src, "why": why}
    added.append((path, kind, owner, src, why))
    return True


# ============================================================ T5 系统临时区
# 证据：TMP / TEMP 环境变量直接指向该目录。这是操作系统级声明，与任何
# 归因规则无关。Local\Temp 属于系统临时区，不属于任何单个软件。
tmp_targets = set()
envs = inv.get("envs") or {}
for scope, kv in envs.items():
    if not isinstance(kv, dict):
        continue
    for k, v in kv.items():
        if k.upper() in ("TMP", "TEMP") and isinstance(v, str) and v.strip():
            tmp_targets.add(os.path.normpath(os.path.expandvars(v)).lower())

print("=" * 100)
print("T5  TMP/TEMP 环境变量指向 -> kind=system")
print("=" * 100)
print(f"  环境变量目标：{sorted(tmp_targets)}")
for d in dirs:
    if os.path.normpath(d["path"]).lower() in tmp_targets:
        if put(d["path"], "system", None, "T5",
               "TMP/TEMP 环境变量直接指向，系统临时区"):
            print(f'  + {gib(d["size"]):6.2f} GiB  {d["path"]}')

# ============================================================ T6 多产品容器
# 证据：子目录本身就是彼此独立的产品标识，说明父目录只是收纳壳。
# 三种可独立识别的子目录形态：
#   A  Appx 家族名：<Name>_<Version?>_<Arch?>__<PublisherId>  （含 "__"）
#   B  MSI ProductCode：{8-4-4-4-12} GUID
#   C  每个子目录自带同名 exe（per-user 安装的应用各自成体）
APPX_RE = re.compile(r"^[^\\/]+__[0-9a-z]{8,}$", re.I)
GUID_RE = re.compile(r"^\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                     r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")

print()
print("=" * 100)
print("T6  子目录形态证明多产品容器 -> kind=container")
print("=" * 100)
for d in dirs:
    p = d["path"]
    try:
        kids = [e for e in os.scandir(p) if e.is_dir()]
    except OSError:
        continue
    if len(kids) < 3:
        continue
    appx = sum(1 for e in kids if APPX_RE.match(e.name))
    guid = sum(1 for e in kids if GUID_RE.match(e.name))
    exe = 0
    for e in kids[:40]:
        try:
            if any(f.name.lower().endswith(".exe") for f in os.scandir(e.path)
                   if f.is_file()):
                exe += 1
        except OSError:
            pass
    n = len(kids)
    form, cnt = None, 0
    if appx >= max(3, n * 0.6):
        form, cnt = "Appx 家族名（含 PublisherId）", appx
    elif guid >= max(3, n * 0.6):
        form, cnt = "MSI ProductCode GUID", guid
    elif exe >= max(3, min(n, 40) * 0.6):
        form, cnt = "各子目录自带 exe（独立 per-user 安装）", exe
    if not form:
        continue
    if put(p, "container", None, "T6",
           f"{cnt}/{n} 个子目录为{form}，父目录仅为容器"):
        print(f'  + {gib(d["size"]):6.2f} GiB  {p}')
        print(f'      {cnt}/{n} 子目录形态：{form}')

# ============================================================ T7 安装包文件名
# 证据：目录内安装包文件名含产品名（electron-updater 下载的升级包会保留
# 原始产品文件名）。剥掉版本号 / 架构 / setup 等通用词后剩下的就是产品名。
VER_RE = re.compile(
    r"[_\- ]?v?\d+(?:[._]\d+)+.*$|[_\- ](?:x64|x86|win32|win|amd64|arm64|"
    r"setup|installer|install|update|updater|full|portable|web)\b",
    re.I)
GENERIC = {"installer", "setup", "update", "updater", "install", "app",
           "temp", "tmp", "latest", "pending", "package", "bundle", "nsis"}
INST_RE = re.compile(r"\.(exe|msi)$", re.I)


def product_from_filename(fn):
    """从安装包文件名提取产品名，提不出返回 None。"""
    stem = os.path.splitext(fn)[0]
    prev = None
    while prev != stem:
        prev = stem
        stem = VER_RE.sub("", stem).strip(" _-.")
    if not stem or stem.lower() in GENERIC:
        return None
    if len(stem) < 3 or not re.search(r"[A-Za-z\u4e00-\u9fff]", stem):
        return None
    # 纯 hash / 纯数字形态不算产品名
    if re.fullmatch(r"[0-9a-f]{8,}", stem, re.I):
        return None
    return stem.replace("_", " ").replace("-", " ").strip()


print()
print("=" * 100)
print("T7  目录内安装包文件名含产品名 -> kind=app + owner")
print("=" * 100)
for d in dirs:
    p, nm = d["path"], d["name"]
    if not re.search(r"-updater$|updater$", nm, re.I):
        continue
    names = []
    for root, dnames, fnames in os.walk(p):
        if root.count("\\") - p.count("\\") > 2:
            dnames[:] = []
            continue
        names.extend(f for f in fnames if INST_RE.search(f))
        if len(names) > 60:
            break
    cands = [c for c in (product_from_filename(f) for f in names) if c]
    if not cands:
        print(f'  -   {gib(d["size"]):6.2f} GiB  {nm:<28} 仅通用文件名，不标注')
        continue
    top, hits = Counter(cands).most_common(1)[0]
    if put(p, "app", top, "T7",
           f"目录内安装包文件名 {hits} 次指向产品「{top}」"):
        print(f'  + {gib(d["size"]):6.2f} GiB  {nm:<28} -> {top}  (命中 {hits})')

# ============================================================ 落盘
json.dump(truth, open(TRUTH_PATH, "w", encoding="utf-8"),
          ensure_ascii=False, indent=1, sort_keys=True)

print()
print("=" * 100)
print(f"基准集 {before} -> {len(truth)} 条（新增 {len(added)}）")
print(f"  按来源：{dict(Counter(v['src'] for v in truth.values()))}")
print(f"  按类型：{dict(Counter(v['kind'] for v in truth.values()))}")
print(" written: truth.json")
