r"""迁移/清理计划器。**全程只读，不碰任何文件。**

产出一份可审计的计划：做什么、动哪些文件、腾出多少、有什么拦阻、怎么回滚。执行器
只认这份计划，不自己判断——把"决定"与"动手"分开，才能在动手之前把决定看清楚。

三种动作，按风险从低到高：

    redirect  改一个环境变量让软件自己去新位置。**不搬旧文件**，旧缓存作为可清理项
              单独列出。风险最低：改错了把变量删掉即恢复。
    cleanup   删除可再生缓存。风险中等但可回滚（先移到回收区，过期才真删）。
    junction  复制到新盘 → 建 junction → 校验 → 删源。风险最高，放最后做。

**拦阻检查比候选列表更重要。** 这个工具的用户是非程序员，他会信任界面给的建议。
所以宁可把能动的判成不能动（他损失一次优化机会），也不能把不能动的判成能动（他丢
数据）。当前会拦下：目录不存在、体积过小不值当、性质未定性或不可动、目录已是重解析
点、归属是系统、置信度不足、目标盘容量不够、有进程正在占用。
"""
from __future__ import annotations

import ctypes
import ntpath
import os
from dataclasses import dataclass, field

from . import envvar as envvar_mod
from . import redirect as redirect_mod
from . import target_root as target_root_mod
from .. import sysdirs

# 门槛：小目录搬来搬去的收益抵不过风险与用户的注意力成本
MIN_ACTIONABLE_SIZE = 50 * 1024 * 1024
# 归因置信度低于此值不进候选：判错归属会把别的软件的数据搬走
MIN_CONFIDENCE = 0.60
# 目标盘至少要留这么多余量，不能刚好填满
TARGET_FREE_MARGIN = 2 * 1024 * 1024 * 1024

# junction 迁移的体积门槛。比清理的 50 MiB 高一个量级：搬运要真复制一遍数据、
# 且给软件引入了一层重解析点，小目录不值得担这个风险。
JUNCTION_MIN_SIZE = 500 * 1024 * 1024

CLEANABLE_CATS = {"可再生缓存", "可清理"}
# 归属这些类型的目录不做自动处理：容器体积由子目录承担（处理容器会重复计数），
# 系统与厂商共享目录影响面超出单个软件
BLOCKED_OWNER_KINDS = {"container", "system", "vendor"}
# 这两条的理由都是"别整块动，应逐个子目录处理"，所以对子目录级计划不成立——
# 子目录计划正是它们要求的做法。实测：Roaming\Tencent\Logs 明确定性为可再生缓存，
# 却因继承父目录的 vendor 归属被"厂商共享目录不可整块处理"挡住。
# system 不在此列：系统归属的风险与层级无关，照拦。
PARENT_LEVEL_BLOCKED_KINDS = {"container", "vendor"}


@dataclass
class Blocker:
    """一条拦阻原因。code 供程序判断，reason 给人看。"""
    code: str
    reason: str


@dataclass
class Plan:
    """一份执行计划。executable=False 时 blockers 说明为什么。"""
    path: str
    name: str
    action: str                      # redirect / cleanup / junction / none
    size: int
    files: int
    owner: str | None
    owner_kind: str | None
    cat: str | None
    confidence: float
    reclaimable: int                 # 预计能腾出的 C 盘空间
    steps: list[dict] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    target: str | None = None
    env_var: str | None = None
    redirect_mechanism: dict | None = None
    notes: list[str] = field(default_factory=list)
    # 子目录级计划：父目录整块不可动，但它下面某个子目录本身是缓存。
    # parent_path 非空即表示本条是子级，界面据此缩进显示、也便于去重核查。
    parent_path: str | None = None

    @property
    def executable(self) -> bool:
        return not self.blockers and self.action != "none"

    def to_dict(self) -> dict:
        return {
            "path": self.path, "name": self.name, "action": self.action,
            "size": self.size, "files": self.files, "owner": self.owner,
            "owner_kind": self.owner_kind, "cat": self.cat,
            "confidence": round(self.confidence, 3),
            "reclaimable": self.reclaimable, "target": self.target,
            "env_var": self.env_var,
            "redirect_mechanism": self.redirect_mechanism,
            "steps": self.steps, "notes": self.notes,
            "blockers": [{"code": b.code, "reason": b.reason} for b in self.blockers],
            "executable": self.executable,
            "parent_path": self.parent_path,
        }


