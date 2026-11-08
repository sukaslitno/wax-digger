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

from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))

# Sizes required by macOS iconutil (.iconset convention)
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]

# Sizes baked into the Windows .ico
ICO_SIZES = [256, 128, 64, 48, 32, 16]

# macOS icon grid: the artwork is an inset rounded square, not a full-bleed one,
# so the icon lines up with every other icon in the dock.
MAC_CANVAS = 1024
MAC_SHAPE = 824
MAC_RADIUS = 185


def make_square(img: Image.Image, size: int = 1024) -> Image.Image:
    """Fit img into a transparent square canvas, then resize."""
    side = max(img.width, img.height)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    img = img.convert("RGBA")
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    return canvas.resize((size, size), Image.LANCZOS)


def rounded_macos(base: Image.Image) -> Image.Image:
    """Inset the artwork and round its corners the way macOS icons are drawn."""
    art = base.resize((MAC_SHAPE,) * 2, Image.LANCZOS)

    ss = 4  # draw the mask oversized so the corners downscale smooth
    mask = Image.new("L", (MAC_SHAPE * ss,) * 2, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, MAC_SHAPE * ss - 1, MAC_SHAPE * ss - 1], radius=MAC_RADIUS * ss, fill=255
    )
    art.putalpha(ImageChops.multiply(art.getchannel("A"), mask.resize((MAC_SHAPE,) * 2, Image.LANCZOS)))

    canvas = Image.new("RGBA", (MAC_CANVAS,) * 2, (0, 0, 0, 0))
    off = (MAC_CANVAS - MAC_SHAPE) // 2
    canvas.paste(art, (off, off), art)
    return canvas


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

    base = make_square(img, 1024)   # full bleed — Windows icons are square
    mac = rounded_macos(base)       # inset + rounded — macOS dock and Finder

    png = os.path.join(HERE, "app_icon.png")
    mac.save(png)
    print(f"wrote {os.path.relpath(png, HERE)}")

    ico = os.path.join(HERE, "icon.ico")
    base.save(ico, sizes=[(s, s) for s in ICO_SIZES])
    print(f"wrote {os.path.relpath(ico, HERE)}")

    icns = os.path.join(HERE, "icon.icns")
    if build_icns(mac, icns):
        print(f"wrote {os.path.relpath(icns, HERE)} (via iconutil)")
    else:
        try:
            base.save(icns)
            print(f"wrote {os.path.relpath(icns, HERE)} (via Pillow fallback)")
        except Exception as e:
            print(f"! could not write icon.icns ({e})")


if __name__ == "__main__":
    main()
