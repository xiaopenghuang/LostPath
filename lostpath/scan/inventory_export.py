r"""调用 export_inventory.ps1 拿软件台账清单（只读）。

单独一个模块而不是塞进 runner：PowerShell 调用有一批 Windows 特有的坑要处理，
和管道编排是两件事。

坑（MEMORY.md 已记，此处按同一范式处理）：
- PowerShell 5.1 按 ANSI(GBK) 解码无 BOM 的 UTF-8 脚本。本脚本自身是纯 ASCII，
  但输出路径可能含中文（仓库就可能放在带中文的目录下），故路径一律走参数
  传入，不进脚本字面量。
- 输出用 utf-8（无 BOM）写，读时用 utf-8-sig 容错——PowerShell 的
  `[IO.File]::WriteAllText` 配 `UTF8Encoding($false)` 不带 BOM，但换个写法就带，
  两种都得能读。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from lostpath.proc import run_hidden

HERE = Path(__file__).resolve().parent
PS1 = HERE / "export_inventory.ps1"

# Get-AppxPackage 在装了大量商店应用的机器上能跑十几秒，Get-ScheduledTask 同理。
# 实测本机 ~8s，留 4 倍余量。
TIMEOUT_SEC = 180


class InventoryExportError(RuntimeError):
    """PowerShell 清单导出失败。带上 stderr 便于排障。"""


def export_inventory(out_path=None):
    """跑 PowerShell 导出台账清单，返回解析后的 dict。

    out_path 为 None 时写到系统临时目录（调用方不关心中间文件时用）。
    """
    if not PS1.is_file():
        raise InventoryExportError(f"找不到导出脚本：{PS1}")

    tmp_created = False
    if out_path is None:
        import tempfile
        out_path = Path(tempfile.gettempdir()) / "lostpath_inventory.json"
        tmp_created = True
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        # run_hidden：打包后引擎无控制台，裸 subprocess 会弹出 PowerShell 黑框。
        # 这一处在扫描的清单导出阶段跑，耗时数秒，弹出来尤其显眼。见 lostpath/proc.py
        proc = run_hidden(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(PS1), "-OutFile", str(out_path)],
            timeout=TIMEOUT_SEC, capture_output=True,
        )
    except subprocess.TimeoutExpired:
        raise InventoryExportError(
            f"清单导出超时（{TIMEOUT_SEC}s）。常见原因是 Get-AppxPackage 卡住")
    except OSError as e:
        raise InventoryExportError(f"无法启动 PowerShell：{e}")

    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise InventoryExportError(
            f"清单导出失败（rc={proc.returncode}）：{err[:500]}")
    if not out_path.is_file():
        raise InventoryExportError(f"导出脚本未产出文件：{out_path}")

    try:
        # 两种 BOM 情况都要能读，见模块 docstring
        data = json.loads(out_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise InventoryExportError(f"清单文件无法解析：{type(e).__name__}: {e}")
    finally:
        if tmp_created:
            try:
                out_path.unlink()
            except OSError:
                pass

    # 空结果不是"没数据"而是"枚举坏了"——本项目在 read_registry 上栽过一次
    # （with 块外句柄已关，EnumKey 全静默失败，返回 0 条看起来像没装软件）。
    if not data.get("apps"):
        raise InventoryExportError(
            "清单里注册表卸载项为 0 条。正常机器不可能没有，"
            "应为枚举失败而非真的没有软件")
    return data


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="导出软件台账清单（只读）")
    ap.add_argument("-o", "--out", default="inventory.json")
    args = ap.parse_args(argv)
    data = export_inventory(args.out)
    print(f"apps={len(data.get('apps') or [])} "
          f"services={len(data.get('services') or [])} "
          f"startup={len(data.get('startup') or [])} "
          f"appPaths={len(data.get('appPaths') or [])} "
          f"appx={len(data.get('appx') or [])} "
          f"tasks={len(data.get('tasks') or [])}")
    print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