# ------------------------------------------------------------------ 磁盘容量
def drive_free_bytes(path: str) -> int | None:
    """取路径所在盘的可用空间。拿不到返回 None（调用方须当作"未知"而非"充足"）。"""
    try:
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if not drive:
            return None
        free = ctypes.c_ulonglong()
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            drive + "\\", None, None, ctypes.byref(free))
        return free.value if ok else None
    except (OSError, AttributeError):
        return None


def is_reparse_point(path: str) -> bool:
    """已是 junction/符号链接的目录不能再迁移——它的体积本就不在这个盘上。"""
    if os.path.islink(path):
        return True
    try:
        return bool(os.lstat(path).st_file_attributes & 0x400)
    except (OSError, AttributeError, ValueError):
        return False


def _running_under(path: str, pdirs: set[str]) -> str | None:
    r"""pdirs 里有没有哪个进程目录**等于 path 或落在 path 之下**；有就返回那一个。

    必须判祖先，不能只判相等。真实故障：计划路径是
    `C:\Users\<user>\AppData\Local\uv`，而正在跑的解释器在
    `...\uv\cache\archive-v0\<hash>\Scripts\`——它是前者的**子孙**，`in` 判假，
    于是"软件正在运行"这道闸门整个失守，1.6 GB 缓存被判为可执行，执行时删到一半
    撞上被加载的 DLL 而失败。原实现的提示语写的是"就在该目录下"，本就是包含语义，
    只是代码只做了相等比较。

    比较前统一去掉末尾反斜杠：`C:\x\` 与 `C:\x` 是同一个目录，而盘根
    （`c:\`）去掉后是 `c:`，用 `startswith(path + sep)` 仍能正确匹配其下内容。
    pdirs 由 winproc.running_process_dirs() 给出，已全部小写。
    """
    if not pdirs:
        return None
    target = path.lower().rstrip("\\")
    if not target:
        return None
    prefix = target + "\\"
    for d in pdirs:
        cur = d.rstrip("\\")
        if cur == target or cur.startswith(prefix):
            return d
    return None


def running_process_dirs() -> set[str]:
    """正在运行的进程的可执行文件所在目录（小写）。

    用于判断"软件是否正在运行"。只读进程列表，不去试探文件锁——试探性地独占打开
    文件本身就是副作用，而 Windows 也不允许独占打开目录。

    **实现搬到 `lostpath/winproc.py` 走 Win32 API 了。** 原先这里起 PowerShell 跑
    `Get-Process`，实测 **4.35 秒**——而 `/api/plan` 每次都要它，于是软件台账与
    迁移中心每次打开都卡四秒多，用户反馈"有点卡卡的"。换成
    `CreateToolhelp32Snapshot` 后 **21.7 毫秒**，快约 200 倍，拿到的目录还更多
    （76 vs 74，PowerShell 版一个进程取 .Path 抛异常就把整条管道的结果一起吞了）。

    拿不到就返回空集合，等于"没有软件在跑"——与旧版异常兜底时的行为一致。
    """
    try:
        from lostpath import winproc
        return winproc.running_process_dirs()
    except Exception:
        # 拿不到就当作"未知"，交给 blockers 里的提示，不阻塞出计划
        return set()


# 已设置状态的三种取值。函数名以外还要有这层命名，是因为下面两处分支的语义差别
# 很容易在阅读时被当成同一件事：
#   "unset"        变量没设过 —— 按原逻辑提议设置
#   "redirected"   已设且指向源盘之外 —— 用户自己做过了，我们只清残留，绝不再设
#   "same_drive"   已设但仍在源盘上 —— 是用户的明确选择，不静默覆盖，交回给人
ENV_UNSET = "unset"
ENV_REDIRECTED = "redirected"
ENV_SAME_DRIVE = "same_drive"


def _same_drive(a: str, b: str) -> bool:
    return (os.path.splitdrive(os.path.abspath(a))[0].lower()
            == os.path.splitdrive(os.path.abspath(b))[0].lower())


