<div align="center">

<img src="ui/public/logo.png" alt="LostPath" width="120">

# LostPath

**把 Windows C 盘的占用归因到具体软件，并给出可撤销的处理方案**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white)
![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)
![Electron](https://img.shields.io/badge/Electron-33-47848F?logo=electron&logoColor=white)
![Tests](https://img.shields.io/badge/tests-325%20passing-brightgreen)

**中文** · [English](README.en.md)

</div>

---

软件装在 D 盘，C 盘却一天天满：VS Code 的缓存在 `AppData\Roaming\Code`，Docker 的镜像在
`ProgramData`，`uv` 与 `pnpm` 的缓存又各在一处。LostPath 扫描 C 盘、把每处占用归因到具体
软件，标明它是可再生缓存还是用户数据，并给出对应的处理方式——改环境变量、清理，或跨盘迁移
后原位留 junction。

所有判断都基于本机实扫结果并附证据链。扫描全程只读；任何写操作执行前落回滚记录，数据先进
回收区，30 天内可撤销。

## 功能特性

- **归因带证据与置信度** — 注册表卸载项、Appx 家族名、快捷方式指向、可执行文件签名、目录
  命名特征多源交叉，每条结论可展开查看依据与权重。
- **硬链接感知的体积核算** — 区分 `logical`（逐文件累加）、`dedup`（每个 inode 一次）与
  `freeable`（全部链接都在树内）。实测某 `uv` 缓存逻辑体积 1.63 GB，实际仅占 0.31 GB。
- **三种处理方式，按风险递进** — 优先改软件官方支持的环境变量（不搬文件），其次清理可再生
  缓存，最后才是跨盘迁移 + junction。
- **30 天可撤销** — 执行前写入回滚清单，删除的数据移入回收区而非直接删除；环境变量原值一并
  记录，撤销时还原。
- **迁移目标位置可自定义** — 默认自动选择非系统盘中可用空间最大的一个。
- **明确的拒绝清单** — 正在运行、已是重解析点、系统归属、置信度不足、目标盘容量不够等情况
  一律不处理，并逐条给出原因。
- **标准权限可用** — 不要求管理员。权限不足导致的扫描盲区会明确列出路径与条数。
- **浅色 / 深色双主题** — 文字对比度按 WCAG 实测（正文 ≥4.5:1，非文本元素 ≥3:1），可点元素
  均可键盘访问。

## 安装

从 [Releases](https://github.com/xiaopenghuang/LostPath/releases) 下载 `LostPath.Setup.<版本>.exe`
并运行。目标机器无需安装 Python、conda 或 Node。

要求 Windows 10/11 x64。核心逻辑依赖 `winreg`、PowerShell 与 NTFS 重解析点语义，暂无跨平台
计划。

## 从源码运行

```bash
# 后端
python -m pip install fastapi uvicorn

# 前端
cd ui && npm install && npm run build && cd ..

# 启动（引擎同时提供静态文件服务）
python engine/main.py
```

浏览器打开 `http://127.0.0.1:8321`。

桌面外壳（原生窗口、单实例锁、引擎生命周期管理）：

```bash
cd desktop && npm install && npm start
```

外壳按三级顺序定位引擎：打包进 resources 的 exe → 仓库 `dist/lostpath-engine.exe` → 用 conda
运行源码。第三级需要 conda 位于 PATH，或通过环境变量指定：

```bash
set LOSTPATH_CONDA_EXE=D:\miniconda3\Scripts\conda.exe
set LOSTPATH_CONDA_ENV=lostpath
```

## 打包

```bash
sh tools/build-release.sh
# 指定解释器：
LOSTPATH_PY=/d/conda/envs/lostpath/python.exe sh tools/build-release.sh
```

依次执行：前端构建 → 图标 → PyInstaller 打包引擎 → electron-builder 生成安装包。**顺序不可
调整**：引擎 exe 内嵌 `ui/dist`，安装包内嵌引擎 exe，两处都是快照拷贝而非引用。

## 技术栈

| 层 | 技术 |
|---|---|
| 归因引擎 | Python 3.12 |
| 本地服务 | FastAPI + uvicorn（`127.0.0.1:8321`，19 个 JSON 端点）|
| 界面 | React 18 + TypeScript 5 + Vite + Ant Design + AntV G6 |
| 桌面外壳 | Electron 33 |
| 打包 | PyInstaller + electron-builder (NSIS) |

## 项目结构

```
lostpath/
  scan/         目录枚举与证据采集
  attribute/    归因引擎与知识库
  act/          计划器（只读）与执行器
  storage/      快照与路径解析
engine/         FastAPI 服务与软件台账
ui/             前端
desktop/        Electron 外壳
tests/          325 项测试与脱敏基准数据
```

## 数据位置

全部位于 `%LOCALAPPDATA%\LostPath\`：

```
snapshots/   扫描快照        operations/  回滚台账
recycle/     回收区（30 天）  icons/       图标缓存
config/      用户设置        logs/
```

不写入安装目录（`Program Files` 下非管理员无写权限），也不使用 Roaming（快照描述本机磁盘
事实，漫游到其他机器即为错误数据）。可通过 `LOSTPATH_DATA_DIR` 整体重定向。

## 测试

```bash
python -m pytest -q                  # 快速套件，约 10s，读取脱敏基准
python -m pytest -m integration -q   # 集成套件，启动真实引擎全盘扫描，约 40s
```

`tools/install-hooks.sh` 可安装 pre-commit 钩子，提交前自动运行快速套件。

归因基准 `tests/fixtures/machine-a/` 是一台真实机器的脱敏快照（用户名、SID、环境变量已移除），
随仓库发布，因此归因结果可独立复算：

```bash
python tests/test_attribution_baseline.py
# 基准 27 条：correct 27, wrong 0, missing 0
# 覆盖 106 处痕迹 / 50.65 GiB，其中 1.90 GiB 未归因
```

## 已知限制

- 深路径（>260 字符）的字节数暂不计入统计。
- 执行前显示的 `reclaimable` 为上界；精确的 `freeable` 在执行后测量并记入台账。
- 65 个 Appx 显示名未解析，界面显示 `ms-resource://` 原值。
- 少数带自校验或反作弊机制的软件不兼容 junction，可回滚还原。
- 非管理员运行时存在扫描盲区（开发机实测 97 个目录不可读，多为其他用户目录与系统保护目录）。

## 许可

[MIT](LICENSE)
