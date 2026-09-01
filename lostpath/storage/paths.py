"""唯一决定"用户数据放哪"的模块。其他地方一律通过这里取路径。

设计约束：
- 用 LOCALAPPDATA 而非 APPDATA(Roaming)。快照描述的是本机 C 盘事实——路径、
  体积、装了什么全是机器特有的，跟着域账户漫游到另一台机器就是错数据。
- 数据一律在安装目录之外。装到 Program Files 后非管理员写不进去，而"默认只读、
  非管理员可用"是项目红线。
- LOSTPATH_DATA_DIR 覆盖用于测试与便携模式，避免污染真实用户目录。
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "LostPath"
ENV_OVERRIDE = "LOSTPATH_DATA_DIR"


def data_root() -> Path:
    """用户数据根目录。Windows 下为 %LOCALAPPDATA%\\LostPath。"""
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA")
    if not base:  # 非 Windows 或环境异常时的兜底
        base = Path.home() / "AppData" / "Local"
    return Path(base) / APP_DIR_NAME


def snapshots_dir() -> Path:
    return data_root() / "snapshots"


def latest_snapshot() -> Path:
    """当前快照。扫描产出写这里，API 读这里。"""
    return snapshots_dir() / "latest.json"


def icons_dir() -> Path:
    """图标缓存。从本机 exe 提取，可随时重建，故与快照同放 Local。"""
    return data_root() / "icons"


def config_dir() -> Path:
    return data_root() / "config"


def portable_config() -> Path:
    """用户确认的便携软件。存的是绝对路径（G:\\UV\\bin\\uv.exe），换机器即失效，
    所以放 Local 不漫游。"""
    return config_dir() / "portable.json"


def target_root_config() -> Path:
    """用户指定的迁移目标位置。不设则由 planner 自动挑剩余最大的非系统盘。

    与 portable.json 同理放 Local：存的是本机盘符（E:\\LostPathStore），
    漫游到另一台机器上那个盘可能根本不存在。"""
    return config_dir() / "target_root.json"


def rules_config() -> Path:
    """用户手动确认的路径规则。与目标位置配置分开，便于导出和恢复。"""
    return config_dir() / "rules.json"


def inspection_config() -> Path:
    """自动巡检配置。只存用户偏好，不存扫描结果。"""
    return config_dir() / "inspection.json"


def logs_dir() -> Path:
    return data_root() / "logs"


def ensure_dirs() -> None:
    """首次启动时建目录树。幂等。"""
    for d in (snapshots_dir(), icons_dir(), config_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)


def describe() -> dict[str, str]:
    """供 /api/health 与排障使用：把实际生效的路径摊出来。"""
    return {
        "data_root": str(data_root()),
        "latest_snapshot": str(latest_snapshot()),
        "icons_dir": str(icons_dir()),
        "portable_config": str(portable_config()),
        "target_root_config": str(target_root_config()),
        "rules_config": str(rules_config()),
        "inspection_config": str(inspection_config()),
        "override_active": bool(os.environ.get(ENV_OVERRIDE)) and ENV_OVERRIDE,
    }