def env_var_state(var: str, source_path: str,
                  env_lookup=None) -> tuple[str, str | None, str | None]:
    r"""这个环境变量当前是什么状态。返回 (state, value, scope)。

    **为什么出计划前必须问这一句。** 原先从不读现值，于是对一台已经把
    `UV_CACHE_DIR` 设到 `G:\UV\uvcache` 的机器，计划器照样提议"设置
    UV_CACHE_DIR = <本工具的目标盘>"。真执行的话：写进 HKCU 的值会盖掉 HKLM 那份
    （用户级优先），软件转而用本工具选的位置，而用户原来那份缓存（实测 16.10 GiB）
    立刻变成谁都不读的孤儿——它既不在 C 盘所以本工具扫不到，也不再被软件使用。
    界面上显示的是"腾出 1.57 GiB 成功"。

    这类错误执行完之后极难发现，所以判定必须发生在出计划阶段。

    env_lookup 可注入是为了可测：快套件不得读真注册表——开发机上
    `UV_CACHE_DIR` 恰好是设过的，直接读会让同一条测试在不同机器上得出不同结论。
    """
    lookup = env_lookup or envvar_mod.effective_var
    value, scope = lookup(var)
    if value is None or not str(value).strip():
        # 空字符串按未设处理：软件读到空值会退回自己的默认位置，与没设一样
        return ENV_UNSET, value, scope
    if _same_drive(value, source_path):
        return ENV_SAME_DRIVE, value, scope
    return ENV_REDIRECTED, value, scope


