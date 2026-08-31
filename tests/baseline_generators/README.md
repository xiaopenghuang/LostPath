# 基准生成器（历史留档，不可在 fixtures 上重跑）

`tests/fixtures/machine-a/truth.json` 是本项目唯一的归因质量标尺。这两个脚本是
它的产出过程，留在仓库里是为了让"基准本身可被审计"——否则基准就成了无来源的
断言，而归因引擎的全部可信度都压在它上面。

| 脚本 | 作用 |
|---|---|
| `build_truth.py` | 初版 37 条：证据源 T1 junction 目标 / T2 Appx 家族名精确相等 / T3 服务与启动项 exe 路径分量 / T4 已人工逐条核验 |
| `extend_truth.py` | 补到 41 条：T5 TMP/TEMP 环境变量指向 / T6 子目录形态证明多产品容器 / T7 目录内安装包文件名含产品名。补的四条（Package Cache + 三个 updater）原本全在基准盲区里，不补则等于让 v4 自己给自己划考纲 |

**两条铁律**（改基准前先读）：

1. **不 import 归因知识库。** 用引擎自己的推断规则去评判引擎是循环论证。
2. **只收可独立程序化验证的证据。** 拿不到硬证据的目录一律不标，宁可缩小分母，
   不可掺入猜测——分母掺假比分子出错更难发现。

**为什么不能在 fixtures 上重跑**：T1 要 `os.readlink()` 解 junction 真实目标，
T6 要读子目录实际形态，都依赖被扫机器的活文件系统。脱敏 fixtures 里的
`C:\Users\devuser\...` 在任何真机上都不存在，重跑只会得到一份更小且不同的基准。
它们当年跑在 machine-a（开发者本机，Windows 10 19045，非管理员会话）上。

**要给新机器建基准就在那台机器上跑这两个脚本**：需先备齐 `inventory.json`
（`lostpath/scan/export_inventory.ps1` 产出）、`scan_c.json`
（`lostpath/scan/scan_dirs.py`）、`attribution_v4.json`（归因输出，仅取
path/name/size 三个中性字段），改脚本里的 `BASE` 指向该目录，产出后再脱敏。
