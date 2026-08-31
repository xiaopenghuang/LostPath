r"""迁移目标位置的校验与持久化。

**为什么独立成一个模块，而不塞进 planner.py**：`planner.py` 开头第一句就是"全程只读，
不碰任何文件"，而"记住用户选的位置"必须写盘。混进去会让那句话变成假话——而执行器
信任计划器的前提正是那句话。

**为什么校验必须在服务端**：`engine/main.py` 的安全要点写着"绝不接受调用方自带的记录
或任意目标目录"，可 `ExecuteReq.target_root` 一直就是调用方自带的任意目录，且零校验就
进了 `os.path.join`。界面上做检查挡不住直接打 HTTP 的调用方，所以规则得落在这里。

**为什么每次读取都重新校验**（见 `effective()`）：盘会被拔、盘符会变、目录会被别的程序
删掉。保存那一刻合法不等于用起来还合法，而这个值决定把用户几个 GiB 的数据搬到哪。
"""
from __future__ import annotations

import ctypes
import json
import ntpath
import os
import tempfile
import threading

from ..storage import paths as lp_paths

# 写入探测的上限。本地磁盘上正常是 1~2 ms，2 秒已经是三个数量级的余量——
# 超过它说明这个位置有别的毛病，不该让界面跟着一起等。
_PROBE_TIMEOUT_SEC = 2.0

# GetDriveTypeW 的返回值。只有 FIXED 能当迁移目标：
#   REMOVABLE 拔了盘软件就找不到数据；REMOTE 的目录 junction 根本建不出来
#   （Windows 的重解析点只能指向本地卷）；CDROM 只读；NO_ROOT_DIR 是盘不存在。
DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR, DRIVE_REMOVABLE = 0, 1, 2
DRIVE_FIXED, DRIVE_REMOTE, DRIVE_CDROM, DRIVE_RAMDISK = 3, 4, 5, 6

_TYPE_REASON = {
    DRIVE_NO_ROOT_DIR: ("drive_not_found", "这个盘不存在（可能盘符变了或盘没插）"),
    DRIVE_REMOVABLE: ("removable_drive",
                      "可移动盘不能当目标：拔掉之后软件会找不到数据，"
                      "而原位置只剩一个指向空处的链接"),
    DRIVE_REMOTE: ("remote_drive",
                   "网络位置不能当目标：Windows 的目录 junction 只能指向本地卷，"
                   "建链接这一步会失败"),
    DRIVE_CDROM: ("cdrom_drive", "光驱是只读的，写不进去"),
    DRIVE_UNKNOWN: ("unknown_drive_type", "无法识别这个位置的驱动器类型"),
    DRIVE_RAMDISK: ("ramdisk_drive", "内存盘重启即清空，放数据会丢"),
}


def _drive_type(root: str) -> int:
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(root))
    except (OSError, AttributeError):
        return DRIVE_UNKNOWN