# ------------------------------------------------------------------ 计划构建
def plan_for(record: dict, target_root: str | None = None,
             process_dirs: set[str] | None = None,
             as_child: bool = False, env_lookup=None) -> Plan:
    """为一条痕迹记录出计划。record 是快照里的一项（归因输出）。

    as_child=True 表示这是"父目录整块不可动、单独处理它下面某个子目录"的计划。
    此时不施加 container / vendor 两条归属拦阻——它们的理由本就是"应逐个子目录
    处理"，而这条计划正是那个做法。必须在这里就不加，不能事后摘：下面有
    `if p.blockers: return p`，带着拦阻走到那儿就再也算不出动作了。
    """
    path = record.get("path") or ""
    size = int(record.get("size") or 0)
    cat = record.get("cat")
    owner_kind = record.get("owner_kind")
    hint = record.get("redirect")
    mech = redirect_mod.resolve(hint)

    p = Plan(
        path=path, name=record.get("name") or os.path.basename(path),
        action="none", size=size, files=int(record.get("files") or 0),
        owner=record.get("owner"), owner_kind=owner_kind, cat=cat,
        confidence=float(record.get("conf") or 0.0), reclaimable=0,
        redirect_mechanism=mech,
    )

    # ---------------- 先判拦阻，拦住的不给动作 ----------------
    if not path or not os.path.isdir(path):
        p.blockers.append(Blocker("missing", "目录已不存在，快照可能已过期，建议重扫"))
        return p
    if is_reparse_point(path):
        p.blockers.append(Blocker(
            "already_linked", "该目录已是 junction/符号链接，体积并不在本盘上"))
        return p
    if size < MIN_ACTIONABLE_SIZE:
        p.blockers.append(Blocker(
            "too_small",
            f"仅 {size / 2**20:.0f} MiB，低于 {MIN_ACTIONABLE_SIZE // 2**20} MiB 门槛，"
            f"折腾的风险大于收益"))
    if owner_kind in BLOCKED_OWNER_KINDS and not (
            as_child and owner_kind in PARENT_LEVEL_BLOCKED_KINDS):
        why = {
            "container": "这是容器目录，体积由各子目录承担，应逐个子目录处理",
            "system": "归属系统而非某个软件，影响面超出单个应用",
            "vendor": "厂商共享目录，下面是多个产品，不可整块处理",
        }[owner_kind]
        p.blockers.append(Blocker(f"owner_{owner_kind}", why))
    # junction 的服务对象**正是**这批"不可清理"的目录，所以它们不能被 not_cleanable
    # 挡在门外：那条拦阻的理由是"不删不确定性质的东西"，而 junction 一个字节都不删，
    # 只换存放位置。判定要提前算，因为下面 `if p.blockers: return p` 一旦触发就再也
    # 走不到动作选择那一步——最初写成事后判断，结果 junction 计划恒为 0 条。
    will_junction = (cat not in CLEANABLE_CATS
                     and not (mech and mech["kind"] in ("env", "manual"))
                     and _junction_worthy(record, size, cat))
    if cat not in CLEANABLE_CATS and not will_junction:
        p.blockers.append(Blocker(
            "not_cleanable",
            f"定性为「{cat or '未定性'}」。只处理可再生缓存与可清理项；"
            f"未定性的先补知识库规则，不靠猜"))
    if p.confidence < MIN_CONFIDENCE:
        p.blockers.append(Blocker(
            "low_confidence",
            f"归因置信度仅 {p.confidence:.0%}，低于 {MIN_CONFIDENCE:.0%}。"
            f"判错归属会动到别的软件的数据"))
    if mech and mech["kind"] == "unsafe":
        p.blockers.append(Blocker("unsafe_redirect", mech["note"]))

    # 变量已被设过？必须在这里判，不能事后摘——下面 `if p.blockers: return p` 一旦
    # 触发就再也走不到动作选择那一步（和上面 junction 那条踩的是同一个坑）。
    env_state, env_value, env_scope = ENV_UNSET, None, None
    if mech and mech["kind"] == "env":
        env_state, env_value, env_scope = env_var_state(
            mech["var"], path, env_lookup)
        if env_state == ENV_SAME_DRIVE:
            where = {"user": "用户级", "machine": "系统级"}.get(env_scope, "")
            p.blockers.append(Blocker(
                "env_var_already_set",
                f"{mech['var']} 已被设为「{env_value}」（{where}），而它仍在本盘上。"
                f"这是你自己的设置，本工具不静默覆盖——请先决定是保留它还是改成"
                f"别的盘"))

    # 软件正在运行时不动它的文件
    pdirs = process_dirs if process_dirs is not None else set()
    running = _running_under(path, pdirs)
    if running:
        where = "就在该目录下" if running.rstrip("\\") == path.lower().rstrip("\\") \
            else f"在其子目录 {running} 下"
        p.blockers.append(Blocker(
            "in_use", f"有进程的可执行文件{where}，说明正在运行"))

    if p.blockers:
        return p

    # ---------------- 选动作：能改环境变量就不搬文件 ----------------
    # ENV_REDIRECTED 时**不走 redirect**：重定向这件事用户自己做过了，再设一次只会
    # 把他选的位置换成我们的，让他原来那份缓存变成谁都不读的孤儿。此时该做的只是
    # 清掉旧位置的残留，所以这里把 env_auto 判成假，让它落到最后的 cleanup 分支。
    #
    # 落到 cleanup 是安全的，不必额外判断：能走到这里说明 cat 必在 CLEANABLE_CATS
    # ——mech 是 env 时 will_junction 恒为假，若 cat 不可清理，not_cleanable 早已在
    # 上面加上并 return 了。
    env_auto = bool(mech and mech["kind"] == "env"
                    and env_state != ENV_REDIRECTED)
    if env_auto:
        p.action = "redirect"
        p.env_var = mech["var"]
        p.target = mirror_target(root_for_path(path, target_root), path)
        # 改变量不搬旧文件，所以腾出的空间来自随后清理旧缓存
        p.reclaimable = size
        p.steps = [
            {"n": 1, "title": "设置用户级环境变量",
             "detail": f"{mech['var']} = {p.target}",
             "reversible": "删除该变量即恢复原状"},
            {"n": 2, "title": "清理旧缓存",
             "detail": f"删除 {path}（{size / 2**30:.2f} GiB），软件下次运行会在新位置重建",
             "reversible": "删除前移入回收区，回滚清单记录原路径"},
        ]
        # mech["note"] 不放进 notes：它随 redirect_mechanism 一起给了调用方，
        # 重复放会让界面把同一句话显示两遍。
        p.notes = [
            "不复制旧文件：这是可再生缓存，让软件在新位置重新下载比搬运更可靠，"
            "也避免搬运过程中损坏",
            "环境变量只对新启动的进程生效，已在运行的程序需重启",
        ]
    elif mech and mech["kind"] == "manual":
        p.action = "none"
        p.blockers.append(Blocker(
            "manual_redirect",
            f"该软件的官方做法不是环境变量，需手动执行：{mech.get('how', hint)}"))
        p.notes = [mech["note"]]
        return p
    elif will_junction:
        # 不能删、又没有官方重定向机制：搬到别的盘，原位留 junction。
        # 数据一字不少地保留，代价是多占一次复制时间，且个别软件不认重解析点。
        p.action = "junction"
        p.target = mirror_target(root_for_path(path, target_root), path)
        p.reclaimable = size
        p.steps = [
            {"n": 1, "title": "复制到新盘",
             "detail": f"{path} → {p.target}（{size / 2**30:.2f} GiB，大目录会花几分钟）",
             "reversible": "复制阶段失败源目录一动没动"},
            {"n": 2, "title": "比对文件数与字节数",
             "detail": "两边完全一致才继续，否则就地中止",
             "reversible": "校验不过不会动源目录"},
            {"n": 3, "title": "源目录移入回收区",
             "detail": "不是删除；30 天内可整份放回原位",
             "reversible": "回滚即移回原路径"},
            {"n": 4, "title": "原位建 junction",
             "detail": f"{path} → {p.target}，软件仍按老路径访问",
             "reversible": "回滚会摘掉链接再还原数据"},
        ]
        p.notes = [
            f"定性「{cat or '未定性'}」：不做删除，只改存放位置，数据完整保留",
            "少数软件不认 junction（尤其带自校验或反作弊的），出问题就回滚",
            "跨盘复制期间请勿运行该软件",
        ]
    else:
        # 无官方重定向、或官方变量已由用户自己设到别的盘：可再生缓存直接清理
        # （比 junction 风险低一个量级）
        p.action = "cleanup"
        p.reclaimable = size
        p.steps = [
            {"n": 1, "title": "移入回收区",
             "detail": f"{path} → 回收区（同盘移动，秒级完成）",
             "reversible": "回滚即移回原路径"},
            {"n": 2, "title": "校验并登记",
             "detail": "写回滚清单（原路径、文件数、体积、可恢复期）",
             "reversible": "—"},
            {"n": 3, "title": "过期后真删",
             "detail": "回收期内不真删，超期由用户确认或自动清空",
             "reversible": "真删后不可恢复"},
        ]
        p.notes = [
            f"定性「{cat}」：{record.get('why') or '知识库规则判定'}",
            "可再生缓存删掉后软件会自行重建，代价是下次启动稍慢",
        ]
        if env_state == ENV_REDIRECTED:
            where = {"user": "用户级", "machine": "系统级"}.get(env_scope, "")
            p.notes.insert(0, (
                f"{mech['var']} 已由你设为「{env_value}」（{where}），软件已经在用新"
                f"位置，所以本工具不动环境变量，只清掉旧位置遗留的这份"))

    # ---------------- 目标盘容量检查（仅搬运类动作需要）----------------
    if p.action in ("redirect", "junction") and p.target:
        free = drive_free_bytes(p.target)
        # junction 要真把数据复制过去，所以需要的是"目录体积 + 安全余量"；
        # redirect 不搬旧文件，只要目标盘别本来就快满。
        need = TARGET_FREE_MARGIN + (size if p.action == "junction" else 0)
        if free is None:
            p.notes.append("无法读取目标盘可用空间，执行前请自行确认容量")
        elif free < need:
            p.blockers.append(Blocker(
                "target_full",
                f"目标盘可用 {free / 2**30:.1f} GiB，"
                f"本次需要 {need / 2**30:.1f} GiB"
                + (f"（{size / 2**30:.1f} GiB 数据 + "
                   f"{TARGET_FREE_MARGIN // 2**30} GiB 安全余量）"
                   if p.action == "junction" else
                   f"（{TARGET_FREE_MARGIN // 2**30} GiB 安全余量）")))
    return p


