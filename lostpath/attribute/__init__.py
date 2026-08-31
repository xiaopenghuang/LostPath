"""归因：把 C 盘足迹目录判给具体软件，每条判断附证据与置信度。

对外只暴露 attribute_footprint()。输入三份已解析的 JSON（扫描结果、软件台账、
快捷方式），输出 (records, stats)。

`attribute_v4.py` 里保留的 v4 字样是搬迁期的临时状态：AGENTS.md 要求"先原样搬、
后重构命名"，命名规范（标识符不带版本号，版本进 schema_version）另开一轮统一。
"""
from .attribute_v4 import attribute_footprint

__all__ = ["attribute_footprint"]
