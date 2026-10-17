"""生成应用图标 zhihu2md.ico。

用法：
    python tools/make_icon.py                         # 内置绘制的「知」字图标
    python tools/make_icon.py --source asuka.png      # 用指定 PNG 当图标

会输出多尺寸 ICO（16~256），并额外写一张 256 的 *.preview.png 方便肉眼检查。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def square_from_source(path: Path) -> Image.Image:
    """读入图片，居中补成正方形（透明边），保证缩放不变形。"""
    img = Image.open(path).convert("RGBA")
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    return canvas


def drawn_icon() -> Image.Image:
    """默认图标：蓝色圆角方块 + 白色「知」。"""
    size = 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        r = int(0x2B + (0x12 - 0x2B) * t)
        g = int(0x6C + (0x3D - 0x6C) * t)
        b = int(0xB0 + (0x78 - 0xB0) * t)
        draw.line([(0, y), (size, y)], fill=(r, g, b, 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=112, fill=255)
    img.putalpha(mask)

    font = None
    for candidate in (
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ):
        if Path(candidate).exists():
            try:
                font = ImageFont.truetype(candidate, 300)
                break
            except OSError:
                continue
    if font is not None:
        bbox = draw.textbbox((0, 0), "知", font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]), "知",
                  font=font, fill=(255, 255, 255, 246))
    return img


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 zhihu2md.ico")
    parser.add_argument("--source", default="", help="用作图标的 PNG/JPG 路径")
    parser.add_argument("--out", default=str(ROOT / "zhihu2md.ico"))
    args = parser.parse_args()

    if args.source:
        source = Path(args.source)
        if not source.exists():
            raise SystemExit("找不到图片：%s" % source)
        image = square_from_source(source)
        print("用图片生成图标：%s %s" % (source, Image.open(source).size))
    else:
        image = drawn_icon()
        print("用内置图形生成图标")

    out = Path(args.out)
    image.save(out, sizes=SIZES)
    preview = out.with_name(out.stem + ".preview.png")
    image.resize((256, 256), Image.LANCZOS).save(preview)
    print("wrote %s (%d B) 和 %s" % (out, out.stat().st_size, preview.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