def _junction_worthy(record: dict, size: int, cat: str | None) -> bool:
    r"""这个目录值不值得用 junction 搬走。

    junction 的定位是"不能删但能搬"的兜底，所以只在没有更轻的手段时才提。判据：

    * 定性不是"不可动"。那张表里的目录（Windows\Installer、WinSxS、DriverStore、
      Package Cache）已知会因重解析点而修复/卸载失败，搬了等于弄坏。
    * 归属明确到某个软件。厂商共享目录与容器由上层拦阻处理，系统目录不碰。
    * 体积够大。搬运要真复制一遍，小目录折腾的风险大于收益，门槛比清理高不少。
    """
    if cat == "不可动":
        return False
    if record.get("owner_kind") != "app":
        return False
    if not record.get("owner"):
        return False
    return size >= JUNCTION_MIN_SIZE


def default_target_root() -> str:
    """默认目标根目录：挑一个非 C 盘、可用空间最大的固定盘。

    只做建议，用户可改。挑最大剩余而非固定盘符，是因为每台机器盘符布局不同。
    """
    best, best_free = None, -1
    system_drive = sysdirs.system_drive().upper()
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if not (bitmask >> i) & 1:
            continue
        letter = chr(ord("A") + i) + ":"
        if letter == system_drive:
            continue
        if ctypes.windll.kernel32.GetDriveTypeW(letter + "\\") != 3:  # DRIVE_FIXED
            continue
        free = drive_free_bytes(letter + "\\")
        if free is not None and free > best_free:
            best, best_free = letter, free
    # 必须带分隔符：os.path.join("E:", x) 得到 "E:x"，那是**盘符相对路径**（相对于
    # 进程在 E: 上的当前目录），不是 "E:\x"。它在当前目录恰为根时看起来是对的，
    # 换个工作目录就会写到别处——对一个搬用户数据的工具来说是事故。
    return os.path.join((best or system_drive) + os.sep, "LostPathStore")


