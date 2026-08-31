r"""把 ico/LostPath.png 转成多尺寸 .ico，供 Electron 窗口与 NSIS 安装包使用。

为什么要多尺寸：Windows 在不同位置取不同尺寸（任务栏 32、桌面 48、资源管理器大图标
256）。只塞一个 256 的话，小尺寸处由系统临时缩放，边缘会糊。

一次性工具，产物 ico/LostPath.ico 提交进仓库——构建机不该依赖 Pillow。

    python tools/make_icon.py
"""
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "ico" / "LostPath.png"
DST = ROOT / "ico" / "LostPath.ico"

# Windows 实际会取用的尺寸。16 也留着：小图标模式与标题栏会用
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    if not SRC.is_file():
        print(f"找不到源图：{SRC}", file=sys.stderr)
        return 1
    img = Image.open(SRC)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    w, h = img.size
    if w != h:
        # 非正方形会被 Windows 拉伸变形，补成正方形画布居中放置
        side = max(w, h)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(img, ((side - w) // 2, (side - h) // 2))
        img = canvas
        print(f"源图 {w}x{h} 非正方形，已补成 {side}x{side}")
    img.save(DST, format="ICO", sizes=SIZES)
    print(f"已生成 {DST}（{DST.stat().st_size} 字节，{len(SIZES)} 个尺寸）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