def _probe_writable(path: str) -> tuple[bool, str | None]:
    r"""能不能往这儿写。

    目标目录通常还不存在（我们不预先创建它——用户改两次主意就会留下空目录），
    所以往上找第一个已存在的祖先来试。用 `tempfile` 而非自己 open：它在 Windows 上
    带 O_TEMPORARY，句柄一关系统自己删，不会因为我们中途抛异常而留下垃圾文件。
    """
    p = os.path.abspath(path)
    while not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            return False, "路径所在的位置不存在"
        p = parent

    # 最近的已存在祖先就是盘根时，不探测。两个理由，第二个是实测撞出来的：
    #
    # ① 语义不对。我们要建的是盘根**下面**的一个目录，而"能否在盘根直接建文件"是
    #    另一回事——C:\ 根目录建文件要管理员，C:\LostPathStore 里写文件不要。
    # ② **实测会挂住。** 本机标准权限下 `tempfile.TemporaryFile(dir="C:\\")`
    #    90 秒不返回（同一函数在 E:\ 上是 0.7 ms）。而界面每敲一个字符就校验一次，
    #    用户打出 "C:\" 的那一刻请求就挂在那儿了——最初正是在浏览器里看到
    #    "正在核对…" 再也不消失才发现的。
    if p.rstrip("\\") == ntpath.splitdrive(p)[0]:
        return True, None

    result: list[tuple[bool, str | None]] = []

    def run() -> None:
        try:
            with tempfile.TemporaryFile(dir=p):
                pass
            result.append((True, None))
        except OSError as e:
            # 报原文而不是笼统的"不可写"：权限不足和路径过长要用户做的事完全不同
            result.append((False, f"写入测试失败：{e.strerror or e}"))

    # 限时兜底。上面那条盘根短路挡掉了已知会挂的情形，这层管其余的：挂载点、被安全
    # 软件盯着的目录都可能让一次创建文件长时间不返回，而这个函数在用户每次输入时都
    # 会被调用。**超时按"可以用"处理**——写不进去执行阶段会如实报错并中止（那里有
    # 完整的回滚记录），而卡死输入框会让人以为整个程序死了。
    # 线程用 daemon：真挂住时它收不回来，但进程退出不会被它拖住。
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(_PROBE_TIMEOUT_SEC)
    if not result:
        return True, None
    return result[0]