def root_for_path(path: str, fallback: str | None = None) -> str:
    r"""这个源路径该用哪个目标根。优先级从高到低：

      1. 该路径的**逐项覆盖**（用户为它单独指定过）
      2. 调用方传进来的 `fallback`（一次性指定，如 /api/act/execute 带的值）
      3. 全局保存的根
      4. 自动挑

    逐项覆盖排在 fallback 之前是刻意的：它是用户针对这一条做的、且已落盘的决定，
    比"这次请求顺带带上的全局值"更具体。

    **出计划与执行必须都走这个函数。** executor 会重新 plan_for 一遍，若两边解析
    规则不同，就会出现"界面显示要搬到 G，实际搬到 E"——那比不灵活危险得多。
    """
    ov = target_root_mod.override_for(path)
    if ov:
        return ov
    return fallback or effective_target_root()


def effective_target_root() -> str:
    r"""实际要用的目标根：用户在界面上设过就用他设的，否则自动挑。

    与 `default_target_root()` 分成两个函数、而不是让后者去读配置，有两个理由：

    * 名字得说真话。`default_` 返回"用户自定义的值"会误导所有调用方。
    * `test_default_target_root_is_absolute` 盯的是自动挑那段逻辑。让它读配置，
      在设过配置的机器上那条断言就测不到被测代码了——正是"靠巧合绿"那一族。

    `target_root_mod.effective()` 每次都重新校验存的值，盘拔了/盘符变了会返回
    None，于是这里静默回落到自动挑。**静默是刻意的**：出计划是只读操作，此刻弹错误
    没有用户能采取的动作；界面另有 `/api/target-root` 明确告知失效状态。
    """
    return target_root_mod.effective() or default_target_root()


def _safe_name(name: str) -> str:
    """目录名转成可安全用于路径的形式。"""
    out = "".join("_" if c in '\\/:*?"<>|' else c for c in name)
    return out.strip(". ") or "unnamed"


def _under(path: str, base: str) -> bool:
    r"""path 是否在 base 之内。

    **比较必须带上分隔符。** 裸 `startswith` 会把 `C:\Users\10\...` 判成
    `C:\Users\1` 的子目录（实测确认），于是别的用户的目录会被按本用户的相对
    路径去镜像，算出来的目标位置完全不对。
    """
    if not path or not base:
        return False
    a = ntpath.normpath(path).lower().rstrip("\\")
    b = ntpath.normpath(base).lower().rstrip("\\")
    return a == b or a.startswith(b + "\\")


