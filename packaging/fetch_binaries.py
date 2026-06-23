#!/usr/bin/env python3
"""
fetch_binaries.py — download yt-dlp + ffmpeg (+ ffprobe) for the *current* OS
into packaging/bin/, so PyInstaller can bundle them into a self-contained app.

  python3 packaging/fetch_binaries.py

No third-party deps — uses urllib + zipfile/tarfile from the stdlib.
"""

import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "bin")

YTDLP = {
    "Windows": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
    "Darwin": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos",
    "Linux": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux",
}

# ffmpeg/ffprobe static builds.
# Windows/Linux: BtbN bundles ffmpeg + ffprobe in one archive.
# macOS: ffmpeg-static ships a native, dependency-free binary per arch
#        (evermeet's cookie-on-302 flow breaks urllib). ffprobe isn't included,
#        which is fine — yt-dlp only needs ffmpeg for our re-encode extraction.
# NB: BtbN's rolling build lives under the explicit "latest" *tag*, not GitHub's
# /releases/latest/ (which resolves to the stable nX.Y release that lacks the
# master-* assets -> 404).
FFMPEG_WIN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
FFMPEG_LINUX = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"


def ffmpeg_mac_url():
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    return f"https://github.com/eugeneware/ffmpeg-static/releases/latest/download/ffmpeg-darwin-{arch}"


def log(msg):
    print(msg, flush=True)


def download(url, dest, attempts=4):
    log(f"  downloading {url}")
    last = None
    for i in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wax-digger-build"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            if os.path.getsize(dest) > 0:
                return
            raise RuntimeError("empty download")
        except (urllib.error.URLError, RuntimeError, TimeoutError) as e:
            last = e
            log(f"    retry {i}/{attempts} ({e})")
            time.sleep(3 * i)
    raise RuntimeError(f"failed to download {url}: {last}")


def make_executable(path):
    if os.name != "nt":
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def save_binary(src, name):
    ext = ".exe" if os.name == "nt" else ""
    dest = os.path.join(BIN, name + ext)
    shutil.copyfile(src, dest)
    make_executable(dest)
    log(f"  saved bin/{name}{ext}")


def extract_from_archive(archive, member_names, out_name):
    """Pull the first matching member (by basename) out of a zip/tar archive."""
    tmp = tempfile.mkdtemp()
    try:
        if archive.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                target = _find_member(z.namelist(), member_names)
                z.extract(target, tmp)
                save_binary(os.path.join(tmp, target), out_name)
        else:  # tar.xz / tar.gz
            with tarfile.open(archive) as t:
                names = t.getnames()
                target = _find_member(names, member_names)
                t.extract(target, tmp)
                save_binary(os.path.join(tmp, target), out_name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _find_member(names, wanted_basenames):
    for n in names:
        base = os.path.basename(n).lower()
        if base in wanted_basenames:
            return n
    raise RuntimeError(f"none of {wanted_basenames} found in archive ({len(names)} entries)")


def fetch_ytdlp(system):
    log("yt-dlp:")
    url = YTDLP[system]
    tmp = os.path.join(tempfile.gettempdir(), os.path.basename(url))
    download(url, tmp)
    save_binary(tmp, "yt-dlp")


def fetch_ffmpeg(system):
    log("ffmpeg:")
    if system == "Windows":
        arc = os.path.join(tempfile.gettempdir(), "ffmpeg-win.zip")
        download(FFMPEG_WIN, arc)
        extract_from_archive(arc, {"ffmpeg.exe"}, "ffmpeg")
        extract_from_archive(arc, {"ffprobe.exe"}, "ffprobe")
    elif system == "Linux":
        arc = os.path.join(tempfile.gettempdir(), "ffmpeg-linux.tar.xz")
        download(FFMPEG_LINUX, arc)
        extract_from_archive(arc, {"ffmpeg"}, "ffmpeg")
        extract_from_archive(arc, {"ffprobe"}, "ffprobe")
    elif system == "Darwin":
        tmp = os.path.join(tempfile.gettempdir(), "ffmpeg-mac")
        download(ffmpeg_mac_url(), tmp)
        save_binary(tmp, "ffmpeg")  # native static binary, no ffprobe needed
    else:
        raise RuntimeError(f"unsupported OS: {system}")


def main():
    system = platform.system()
    if system not in YTDLP:
        log(f"Unsupported platform: {system}")
        sys.exit(1)

    os.makedirs(BIN, exist_ok=True)
    log(f"Fetching binaries for {system} -> packaging/bin/")
    fetch_ytdlp(system)
    fetch_ffmpeg(system)
    log("Done.")


if __name__ == "__main__":
    main()
