r"""Windows 目录枚举：一次拿到名字、大小、属性和**文件 ID**。

**为什么不用 `os.scandir`。** 硬链接去重需要知道两个路径是不是同一份内容，而
`os.DirEntry.stat()` 在 Windows 上的数据来自 `FindFirstFileW`，那个 API 既不返回链接数
（`st_nlink` 恒为 0）也不返回文件 ID（`st_ino` 恒为 0）。`DirEntry.inode()` 有真值，但
它内部要为每个文件开一次句柄——实测慢 170%。

`GetFileInformationByHandleEx(FileIdBothDirectoryInfo)` 一次调用返回约 64 KiB 的目录条
目，每条都带 `FileId`，与 `os.stat().st_ino` 实测完全一致。代价只比 `os.scandir` 高
**14.2%**（对比 `os.stat()` 的 170%），全盘扫描 21 秒变约 24 秒——去重因此从"太贵，只能
报逻辑大小"变成"顺手就做了"。

实测健壮性：14 个真实目录（System32 / WinSxS / $Recycle.Bin / 盘根 / 5 个拒绝访问的系统
目录 / 长路径）与 `os.scandir` **零分歧**——条数、文件名、字节数一致，拒绝访问时两者都
失败。长路径（>260 字符）两者都需要 `\\?\` 前缀，行为一致，不是回退。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os

__all__ = ["list_dir", "Entry", "FILE_ATTRIBUTE_DIRECTORY",
           "FILE_ATTRIBUTE_REPARSE_POINT", "available"]

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_FILE_LIST_DIRECTORY = 0x0001
_SHARE_ALL = 0x0001 | 0x0002 | 0x0004      # read|write|delete，不挡别人用这个目录
_OPEN_EXISTING = 3
_BACKUP_SEMANTICS = 0x02000000             # 打开目录句柄必需
_FileIdBothDirectoryInfo = 10
_ERROR_NO_MORE_FILES = 18
_BUF = 65536

if os.name == "nt":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wt.HANDLE
    _k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, wt.HANDLE]
    _k32.GetFileInformationByHandleEx.restype = wt.BOOL
    _k32.GetFileInformationByHandleEx.argtypes = [wt.HANDLE, ctypes.c_int,
                                                  ctypes.c_void_p, wt.DWORD]
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _INVALID = ctypes.c_void_p(-1).value
else:                                       # 非 Windows：让 import 与调用都不炸
    _k32 = None
    _INVALID = -1


class _INFO(ctypes.Structure):
    """FILE_ID_BOTH_DIR_INFO。字段顺序必须与 Windows 头文件一致，错一个就读到垃圾。"""

    _fields_ = [
        ("NextEntryOffset", wt.DWORD),
        ("FileIndex", wt.DWORD),
        ("CreationTime", wt.LARGE_INTEGER),
        ("LastAccessTime", wt.LARGE_INTEGER),
        ("LastWriteTime", wt.LARGE_INTEGER),
        ("ChangeTime", wt.LARGE_INTEGER),
        ("EndOfFile", wt.LARGE_INTEGER),
        ("AllocationSize", wt.LARGE_INTEGER),
        ("FileAttributes", wt.DWORD),
        ("FileNameLength", wt.DWORD),
        ("EaSize", wt.DWORD),
        ("ShortNameLength", ctypes.c_byte),
        ("ShortName", wt.WCHAR * 12),
        ("FileId", wt.LARGE_INTEGER),
        ("FileName", wt.WCHAR * 1),         # 变长，按 FileNameLength 取
    ]


class Entry:
    """一个目录条目。字段是枚举时一次拿到的，不需要再 stat。"""

    __slots__ = ("name", "path", "size", "file_id", "attrs")

    def __init__(self, name: str, path: str, size: int,
                 file_id: int, attrs: int):
        self.name = name
        self.path = path
        self.size = size
        # 0 与 -1 视为"未知"：个别文件系统不给真 ID，若当成真值会把不同文件当成
        # 同一份，去重后体积被低报。低报比高报更危险——用户会以为清不出空间。
        self.file_id = file_id if file_id not in (0, -1) else None
        self.attrs = attrs

    @property
    def is_dir(self) -> bool:
        return bool(self.attrs & FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_reparse(self) -> bool:
        return bool(self.attrs & FILE_ATTRIBUTE_REPARSE_POINT)

    def __repr__(self) -> str:
        return f"Entry({self.name!r}, size={self.size}, id={self.file_id})"


def available() -> bool:
    return _k32 is not None


def list_dir(path: str) -> list[Entry]:
    r"""列出 path 下的条目（不含 `.` 与 `..`）。

    失败时抛 `OSError`，与 `os.scandir` 一致——调用方原有的 `except OSError` 记
    denied 的逻辑不用改。
    """
    if _k32 is None:
        raise OSError(f"winfs 仅支持 Windows：{path}")
    h = _k32.CreateFileW(path, _FILE_LIST_DIRECTORY, _SHARE_ALL, None,
                         _OPEN_EXISTING, _BACKUP_SEMANTICS, None)
    if h == _INVALID or not h:
        err = ctypes.get_last_error()
        raise OSError(0, f"CreateFileW failed (winerror {err})", path, err)
    out: list[Entry] = []
    buf = ctypes.create_string_buffer(_BUF)
    try:
        while True:
            ok = _k32.GetFileInformationByHandleEx(
                h, _FileIdBothDirectoryInfo, buf, _BUF)
            if not ok:
                err = ctypes.get_last_error()
                if err == _ERROR_NO_MORE_FILES:
                    break
                raise OSError(0, f"GetFileInformationByHandleEx failed "
                                 f"(winerror {err})", path, err)
            off = 0
            while True:
                rec = _INFO.from_buffer(buf, off)
                name = ctypes.wstring_at(
                    ctypes.addressof(rec) + _INFO.FileName.offset,
                    rec.FileNameLength // 2)
                if name not in (".", ".."):
                    out.append(Entry(name, os.path.join(path, name),
                                     rec.EndOfFile, rec.FileId,
                                     rec.FileAttributes))
                if rec.NextEntryOffset == 0:
                    break
                off += rec.NextEntryOffset
    finally:
        _k32.CloseHandle(h)
    return out