def mirror_suffix(path: str, user_home: str | None = None) -> str:
    r"""把源路径转成"挂到目标根下面"的相对后缀，保留原有层级。

        C:\Users\1\AppData\Local\ms-playwright  ->  AppData\Local\ms-playwright
        C:\ProgramData\Adobe                    ->  ProgramData\Adobe

    配上根 `G:\1` 就得到 `G:\1\AppData\Local\ms-playwright`——新盘看起来是用户
    目录的镜像，一眼能看出东西原本在哪。

    **为什么不用软件显示名当叶子**（原先是 `_safe_name(p.name)`）：显示名会重复。
    基准数据里 106 条有 16 个重名，NVIDIA 一家占 6 条，全都算出同一个
    `<root>\NVIDIA`。后果分两种：junction 撞 junction 被 executor 以"目标目录
    已存在且非空"拒绝（安全但提示看不出根因）；redirect 撞 redirect **不拦**
    （executor 只 mkdir(exist_ok=True)），两个软件的环境变量指到同一个目录、
    缓存互相覆盖。源路径天然唯一，用它做后缀一次消掉这一整类问题，不必再单独
    写一套撞名检测。

    `user_home` 可注入：基准 fixture 是别的机器扫的，用户名不同。直接读本机
    HOME 会让断言只在开发机成立——`attribute_v4.py` 与 `collect_evidence.py`
    早就是这个模式。
    """
    home = user_home or os.path.expanduser("~")
    p = ntpath.normpath(path or "")

    if _under(p, home):
        # 同盘才走 relpath：跨盘/UNC 它会抛 ValueError（实测），而 _under 已
        # 保证同盘，所以这里是安全的。
        rel = ntpath.relpath(p, ntpath.normpath(home))
    else:
        # 不在用户目录下（ProgramData、Program Files 之类）：去掉盘符保留其余层级。
        # UNC 的 splitdrive 给出 ('\\\\NAS\\share', '\\x')，同样只剩后半段。
        rel = ntpath.splitdrive(p)[1].lstrip("\\")

    rel = rel.strip("\\").strip()
    # 兜底：源恰好等于 home、或是盘根、或空串时 rel 会是 "." 或空。
    # 空后缀会让 join 得到根目录本身——那意味着直接往根上做 junction，
    # 是事故级的，必须给个明确的名字。
    if not rel or rel == ".":
        return "unnamed"
    return rel


def mirror_target(root: str, path: str, user_home: str | None = None) -> str:
    """目标完整路径 = 根 + 镜像后缀。"""
    return os.path.join(root, mirror_suffix(path, user_home))


def plan_all(records: list[dict], target_root: str | None = None,
             env_lookup=None) -> dict:
    """为整份快照出计划。返回 {plans, summary}。

    进程列表只取一次——每条候选各跑一次 PowerShell 会让出计划慢到不可用。
    env_lookup 同理只解析一次并往下传，顺带让整份计划用的是同一时刻的环境状态。
    """
    pdirs = running_process_dirs()
    root = target_root or effective_target_root()
    lookup = env_lookup or envvar_mod.effective_var
    plans = [plan_for(r, target_root=root, process_dirs=pdirs,
                      env_lookup=lookup) for r in records]
    plans.extend(_child_candidates(records, root, pdirs, lookup))

    # 顺序要紧：冲突标记会让原本可执行的父目录变成不可执行，所以"父目录能整块处理
    # 就不出子计划"这条去重必须等**可执行性最终定下来之后**再做。先去重会两头落空——
    # 实测 Local\uv 因 UV_CACHE_DIR 与 Roaming\uv 冲突被拦，而它的子目录 cache
    # （1.57 GiB）此前已按"父目录可执行"被跳过，结果父子都没进可执行集合。
    _flag_env_var_conflicts(plans)
    plans = _dedup_child_plans(plans)

    actionable = [p for p in plans if p.executable]
    by_action: dict[str, list[Plan]] = {}
    for p in actionable:
        by_action.setdefault(p.action, []).append(p)

    return {
        "target_root": root,
        "plans": [p.to_dict() for p in plans],
        "summary": {
            "total_candidates": len(plans),
            "executable": len(actionable),
            "reclaimable": sum(p.reclaimable for p in actionable),
            "by_action": {k: {"count": len(v),
                              "reclaimable": sum(x.reclaimable for x in v)}
                          for k, v in sorted(by_action.items())},
            "blocked": len([p for p in plans if p.blockers]),
            "blocker_counts": _blocker_counts(plans),
        },
    }


