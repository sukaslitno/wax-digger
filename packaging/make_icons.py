#!/usr/bin/env python3
"""
make_icons.py — render the Wax Digger app icon (the ↓ mark) at 1024px and export
platform icon files: icon.icns (macOS) and icon.ico (Windows).

  python3 packaging/make_icons.py

Requires Pillow (pip install pillow).
"""

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))

DARK = (33, 28, 19, 255)     # #211c13
CREAM = (232, 225, 206, 255)  # #e8e1ce
N = 1024


def render():
    img = Image.new("RGBA", (N, N), CREAM)  # cream = outer frame
    d = ImageDraw.Draw(img)

    # dark tile inset from the cream frame
    frame = int(N * 0.07)
    d.rectangle([frame, frame, N - frame, N - frame], fill=DARK)

    cx = N / 2
    shaft_half = N * 0.085
    # shaft
    d.rectangle(
        [cx - shaft_half, N * 0.2, cx + shaft_half, N * 0.56],
        fill=CREAM,
    )
    # arrowhead
    d.polygon(
        [
            (cx - N * 0.26, N * 0.46),
            (cx + N * 0.26, N * 0.46),
            (cx, N * 0.82),
        ],
        fill=CREAM,
    )
    return img


def main():
    img = render()

    png = os.path.join(HERE, "app_icon.png")
    img.save(png)
    print(f"✓ {os.path.relpath(png, HERE)}")

    ico = os.path.join(HERE, "icon.ico")
    img.save(ico, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"✓ {os.path.relpath(ico, HERE)}")

    icns = os.path.join(HERE, "icon.icns")
    try:
        img.save(icns)
        print(f"✓ {os.path.relpath(icns, HERE)}")
    except Exception as e:
        print(f"! could not write icon.icns ({e}); macOS build can fall back to iconutil")


if __name__ == "__main__":
    main()
