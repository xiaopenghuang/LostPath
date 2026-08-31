r"""用户级环境变量读写。只碰 HKCU\Environment，绝不碰系统级。

**为什么不用 setx**：它把值截断到 1024 字符且不报错，官方文档自己都标了这个限制。
路径类变量虽然通常不长，但一个"静默截断"的写入方式不该出现在会改用户环境的代码里。

写完必须广播 WM_SETTINGCHANGE。不广播的话：注册表已经改了，但已在运行的 explorer.exe
及其子进程（也就是用户之后从开始菜单启动的一切）仍持有旧环境块，表现为"设了变量但
软件没反应"，要重启才生效。广播能让新启动的进程立刻拿到新值。

不碰系统级（HKLM）是刻意的：那需要管理员权限，且影响所有用户。本项目红线是"非管理员
可用"，而缓存重定向本就是当前用户的事。
"""
from __future__ import annotations

import ctypes
import winreg

ENV_KEY = r"Environment"
# 系统级环境变量。**只读，绝不写**——写它要管理员且影响所有用户。
# 但必须能读：见 effective_var() 的说明。
MACHINE_ENV_KEY = (
    r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002


def _read(root, subkey: str, name: str) -> str | None:
    try:
        with winreg.OpenKey(root, subkey) as k:
            value, _type = winreg.QueryValueEx(k, name)
            return value
    except FileNotFoundError:
        return None
    except OSError:
        return None


def get_user_var(name: str) -> str | None:
    """读用户级环境变量。返回 None 表示该变量不存在（与空字符串区分）。

    这个区分对回滚是必需的：原本不存在就该删掉，原本是空字符串就该写回空。
    """
    return _read(winreg.HKEY_CURRENT_USER, ENV_KEY, name)


def get_machine_var(name: str) -> str | None:
    """读系统级环境变量。只读——本模块从不写 HKLM。"""
    return _read(winreg.HKEY_LOCAL_MACHINE, MACHINE_ENV_KEY, name)


def effective_var(name: str) -> tuple[str | None, str | None]:
    r"""这个变量对**新启动的进程**实际是什么值。返回 (value, scope)。

    scope 为 "user" / "machine" / None（未设置）。用户级优先——Windows 建进程
    环境块时先取 HKLM 再用 HKCU 覆盖（PATH 是特例，会拼接；缓存类变量不是）。

    **为什么读两处，而 set/delete 只碰 HKCU。** 这个不对称是刻意的，也是一个真
    bug 的成因：写只碰 HKCU 是红线（不需要管理员、不影响其他用户），但判断"用户
    是不是已经自己重定向过了"必须连 HKLM 一起看——HKLM 的值同样会进新进程的环境
    块。只读 HKCU 的话，一台把 UV_CACHE_DIR 设在 HKLM 的机器会被判成"没设过"，
    于是计划器提议再设一个 HKCU 值把它盖掉，把缓存挪到别处，而用户原来那份（实测
    某机器上是 16.10 GiB）就成了谁都不读的孤儿——界面还报"成功"。
    """
    value = get_user_var(name)
    if value is not None:
        return value, "user"
    value = get_machine_var(name)
    if value is not None:
        return value, "machine"
    return None, None


def set_user_var(name: str, value: str) -> None:
    """写用户级环境变量并广播变更。

    用 REG_EXPAND_SZ 而非 REG_SZ：路径里可能含 %USERPROFILE% 这类可展开变量，
    用 REG_SZ 会让它们变成字面量。
    """
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, ENV_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_EXPAND_SZ, value)
    broadcast_change()


def delete_user_var(name: str) -> bool:
    """删除用户级环境变量。返回是否真的删了（不存在返回 False，不报错）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENV_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    broadcast_change()
    return True


def broadcast_change() -> bool:
    """广播 WM_SETTINGCHANGE，让新进程立刻拿到新环境块。

    失败不抛异常：变量已经写进注册表了，广播只影响"何时生效"。把它变成硬错误会让
    调用方以为写入失败而去回滚，那才是真的坏事。
    """
    try:
        res = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0,
            ctypes.c_wchar_p("Environment"), SMTO_ABORTIFHUNG, 1000,
            ctypes.byref(res))
        return True
    except (OSError, AttributeError):
        return False
