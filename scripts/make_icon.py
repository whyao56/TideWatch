"""生成 exe 用的图标（`packaging/tidewatch.ico`）。

为什么要用代码画而不是找一张图：图标是发行产物的一部分，它必须能被任何人
重新生成、并且**没有来源不明的素材**。三十行 Pillow 就够，比去找一个
「免费商用」的图标再确认授权便宜得多。

图案的意图：深蓝底上是两道潮线（「观潮」），右上角一个红点表示「被标出来的
那一个」。不做文字 —— 16×16 下任何文字都是糊的。

用法：
    python scripts/make_icon.py            # 重新生成并覆盖 ico
    python scripts/make_icon.py --png out/icon-preview.png
"""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "packaging" / "tidewatch.ico"

# 尺寸覆盖任务栏（16）、资源管理器（32/48）、以及高 DPI（256）。
# 少了 256 的话，大图标视图下会被拉伸到模糊。
SIZES = (16, 24, 32, 48, 64, 128, 256)

BG_TOP = (16, 34, 64)
BG_BOTTOM = (10, 22, 44)
WAVE = (94, 214, 214)
WAVE_SOFT = (56, 148, 168)
ALERT = (232, 74, 74)


def _rounded_mask(size: int, radius_ratio: float = 0.22) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * radius_ratio), fill=255)
    return mask


def _background(size: int) -> Image.Image:
    """竖直渐变底。逐行画线而不是用 resize 拉伸，避免出现带状色阶。"""
    img = Image.new("RGB", (1, size))
    px = img.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        px[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM, strict=False))
    return img.resize((size, size), Image.Resampling.NEAREST)


def _draw_waves(draw: ImageDraw.ImageDraw, size: int) -> None:
    """画两道潮线。细尺寸下自动减少点数，否则锯齿比图案还显眼。

    左右各留一段边距：线画到画布边缘时，圆角遮罩会把两端切出毛刺，
    看起来像渲染坏了而不是被裁掉了。
    """
    for index, (color, y_ratio, amp_ratio, width_ratio) in enumerate(
        (
            (WAVE_SOFT, 0.56, 0.085, 0.072),
            (WAVE, 0.71, 0.105, 0.098),
        )
    ):
        amp = size * amp_ratio
        base = size * y_ratio
        width = max(1, round(size * width_ratio))
        left, right = size * 0.085, size * 0.915
        steps = max(24, size) if size >= 32 else 10
        points = []
        for step in range(steps + 1):
            t = step / steps
            x = left + t * (right - left)
            # 两个不同频率的正弦叠加，得到「潮」的不规则感，而不是一条规整的波纹。
            phase = math.sin(t * math.pi * 2 + index * 0.9) * amp
            phase += math.sin(t * math.pi * 4 + index * 1.7) * amp * 0.35
            points.append((x, base + phase))
        draw.line(points, fill=color, width=width, joint="curve")
        # 两端补圆头。line 的 joint 只管拐点，端点默认是平的，
        # 在细线宽下会显得像被截断。
        radius = width / 2
        for x, y in (points[0], points[-1]):
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)


def _draw_alert(draw: ImageDraw.ImageDraw, size: int) -> None:
    """右上角的红点 —— 「这一条被标出来了」。"""
    r = max(1.4, size * 0.085)
    cx, cy = size * 0.735, size * 0.265
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ALERT)


def render(size: int) -> Image.Image:
    base = _background(size).convert("RGBA")
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    _draw_waves(draw, size)
    _draw_alert(draw, size)
    base = Image.alpha_composite(base, layer)
    base.putalpha(_rounded_mask(size))
    return base


def _setup_console() -> None:
    """把标准输出切到 UTF-8 —— 见 scripts/build_exe.py 里同名函数的说明。

    英文 Windows 上 stdout 是 cp1252，编不了汉字，下面那两条「已写入 …」
    会直接把它打挂。中文机器上是 GBK，恰好能编码，所以本地跑不出来。
    """
    if os.name == "nt":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    # 必须在任何 print 之前。
    _setup_console()

    parser = argparse.ArgumentParser(description="生成 TideWatch 图标")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="ico 输出路径")
    parser.add_argument("--png", default=None, help="额外导出一张 256px PNG 预览")
    args = parser.parse_args()

    frames = [render(size) for size in SIZES]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 用最大的那一帧作底，Pillow 会按 sizes 逐档缩放写入，生成多尺寸 ico。
    frames[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"已写入 {out}（{', '.join(f'{s}x{s}' for s in SIZES)}）")

    if args.png:
        png = Path(args.png)
        png.parent.mkdir(parents=True, exist_ok=True)
        frames[-1].resize((256, 256), Image.Resampling.LANCZOS).save(png)
        print(f"已写入预览 {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