def _with_inherited_conf(child: dict, parent: dict) -> dict:
    r"""归属继承自父目录时，置信度也该继承。

    归因只给子目录算它**自身**的证据分，而子目录通常一条证据都没有——`Roaming\Code`
    下的 `WebStorage` 就是 conf=0.0。但 low_confidence 这道门禁问的是"会不会判错归属、
    动到别的软件的数据"，而这个子目录的归属claim完全建立在父目录之上：父目录 93%
    确信属于 VS Code，那么它的子目录属于 VS Code 的把握就是同样的 93%，不是 0。

    只在归属确实来自父目录（inherited）或与父目录一致时才继承，且取两者较大值——
    子目录若另有自己的证据，不该因为继承反而被削弱。归属与父目录不同的子目录说明它
    有独立归因，此时用它自己的分。
    """
    own = float(child.get("conf") or 0.0)
    pconf = float(parent.get("conf") or 0.0)
    same_owner = child.get("owner") == parent.get("owner")
    if not (child.get("inherited") or child.get("owner") is None or same_owner):
        return child
    out = dict(child)
    out["conf"] = max(own, pconf)
    return out


def _child_candidates(records: list[dict], root: str | None,
                      pdirs: set[str], env_lookup=None) -> list[Plan]:
    r"""给每个"定性可清理的子目录"出一份候选计划。

    为什么必须有这一层：归因早就给子目录定了性（attribute_v4 的 children 带 cat），
    但计划器原先只遍历顶层记录，于是 `Roaming\Code` 这 9.93 GiB 只能整块判"未定性"
    并拦下——而它下面 `WebStorage` 6.40 GiB 名字就是缓存、`User` 3.14 GiB 是真设置
    不能碰。整块表达不了"一半能清一半不能"，结果是整块都清不了。

    这里只管生成；与父目录的去重交给 _dedup_child_plans，在可执行性定下来之后做。
    """
    out: list[Plan] = []
    for r in records:
        pname = r.get("name") or (r.get("path") or "")
        for c in r.get("children") or []:
            if c.get("cat") not in CLEANABLE_CATS:
                continue
            cp = plan_for(_with_inherited_conf(c, r), target_root=root,
                          process_dirs=pdirs, as_child=True,
                          env_lookup=env_lookup)
            cp.parent_path = r.get("path")
            if cp.owner is None:
                # 子目录常常自己没有归因证据，归属由父目录继承而来
                cp.owner = r.get("owner")
            cp.notes.append(
                f"父目录「{pname}」整块不可动，"
                f"但这个子目录本身定性为「{c.get('cat')}」，可单独处理")
            out.append(cp)
    return out


def _dedup_child_plans(plans: list[Plan]) -> list[Plan]:
    """父目录整块可执行时丢掉它的子计划，避免同一份空间算两遍。

    不这么做的后果是界面上"可腾出 X GiB"变成虚数：父目录和它的子目录各报一次。
    丢弃而非保留成不可执行项，是因为"父目录已经覆盖了它"不是一条需要用户处理的
    拦阻，列出来只会让人以为还有事没做。
    """
    ok_parents = {p.path.lower().rstrip("\\")
                  for p in plans if p.executable and p.parent_path is None}
    return [p for p in plans
            if p.parent_path is None
            or (p.parent_path or "").lower().rstrip("\\") not in ok_parents]


def _flag_env_var_conflicts(plans: list[Plan]) -> None:
    """一个环境变量只能指向一个位置，多个候选争抢时全部拦下。

    实测触发：`Local\\uv` 与 `Roaming\\uv` 都映射到 UV_CACHE_DIR。各自执行的话第二条
    会覆盖第一条的设置，结果是"两个都报成功、其中一个实际没生效、而旧缓存都已删"。
    这种错误在执行完之后极难看出来，所以必须在出计划阶段就拦住，由人决定留哪个。
    """
    by_var: dict[str, list[Plan]] = {}
    for p in plans:
        if p.action == "redirect" and p.env_var:
            by_var.setdefault(p.env_var, []).append(p)

    for var, group in by_var.items():
        if len(group) < 2:
            continue
        for p in group:
            others = [os.path.basename(x.path) or x.path
                      for x in group if x is not p]
            p.blockers.append(Blocker(
                "env_var_conflict",
                f"{var} 同时被 {len(group)} 个目录申领（还有 {', '.join(others)}）。"
                f"一个变量只能指向一处，需先决定保留哪个"))


def _blocker_counts(plans: list[Plan]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in plans:
        for b in p.blockers:
            out[b.code] = out.get(b.code, 0) + 1
    return dict(sorted(out.items(), key=lambda x: -x[1]))