def _err(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def validate(raw: str | None) -> dict:
    r"""检查一个候选目标根。

    返回 `{ok, normalized, errors, warnings}`。**errors 与 warnings 分开是刻意的**：
    "填了 C 盘"在技术上完全可行，只是达不到用户的目的（腾 C 盘空间），该由用户自己
    拍板；而"填了网络盘"是执行到一半必然失败，不能让他有机会按下去。

    errors 非空时 normalized 为 None——不给出一个"差不多能用"的值，免得调用方
    忽略了 ok 还照样往下走。
    """
    errors: list[dict] = []
    warnings: list[dict] = []

    s = (raw or "").strip().strip('"')
    if not s:
        return {"ok": False, "normalized": None,
                "errors": [_err("empty", "还没填路径")], "warnings": []}

    # \\?\ 前缀（长路径写法）剥掉再判。用户从别处复制来时可能带着它，而留着会让界面
    # 上显示 \\?\E:\... 这种不像人写的路径——见 HANDOVER 里深路径那条债的同一考虑。
    if s.startswith("\\\\?\\"):
        s = s[4:]

    drive, _tail = ntpath.splitdrive(s)

    # UNC 要在 isabs 之前判：`\\NAS\share\x` 的 isabs 是 True（实测），
    # 光靠 isabs 放它过去，会一路走到建 junction 那步才失败。
    if drive.startswith("\\\\") or drive.startswith("//"):
        errors.append(_err("unc_not_supported",
                           "不支持网络路径（\\\\服务器\\共享）：目录 junction 只能"
                           "指向本地磁盘"))
    elif not drive:
        # `\store` 的 isabs 也是 True，但它是"当前盘的根下"——当前盘是谁取决于进程
        # 工作目录，等于没指定盘。
        errors.append(_err("no_drive",
                           "路径要从盘符开始写，例如 E:\\LostPathStore"))
    elif not ntpath.isabs(s):
        # 这条是最容易踩的：`E:` 或 `E:store` 都是**盘符相对路径**，实测
        # os.path.join("E:", "x") 得到 "E:x"，落在进程在 E: 上的当前目录里，
        # 而不是 E:\x。进程当前目录恰为盘根时它看起来还是对的，换个目录就写到别处。
        errors.append(_err("not_absolute",
                           f"盘符后面要有反斜杠：写成 {drive}\\... 而不是 {s}。"
                           f"缺了它就是「相对于该盘当前目录」，实际落点不确定"))

    if errors:
        return {"ok": False, "normalized": None, "errors": errors,
                "warnings": warnings}

    normalized = ntpath.normpath(s)
    drive, _ = ntpath.splitdrive(normalized)

    dtype = _drive_type(drive + "\\")
    if dtype != DRIVE_FIXED:
        code, msg = _TYPE_REASON.get(
            dtype, ("not_fixed_drive", "只能用本机固定磁盘"))
        errors.append(_err(code, f"{drive} {msg}"))
        return {"ok": False, "normalized": None, "errors": errors,
                "warnings": warnings}

    # 不能落在 LostPath 自己的数据目录里：回收区就在那儿，搬过去的数据会和"待永久
    # 删除"的数据混在一个树下，清空回收区时极容易连带删掉。
    data_root = str(lp_paths.data_root())
    if _is_within(normalized, data_root):
        errors.append(_err(
            "inside_app_data",
            f"不能放在 LostPath 自己的数据目录里（{data_root}）：回收区也在那儿，"
            f"清空回收区时可能连带删掉搬过去的数据"))
        return {"ok": False, "normalized": None, "errors": errors,
                "warnings": warnings}

    ok, why = _probe_writable(normalized)
    if not ok:
        errors.append(_err("not_writable", f"{why}"))
        return {"ok": False, "normalized": None, "errors": errors,
                "warnings": warnings}

    # ---- 到这里技术上都能用了，剩下的是"能用但你可能不想要" ----
    system_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\")
    if drive.upper() == system_drive.upper():
        warnings.append(_err(
            "same_as_system_drive",
            f"{drive} 就是系统盘。搬到同一个盘腾不出任何空间——重定向类操作会让"
            f"软件把新缓存继续写在 {drive}，搬迁类操作还要白复制一遍数据"))

    if normalized.rstrip("\\") == drive:
        # 目标**本身就是盘根**（`E:\`）：各软件的目录会直接摊在盘根上。不拦，但值得提。
        #
        # 曾经写成 `dirname(normalized) == drive`，那判的是"盘根下的一级目录"——
        # 于是默认值 E:\LostPathStore 自己就中了这条警告，而警告文案还建议"例如
        # E:\LostPathStore"。**两条单元测试都绿**，因为它们用的 Q:\store 同样是盘根
        # 下一级，测试与实现犯了同一个错。是浏览器里看见那句自相矛盾的话才发现的。
        warnings.append(_err(
            "at_drive_root",
            f"目标是 {drive} 的根目录，各软件的目录会直接摊在盘根上；"
            f"建议给个专门的文件夹，例如 {drive}\\LostPathStore"))

    return {"ok": True, "normalized": normalized, "errors": [],
            "warnings": warnings}


def _is_within(path: str, ancestor: str) -> bool:
    """path 是否等于 ancestor 或落在它之下。

    补分隔符再比前缀：不补的话 `E:\\Store2` 会被判成在 `E:\\Store` 之下
    ——`planner._running_under` 踩过同一个坑。
    """
    a = ntpath.normcase(ntpath.normpath(path)).rstrip("\\")
    b = ntpath.normcase(ntpath.normpath(ancestor)).rstrip("\\")
    return a == b or a.startswith(b + "\\")


# ------------------------------------------------------------------ 持久化
def config_file():
    return lp_paths.target_root_config()


def _read_config() -> dict:
    """整份配置。读不出来（不存在、坏 JSON）就当空的。"""
    try:
        with open(config_file(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_raw() -> str | None:
    """用户存的原始字符串，不校验。给界面显示"你设过什么"用。"""
    v = _read_config().get("target_root")
    return v if isinstance(v, str) and v.strip() else None


def load_overrides() -> dict[str, str]:
    r"""逐项覆盖表：`{源路径小写: 该项要用的根}`。不校验，原样返回。

    键用小写：Windows 路径不区分大小写，而界面传来的大小写未必与快照里一致
    （用户可能从别处复制粘贴）。不统一的话同一个目录会存出两条互相看不见的
    覆盖，用户改了以为没生效。
    """
    raw = _read_config().get("overrides")
    if not isinstance(raw, dict):
        return {}
    return {str(k).lower(): v for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, str) and v.strip()}


def override_for(path: str) -> str | None:
    r"""这个源路径有没有专属的根，且现在还能用。

    **每次都重新校验**，理由与 `effective()` 相同：存的时候合法不代表现在合法。
    失效时返回 None，调用方回落到全局根——静默回落是刻意的，出计划是只读操作，
    此刻报错没有用户能采取的动作；界面另有端点告知失效状态。
    """
    if not path:
        return None
    raw = load_overrides().get(str(path).lower())
    if not raw:
        return None
    res = validate(raw)
    return res["normalized"] if res["ok"] else None


def effective() -> str | None:
    """当前真正能用的目标根；没设过或已失效时返回 None，由调用方回落到自动挑。

    **每次都重新校验**：盘可能被拔了、盘符可能变了。保存时合法不代表现在合法，
    而拿一个失效的路径去搬几个 GiB 的数据，失败点会出现在复制到一半的时候。
    """
    raw = load_raw()
    if not raw:
        return None
    res = validate(raw)
    return res["normalized"] if res["ok"] else None


def save(raw: str | None) -> dict:
    """校验并保存。raw 为空则清除设置、回落到自动挑。

    返回 validate() 的结果（清除时返回 ok=True, normalized=None）。**校验不过不落盘**：
    存一个用不了的值，下次启动只会在别处以更难懂的方式失败。
    """
    if raw is None or not str(raw).strip():
        try:
            os.remove(config_file())
        except FileNotFoundError:
            pass
        except OSError as e:
            return {"ok": False, "normalized": None,
                    "errors": [_err("write_failed", f"清除设置失败：{e}")],
                    "warnings": []}
        return {"ok": True, "normalized": None, "errors": [], "warnings": []}

    res = validate(raw)
    if not res["ok"]:
        return res

    # 读-改-写：直接写 {"target_root": ...} 会把 overrides 整块冲掉。
    # 用户改一次全局根就丢掉所有逐项设置，而且无声无息。
    payload = _read_config()
    payload["target_root"] = res["normalized"]
    err = _write_config(payload)
    if err:
        return {"ok": False, "normalized": None,
                "errors": [err], "warnings": res["warnings"]}
    return res


def _write_config(payload: dict) -> dict | None:
    """原子写整份配置。成功返回 None，失败返回一个 error dict。"""
    cfg = config_file()
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.with_suffix(".json.tmp")
        # 先写临时文件再替换：直接覆盖时若中途断电，配置会变成半个 JSON，
        # 下次启动读到 ValueError 就静默回落——用户会以为设置丢了。
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, cfg)
    except OSError as e:
        return _err("write_failed", f"保存失败：{e}")
    return None


def set_override(path: str, raw: str | None) -> dict:
    r"""给单个源路径指定专属的根。`raw` 为空则清掉这一条、回落到全局根。

    校验用的是同一个 `validate()`——逐项和全局的规则必须一致，否则用户会遇到
    "全局填这个不行、逐项填就行"这种说不通的差异。**校验不过不落盘**。
    """
    if not path or not str(path).strip():
        return {"ok": False, "normalized": None,
                "errors": [_err("no_path", "没有指定要覆盖哪个目录")],
                "warnings": []}

    key = str(path).lower()
    payload = _read_config()
    ov = payload.get("overrides")
    if not isinstance(ov, dict):
        ov = {}

    if raw is None or not str(raw).strip():
        ov.pop(key, None)
        payload["overrides"] = ov
        err = _write_config(payload)
        if err:
            return {"ok": False, "normalized": None, "errors": [err], "warnings": []}
        return {"ok": True, "normalized": None, "errors": [], "warnings": []}

    res = validate(raw)
    if not res["ok"]:
        return res
    ov[key] = res["normalized"]
    payload["overrides"] = ov
    err = _write_config(payload)
    if err:
        return {"ok": False, "normalized": None,
                "errors": [err], "warnings": res["warnings"]}
    return res
