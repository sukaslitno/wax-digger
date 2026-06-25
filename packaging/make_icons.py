#!/usr/bin/env python3
"""
make_icons.py — create platform icon files from icon_source.png.

  python3 packaging/make_icons.py

Requires Pillow (pip install pillow).
On macOS also uses iconutil (built-in) to produce a proper .icns.
"""

import os
import shutil
import subprocess

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))

# Sizes required by macOS iconutil (.iconset convention)
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]

# Sizes baked into the Windows .ico
ICO_SIZES = [256, 128, 64, 48, 32, 16]


def make_square(img: Image.Image, size: int = 1024) -> Image.Image:
    """Fit img into a square canvas with white background, then resize."""
    canvas = Image.new("RGBA", (max(img.width, img.height),) * 2, (255, 255, 255, 255))
    off_x = (canvas.width - img.width) // 2
    off_y = (canvas.height - img.height) // 2
    if img.mode == "RGBA":
        canvas.paste(img, (off_x, off_y), img)
    else:
        canvas.paste(img, (off_x, off_y))
    return canvas.resize((size, size), Image.LANCZOS)


def build_icns(base: Image.Image, out_path: str) -> bool:
    """Try iconutil (macOS); return True on success."""
    if not shutil.which("iconutil"):
        return False

    iconset = os.path.join(HERE, "AppIcon.iconset")
    os.makedirs(iconset, exist_ok=True)

    # iconutil expects exactly these filenames
    pairs = [
        (16,   "icon_16x16.png"),
        (32,   "icon_16x16@2x.png"),
        (32,   "icon_32x32.png"),
        (64,   "icon_32x32@2x.png"),
        (128,  "icon_128x128.png"),
        (256,  "icon_128x128@2x.png"),
        (256,  "icon_256x256.png"),
        (512,  "icon_256x256@2x.png"),
        (512,  "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for px, name in pairs:
        base.resize((px, px), Image.LANCZOS).save(os.path.join(iconset, name))

    result = subprocess.run(
        ["iconutil", "-c", "icns", "-o", out_path, iconset],
        capture_output=True,
    )
    shutil.rmtree(iconset)
    return result.returncode == 0


def main():
    src = os.path.join(HERE, "icon_source.png")
    img = Image.open(src)

    base = make_square(img, 1024)

    png = os.path.join(HERE, "app_icon.png")
    base.save(png)
    print(f"wrote {os.path.relpath(png, HERE)}")

    ico = os.path.join(HERE, "icon.ico")
    base.save(ico, sizes=[(s, s) for s in ICO_SIZES])
    print(f"wrote {os.path.relpath(ico, HERE)}")

    icns = os.path.join(HERE, "icon.icns")
    if build_icns(base, icns):
        print(f"wrote {os.path.relpath(icns, HERE)} (via iconutil)")
    else:
        try:
            base.save(icns)
            print(f"wrote {os.path.relpath(icns, HERE)} (via Pillow fallback)")
        except Exception as e:
            print(f"! could not write icon.icns ({e})")


if __name__ == "__main__":
    main()
