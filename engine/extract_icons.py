"""批量提取软件图标：exe/ico → PNG（用户目录的 icons/）。

用 PowerShell System.Drawing 的 ExtractAssociatedIcon（系统自带，无需管理员）。
路径经 JSON 文件传递，规避 GBK/引号坑（见 MEMORY.md）。

输出目录取自 lostpath.storage.paths：图标是从本机 exe 提取的，属用户数据。
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lostpath.storage import paths as _paths  # noqa: E402

ICONS_DIR = _paths.icons_dir()

PS_TEMPLATE = """$jobs = Get-Content -Raw -Encoding UTF8 '{json}' | ConvertFrom-Json
Add-Type -AssemblyName System.Drawing
foreach ($j in $jobs) {{
  try {{
    $src = $j.src
    $out = $j.out
    if ($src.ToLower().EndsWith('.ico')) {{
      Copy-Item -LiteralPath $src -Destination $out -Force
    }} else {{
      $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($src)
      if ($icon -ne $null) {{
        $icon.ToBitmap().Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
      }}
    }}
  }} catch {{}}
}}
"""


def missing_jobs(entities: list[dict]) -> list[dict]:
    """返回 [{src, out}]：有 icon_src 但 PNG 还没生成的条目。

    目标文件名取自 icon_slug（恒有值），而非 icon（仅在 PNG 已存在时才有值）——
    否则待提取的实体正好都被跳过。
    """
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []
    for e in entities:
        src = e.get("icon_src")
        slug = e.get("icon_slug")
        if not src or not slug:
            continue
        out_path = ICONS_DIR / f"{slug}.png"
        if out_path.exists():
            continue
        jobs.append({"src": src, "out": str(out_path)})
    return jobs


def run_extraction(jobs: list[dict]) -> int:
    if not jobs:
        return 0
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.gettempdir()) / "lostpath_icon_jobs"
    tmp.mkdir(exist_ok=True)
    json_file = tmp / "jobs.json"
    ps1_file = tmp / "extract_icons.ps1"
    json_file.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8-sig")
    ps1_file.write_text(PS_TEMPLATE.format(json=str(json_file)), encoding="utf-8-sig")
    try:
        # run_hidden：打包后引擎无控制台，裸 subprocess 会弹 PowerShell 黑框。
        # 这一处在引擎启动时后台补图标，弹出来的话每次开软件都闪一下。
        # 见 lostpath/proc.py
        from lostpath.proc import run_hidden
        run_hidden(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1_file)],
            timeout=300, capture_output=True,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    done = sum(1 for j in jobs if Path(j["out"]).exists())
    return done
