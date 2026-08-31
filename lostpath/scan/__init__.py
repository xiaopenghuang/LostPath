"""采集：产出归因所需的三份原始输入。

- `scan_dirs.py`         目录体积递归扫描 -> scan_c.json 形态
- `collect_evidence.py`  快捷方式目标 exe 采集 -> shortcuts.json 形态
- `export_inventory.ps1` 注册表卸载项 / 服务 / 启动项 / Appx -> inventory.json 形态

三者目前仍是命令行脚本（M0 原样搬入，见 AGENTS.md「先原样搬、后重构命名」）。
P2 做 `POST /api/scan` 时会给它们补可调用入口，届时才动函数签名。
"""
