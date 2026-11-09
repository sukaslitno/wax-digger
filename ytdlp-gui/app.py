import collections
import itertools
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import webview
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

import ytdlp_update
from i18n import get_language, normalize as normalize_language, set_language, system_language, tr

HOST = "127.0.0.1"
PORT = 7337
URL = f"http://{HOST}:{PORT}"
IS_WINDOWS = os.name == "nt"

# Shown on the info screen (served by /status). The one place to bump it.
APP_VERSION = "1.1.2"

# The UI is a fixed 600px-wide modal; height follows the screen it shows
# (260 empty / 336 folder / 396 info / 432 filled) via Api.resize().
WINDOW_WIDTH = 600
WINDOW_HEIGHT = 260


# ---------------------------------------------------------------------------
# Resource resolution — works both in dev and inside a PyInstaller bundle.
# ---------------------------------------------------------------------------
def resource_dir():
    """Directory that holds bundled data (templates, bin/) at runtime."""
    if getattr(sys, "frozen", False):
        # PyInstaller extracts datas/binaries here (onefile) or next to the exe.
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


RES = resource_dir()


def bundled_binary(name):
    """Return path to a yt-dlp/ffmpeg binary shipped inside the app, or None."""
    exe = name + (".exe" if IS_WINDOWS else "")
    candidate = os.path.join(RES, "bin", exe)
    return candidate if os.path.isfile(candidate) else None


# Search path for a system yt-dlp when nothing is bundled (dev convenience).
YTDLP_EXTRA_PATHS = [
    os.path.expanduser("~/Library/Python/3.9/bin"),
    os.path.expanduser("~/Library/Python/3.10/bin"),
    os.path.expanduser("~/Library/Python/3.11/bin"),
    os.path.expanduser("~/Library/Python/3.12/bin"),
    os.path.expanduser("~/Library/Python/3.13/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
]


def find_ytdlp():
    bundled = bundled_binary("yt-dlp")
    if bundled:
        return [bundled]
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    for p in YTDLP_EXTRA_PATHS:
        candidate = os.path.join(p, "yt-dlp")
        if os.path.isfile(candidate):
            return [candidate]
    # last resort: run as a python module
    return [sys.executable, "-m", "yt_dlp"]


def find_ffmpeg_location():
    """Directory containing a bundled ffmpeg, passed to yt-dlp. None = use PATH."""
    bundled = bundled_binary("ffmpeg")
    if bundled:
        return os.path.dirname(bundled)
    return None


FFMPEG_LOCATION = find_ffmpeg_location()
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".wax-digger_config.json")


def find_gofi():
    bundled = bundled_binary("wax-gofi")
    if bundled:
        return bundled
    exe = "wax-gofi.exe" if IS_WINDOWS else "wax-gofi"
    candidate = os.path.join(os.path.dirname(RES), "packaging", "bin", exe)
    return candidate if os.path.isfile(candidate) else None


def _link_host(url):
    try:
        parsed = urlsplit(url if "://" in url else "https://" + url)
        return parsed.hostname if parsed.scheme in ("http", "https") else None
    except ValueError:
        return None


def spotify_link(url):
    return _link_host(url) in ("open.spotify.com", "www.open.spotify.com")


# Links that may be saved as video (shorts included). music.youtube.com is
# left out on purpose: it is a music service, its tracks are mostly a still
# cover over the song, so it stays audio-only like every other music site.
YOUTUBE_VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}


def youtube_video_link(url):
    return _link_host(url) in YOUTUBE_VIDEO_HOSTS


def existing_gofi_session():
    # Store only the path, never the credential, in Wax's settings.
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            path = json.load(f).get("gofi_config_path", "")
    except (OSError, ValueError, AttributeError):
        path = ""
    if not path and not getattr(sys, "frozen", False):
        path = os.path.join(os.path.dirname(RES), "deezload", "DeezLoader-Reborn", "GoFi", "d-fi.config.json")
    try:
        with open(path, encoding="utf-8") as f:
            return str(json.load(f).get("cookies", {}).get("arl", "")).strip()
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


deezer_arl = os.environ.get("DEEZER_ARL", "").strip() or existing_gofi_session()

DEFAULT_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads")
TEMP_DIR = os.path.join(tempfile.gettempdir(), "wax-digger-temp")

app = Flask(__name__, template_folder=os.path.join(RES, "templates"))

history = []
active_streams = {}
_stream_ids = itertools.count(1)
# A stream nobody reads is dropped this long after its download ends.
STREAM_TTL = 60
active_procs = set()

FORMATS = {
    "mp3_128": ["-x", "--audio-format", "mp3", "--audio-quality", "5"],
    "mp3_320": ["-x", "--audio-format", "mp3", "--audio-quality", "0"],
    "flac":    ["-x", "--audio-format", "flac"],
    "wav":     ["-x", "--audio-format", "wav"],
    # AAC in an .m4a (MP4) container: raw .aac (ADTS) cannot hold tags or a
    # cover, and an AAC source (YouTube itag 140) is copied without re-encoding.
    "aac":     ["-x", "--audio-format", "m4a", "--audio-quality", "0"],
}

# ---------------------------------------------------------------------------
# Video (YouTube links only): video + sound in one .mp4, never re-encoded.
# The quality is a cap: "-S res:N" takes the largest resolution not above N
# and only falls back to the smallest one above it if nothing is that small,
# so a missing exact height never fails the download. "res" is the shorter
# side, so a vertical 1080x1920 short counts as 1080p, as YouTube labels it.
# Up to 1080p H.264 + AAC is preferred: it plays in QuickTime and every other
# player. YouTube has no H.264 above 1080p, so 1440p/2160p come as AV1 or
# VP9 (still put in .mp4, which ffmpeg muxes without re-encoding; mkv would
# open in even fewer players). AAC audio is preferred for the same reason.
# ---------------------------------------------------------------------------
VIDEO_FORMATS = {f"video_{h}": h for h in (360, 480, 720, 1080, 1440, 2160)}
H264_MAX_HEIGHT = 1080


def video_sort(height):
    """The -S format sort of a video quality."""
    fields = [f"res:{height}"]
    if height <= H264_MAX_HEIGHT:
        fields.append("vcodec:h264")
    fields.append("acodec:aac")
    return ",".join(fields)


for _fmt, _height in VIDEO_FORMATS.items():
    FORMATS[_fmt] = ["-f", "bv*+ba/b", "-S", video_sort(_height), "--merge-output-format", "mp4"]

# Spotify links go to the Deezer adapter, which only has these.
SPOTIFY_FORMATS = ("mp3_128", "mp3_320", "flac")

# ---------------------------------------------------------------------------
# File layout. yt-dlp has no "if this is a playlist" switch in -o, so the name
# parts are computed into wax_* fields with --parse-metadata (each rule only
# sets its fields when its regex matches; later rules override earlier ones)
# and -o just glues them together:
#   single track  -> <out>/Artist - Title [320].ext
#   album         -> <out>/Album/001 - Artist - Title [320].ext
#   playlist      -> <out>/Playlist/001 - Artist - Title [320].ext
# This is the same layout the Spotify adapter (vendor/gofi/cmd/wax-gofi) uses,
# so every link type lands the same way.
# The folder only uses playlist-level fields, so every track of one link lands
# in the same folder. yt-dlp sanitizes each field (":" "?" "/" … become
# full-width look-alikes, valid on macOS and Windows) and on Windows also fixes
# trailing dots/spaces and reserved names itself, so --windows-filenames (which
# would mangle "Vol. 2..." on macOS too) is not passed.
# ---------------------------------------------------------------------------
# Quality tag in the file name, as the Spotify adapter writes it.
QUALITY_TAGS = {"mp3_128": "128", "mp3_320": "320", "flac": "flac", "wav": "wav", "aac": "aac"}


def output_template(fmt):
    if fmt in VIDEO_FORMATS:
        # the height actually downloaded (wax_res, see VIDEO_RULES); the
        # requested one only if yt-dlp does not know it
        tag = "%(wax_res&{}p|" + f"{VIDEO_FORMATS[fmt]}p" + ")s"
    else:
        tag = QUALITY_TAGS[fmt]
    return "%(wax_dir|.)s/%(wax_num&{} - |)s%(wax_file)s [" + tag + "].%(ext)s"

_DASH = r"\s+[-–—]\s+"   # " - ", " – ", " — "
NAMING_RULES = [
    # default name: Artist - Title
    "%(artist,uploader,channel|Unknown)s - %(track,title)s:%(wax_file)s",
    # the title already starts with the artist ("Rick Astley - Never Gonna…"):
    # keep it as is instead of "Rick Astley - Rick Astley - …"; tag the rest
    "%(artist,uploader,channel|)s\n%(track,title)s"
    ":(?is)^(?P<wax_x>[^\n]+)\n(?P<wax_file>(?P=wax_x)" + _DASH + "(?P<meta_title>.+))$",
    # no artist metadata, but the title reads "Artist - Title" (label channels)
    "%(artist|)s\n%(track|)s\n%(title)s"
    ":(?s)^\n\n(?P<wax_file>(?P<meta_artist>[^\n]+?)" + _DASH + "(?P<meta_title>.+))$",
    # inside a playlist: folder = playlist title, number = position
    # (a folder needs a letter or digit, so a title like ".." cannot climb out)
    "%(playlist_index|)s\n%(playlist_title,playlist|)s"
    ":(?s)^\\d+\n(?P<wax_dir>[^\n]*\\w[^\n]*)$",
    "%(playlist_index)03d:(?P<wax_num>\\d+)",
    # an album (its tracks' album == the playlist title): number by the
    # album's own track numbers instead of the position in the list
    "%(playlist_index|)s\n%(playlist_title|)s\n%(album|)s\n%(track_number)03d"
    ":(?s)^\\d+\n(?P<wax_p>[^\n]+)\n(?P=wax_p)\n(?P<wax_num>\\d+)$",
]

# Tags on top of what --embed-metadata maps by itself (title, artist, album,
# album_artist, track_number, upload_date as date).
TAG_RULES = [
    "%(release_year,release_date>%Y,upload_date>%Y|)s:%(meta_date)s",
    "%(track_number,playlist_index|)s:%(meta_track)s",
]

# Video quality tag: the downloaded format's shorter side, as YouTube labels
# it (a 1080x1920 short is 1080p). Runs once the format is chosen ("video:").
VIDEO_RULES = [
    "video:%(height)s:^(?P<wax_res>\\d+)$",
    "video:%(aspect_ratio)s %(width)s:^0\\.\\d+ (?P<wax_res>\\d+)$",  # taller than wide
]

# Center-crop covers to a square (YouTube thumbnails are 16:9). Only applies
# when the thumbnail is actually converted (webp -> jpg); jpg covers from
# Bandcamp/SoundCloud are already square and are left alone.
SQUARE_COVER = 'ThumbnailsConvertor+ffmpeg_o:-vf "crop=min(iw\\,ih):min(iw\\,ih)"'
# Containers that can carry embedded cover art (WAV cannot).
COVER_FORMATS = {"mp3_128", "mp3_320", "flac", "aac"}


def build_ytdlp_command(url, fmt, output_path, ytdlp=None, ffmpeg_location=None, temp_dir=None):
    fmt = fmt if fmt in FORMATS else "mp3_320"
    video = fmt in VIDEO_FORMATS
    cmd = list(ytdlp if ytdlp is not None else ytdlp_manager.current()) + FORMATS[fmt]
    if not video:
        # one download pass (no separate video+audio + merge), one progress ramp
        cmd += ["-f", "bestaudio/best"]
    cmd += [
        "--newline",      # progress updates as lines, not \r-glued into one blob
        "--no-playlist",  # a track link with ?list= context is still one track
        "--no-colors",
        "--ignore-config",       # a user's own yt-dlp config must not move files elsewhere
        "--no-abort-on-error",   # a dead track in a playlist skips, the rest still downloads
        "-o", output_template(fmt),
        "-P", output_path,
        # each run gets its own temp dir, so a cancelled run's partial files
        # can be removed without touching anything else
        "-P", f"temp:{temp_dir or TEMP_DIR}",
        "--embed-metadata",
    ]
    for rule in NAMING_RULES + TAG_RULES + (VIDEO_RULES if video else []):
        cmd += ["--parse-metadata", rule]
    if fmt in COVER_FORMATS:
        cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg", "--ppa", SQUARE_COVER]
    elif video:
        # the thumbnail as the file's cover, kept 16:9 like the video
        cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
    if ffmpeg_location:
        cmd += ["--ffmpeg-location", ffmpeg_location]
    cmd += ["--", url]
    return cmd


# ---------------------------------------------------------------------------
# yt-dlp output -> the same __WAX__ events the Spotify adapter emits.
# ---------------------------------------------------------------------------
ERROR_MESSAGES = [
    # order matters: specific causes before broad ones like "unable to download"
    (("drm",), "err.drm"),
    (("http error 429", "too many requests", "rate-limit", "rate limit"),
     "err.rate"),
    (("ffmpeg not found", "ffprobe and ffmpeg not found", "ffmpeg is not installed",
      "ffmpeg could not be found"), "err.ffmpeg"),
    (("private video", "this video is private", "is private", "private track", "private playlist"),
     "err.private"),
    (("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
     "err.age"),
    (("not a bot",), "err.bot"),
    (("err.country", "not made this video available in your country",
      "blocked it in your country", "geo restrict", "geo-restrict", "geoblock", "your location"),
     "err.country"),
    (("sign in", "log in", "login required", "logged-in", "members-only", "join this channel",
      "requires authentication", "account cookies", "premium"),
     "err.signin"),
    (("live event will begin", "premieres in", "is not yet available", "this live stream"),
     "err.not_started"),
    (("unsupported url", "is not a valid url", "no suitable extractor"), "err.unsupported"),
    (("video unavailable", "has been removed", "been terminated", "no longer available",
      "does not exist", "not found", "http error 404", "http error 410", "copyright",
      "is unavailable", "not available", "this track was deleted"),
     "err.gone"),
    (("http error 403", "forbidden"), "err.forbidden"),
    (("failed to resolve", "getaddrinfo", "name or service not known", "nodename nor servname",
      "temporary failure in name resolution", "no address associated", "network is unreachable",
      "connection refused", "connection reset", "timed out", "unable to connect",
      "no route to host", "urlopen error", "errno 8", "errno 11001", "remote end closed",
      "ssl", "unable to download webpage"),
     "err.network"),
    (("no space left", "disk full"), "err.disk"),
    (("permission denied", "read-only file system", "access is denied"), "err.no_write"),
]
GENERIC_ERROR = "err.generic"  # a key, like the rest of ERROR_MESSAGES


def error_key(line):
    """The message key for a raw yt-dlp ERROR line (GENERIC_ERROR if nothing matches)."""
    text = (line or "").lower()
    for needles, key in ERROR_MESSAGES:
        if any(n in text for n in needles):
            return key
    return GENERIC_ERROR


def friendly_error(line):
    """Turn a raw yt-dlp ERROR line into a short lowercase message for the UI,
    in the language the window is set to."""
    return tr(error_key(line))


def extractor_outdated(line):
    """Does this yt-dlp ERROR look like a stale extractor (fixed by updating)?

    Errors with a known, unrelated cause (sign-in, bot check, private, geo,
    rate limit, network…) never count, even if yt-dlp asks for a bug report.
    """
    text = (line or "").lower()
    if any(hint in text for hint in ytdlp_update.OUTDATED_SURE):
        return True
    if error_key(line) not in (GENERIC_ERROR, "err.forbidden"):
        return False
    return any(hint in text for hint in ytdlp_update.OUTDATED_HINTS)


def common_folder(paths):
    """The folder that holds all of these files ('' if they share none)."""
    try:
        return os.path.commonpath([os.path.dirname(p) for p in paths])
    except ValueError:
        return ""


_ITEM_RE = re.compile(r"^\[download\] Downloading item (\d+) of (\d+)")
_PERCENT_RE = re.compile(r"^\[download\]\s+([\d.]+)%")
_CONVERT_RE = re.compile(r"^\[(ExtractAudio|Metadata|EmbedThumbnail|FixupM4a|FixupM3u8)\]")
# the audio file of the current item is on disk (converted, or kept as is)
_DONE_RE = re.compile(r"^\[ExtractAudio\] (Destination: |Not converting audio)|has already been downloaded")

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".opus", ".ogg"}
_MOVED_RE = re.compile(r'^\[MoveFiles\] Moving file "(.+)" to "(.+)"$')
_AUDIO_PATH_RE = re.compile(
    r"^(?:\[ExtractAudio\] Destination: (.+)"
    r"|\[ExtractAudio\] Not converting audio (.+?);"
    r"|\[download\] (.+) has already been downloaded)$")

# share of a track's slice of the ring spent downloading; the rest is ffmpeg
DOWNLOAD_SHARE = 0.9

# Video: two streams, video first, then audio, then ffmpeg merges them. The
# video stream is by far the bigger one, so it gets most of the download share.
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}
VIDEO_STREAM_SHARES = {1: (DOWNLOAD_SHARE,), 2: (0.75, DOWNLOAD_SHARE - 0.75)}
_FORMATS_RE = re.compile(r"^\[info\] .+: Downloading \d+ format\(s\): (\S+)")
_DESTINATION_RE = re.compile(r"^\[download\] Destination: ")
_MERGE_RE = re.compile(r'^\[Merger\] Merging formats into "(.+)"$')
_VIDEO_POST_RE = re.compile(r"^\[(Merger|Metadata|EmbedThumbnail|Fixup\w+|VideoRemuxer|VideoConvertor)\]")
_VIDEO_PATH_RE = re.compile(r"^\[download\] (.+) has already been downloaded$")
# a stream yt-dlp downloads before merging ("... [720p].f136.mp4"), never the result
_PART_RE = re.compile(r"\.f[\w-]+\.\w+$")


class YtdlpProgress:
    """Feed yt-dlp stdout lines, get UI events back.

    Progress is overall: on an album each track owns 1/M of the ring, so the
    ring no longer hits 100% after the first file. A video item is two streams
    and a merge; its slice is split between them (VIDEO_STREAM_SHARES).
    """

    def __init__(self, output_path=None, temp_dir=None, video=False):
        # with an output folder given, the tracker also learns where the audio
        # files ended up (reported as paths in the result)
        self.output_path = os.path.abspath(output_path) if output_path else None
        self.temp_dir = os.path.abspath(temp_dir) if temp_dir else None
        self.paths = []         # final audio files, in order
        self.index = 1          # current item, 1-based
        self.total = 1          # items in the link (1 for a single track)
        self.playlist = False
        self.file_pct = 0.0     # 0..100 within the current item
        self.failed = set()     # items that hit an ERROR
        self.finished = set()   # items whose audio file was written
        self.first_error = ""
        self.errors = []        # [{"item", "message", "line"}] for the download log
        self.outdated = False   # an error suggests updating yt-dlp
        self.last_percent = -1.0
        self.last_message = None
        self.video = video
        self.streams = 1        # video: streams of the current item (2 = video + audio)
        self.stream = -1        # video: the stream downloading now, 0-based

    def _overall(self):
        done = (self.index - 1) + self.file_pct / 100.0
        return round(min(100.0, 100.0 * done / max(1, self.total)), 1)

    def _label(self, converting=False):
        if self.playlist:
            return tr("st.converting_n" if converting else "st.track", n=self.index, m=self.total)
        if self.video:
            if converting:
                return tr("st.merging") if self.streams > 1 else tr("st.converting")
            return tr("st.audio") if self.streams > 1 and self.stream >= 1 else tr("st.video")
        return tr("st.converting") if converting else tr("st.downloading")

    def _progress(self, message):
        percent = self._overall()
        if percent == self.last_percent and message == self.last_message:
            return []
        self.last_percent, self.last_message = percent, message
        return [{"type": "progress", "percent": percent, "message": message}]

    def _note_path(self, path, moved=False):
        """Remember an audio file of this run (as it will lie in the output folder)."""
        if not self.output_path:
            return
        path = os.path.abspath(path.strip().strip("\"'"))
        if os.path.splitext(path)[1].lower() not in (VIDEO_EXTENSIONS if self.video else AUDIO_EXTENSIONS):
            return
        if self.video and _PART_RE.search(path):
            return
        if not moved and self.temp_dir and path.startswith(self.temp_dir + os.sep):
            # still in the run's temp dir: it moves to the same place under the output folder
            path = os.path.join(self.output_path, os.path.relpath(path, self.temp_dir))
        if path not in self.paths:
            self.paths.append(path)

    def feed(self, line):
        line = line.strip()
        if self.output_path:
            m = _MOVED_RE.match(line)
            if m:
                self._note_path(m.group(2), moved=True)
            elif self.video:
                m = _MERGE_RE.match(line) or _VIDEO_PATH_RE.match(line)
                if m:
                    self._note_path(m.group(1))
            else:
                m = _AUDIO_PATH_RE.match(line)
                if m:
                    self._note_path(m.group(1) or m.group(2) or m.group(3))
        m = _ITEM_RE.match(line)
        if m:
            self.playlist = True
            self.index, self.total = int(m.group(1)), max(1, int(m.group(2)))
            self.file_pct = 0.0
            self.streams, self.stream = 1, -1
            return self._progress(self._label())
        if line.startswith("ERROR:"):
            self.failed.add(self.index)
            self.outdated = self.outdated or extractor_outdated(line)
            message = friendly_error(line)
            if not self.first_error:
                self.first_error = message
            if len(self.errors) < LOG_TRACKS:
                self.errors.append({"item": self.index, "message": message, "line": line})
            if self.playlist:
                return []  # yt-dlp moves on to the next item; reported in the result
            return [{"type": "error", "message": message}]
        if self.video:
            return self._feed_video(line)
        if _DONE_RE.search(line):
            self.finished.add(self.index)
        m = _PERCENT_RE.match(line)
        if m:
            pct = min(100.0, float(m.group(1))) * DOWNLOAD_SHARE
            self.file_pct = max(self.file_pct, pct)
            return self._progress(self._label())
        if "has already been downloaded" in line:
            self.file_pct = 100.0
            return self._progress(self._label())
        if _CONVERT_RE.match(line):
            self.file_pct = max(self.file_pct, 100.0 * DOWNLOAD_SHARE)
            return self._progress(self._label(converting=True))
        return []

    def _feed_video(self, line):
        moved = _MOVED_RE.match(line)
        if "has already been downloaded" in line or (
                moved and not _PART_RE.search(moved.group(2))
                and os.path.splitext(moved.group(2))[1].lower() in VIDEO_EXTENSIONS):
            self.finished.add(self.index)  # the merged file is in the output folder
        m = _FORMATS_RE.match(line)
        if m:
            self.streams = 2 if "+" in m.group(1) else 1
            self.stream = -1
            return []
        shares = VIDEO_STREAM_SHARES[self.streams]
        if _DESTINATION_RE.match(line):
            self.stream = min(self.stream + 1, self.streams - 1)
            self.file_pct = max(self.file_pct, 100.0 * sum(shares[:self.stream]))
            return self._progress(self._label())
        m = _PERCENT_RE.match(line)
        if m:
            k = max(0, self.stream)
            pct = 100.0 * (sum(shares[:k]) + shares[k] * min(100.0, float(m.group(1))) / 100.0)
            self.file_pct = max(self.file_pct, pct)
            return self._progress(self._label())
        if "has already been downloaded" in line:
            self.file_pct = 100.0
            return self._progress(self._label())
        if _VIDEO_POST_RE.match(line):
            self.file_pct = max(self.file_pct, 100.0 * DOWNLOAD_SHARE)
            return self._progress(self._label(converting=True))
        return []

    def finish(self, returncode):
        """Final events and whether the whole link was saved."""
        if returncode == 0:
            saved = self.total - len(self.failed)
        else:
            # stopped or failed: only count tracks that actually reached disk
            saved = len(self.finished - self.failed)
        saved = max(0, min(saved, self.total))
        ok = returncode == 0 and saved == self.total
        events = []
        if saved == 0 and (self.playlist or not self.failed):
            # a single track's error was already sent when it happened
            events.append({"type": "error", "message": self.first_error or tr(GENERIC_ERROR)})
        result = {"type": "result", "saved": saved, "failed": self.total - saved, "total": self.total}
        if self.output_path and self.paths:
            result["paths"] = list(self.paths)
            result["folder"] = common_folder(self.paths) or self.output_path
        events.append(result)
        return events, ok


# wax-gofi speaks English. What it says is mapped to message keys here, so the
# window shows it in its own language; the raw text stays in the log.
_GOFI_MESSAGES = {
    "connecting to deezer…": "st.connecting",
    "finding tracks on deezer…": "st.finding",
    "invalid download request": "go.bad_request",
    "unsupported deezer quality": "go.bad_quality",
    "use a spotify track, album, or playlist link": "go.use_spotify",
    "could not connect to deezer. check your arl and connection.": "go.connect",
    "no matching tracks found on deezer": "go.no_matches",
}
# (pattern, key, names of the groups): numbers and details are carried over
_GOFI_PATTERNS = [
    (re.compile(r"^downloading (\d+) of (\d+)$", re.I), "st.track", ("n", "m")),
    (re.compile(r"^finding track (\d+) of (\d+) on deezer…$", re.I), "st.finding_n", ("n", "m")),
    (re.compile(r"^could not find this spotify link on deezer: (.*)$", re.I | re.S), "go.link_not_found", ("detail",)),
    (re.compile(r"^could not check spotify album: (.*)$", re.I | re.S), "go.album", ("detail",)),
]
_GOFI_REASONS = {
    "already there": "why.already_there",
    "not found on deezer": "why.not_found",
    "spotify gave no details for this track": "why.no_details",
    "deezer has no file for this track": "why.no_file",
    "not available in your country": "err.country",
    "deezer session expired, reconnect deezer": "why.session",
    "connection lost while downloading": "why.network",
    "deezer refused the download": "why.refused",
    "the disk is full": "err.disk",
    "can't write to that folder": "err.no_write",
    "couldn't write the tags": "why.tagging",
    "cancelled": "why.cancelled",
    "download failed": "why.generic",
}
_GOFI_LICENSE_RE = re.compile(r"^not available in (.+) for this account$", re.I)
_GOFI_TRACK_N_RE = re.compile(r"^track (\d+)$", re.I)
# the error that means the saved Deezer session is no good
DEEZER_CONNECT_CODE = "go.connect"


def gofi_text(message):
    """(text in the window's language, key or None) for something wax-gofi said."""
    text = message.strip()
    key = _GOFI_MESSAGES.get(text.lower())
    if key:
        return tr(key), key
    for pattern, key, names in _GOFI_PATTERNS:
        m = pattern.match(text)
        if m:
            return tr(key, **dict(zip(names, m.groups()))), key
    return message.lower(), None


def gofi_reason(reason):
    """A per-track reason of wax-gofi in the window's language (unknown ones stay as said)."""
    text = reason.strip()
    key = _GOFI_REASONS.get(text.lower())
    if key:
        return tr(key)
    m = _GOFI_LICENSE_RE.match(text)
    return tr("why.license", label=m.group(1)) if m else reason


def gofi_event(raw):
    """Normalize a wax-gofi event to the UI's copy: lowercase, in the window's language."""
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    message = event.get("message")
    if isinstance(message, str):
        event["message"], key = gofi_text(message)
        if event.get("type") == "error" and key == DEEZER_CONNECT_CODE:
            event["code"] = key
    return event


# ---------------------------------------------------------------------------
# Per-download log, for the "details" screen after a failed or partial
# download: what failed and why first, then the tail of the raw output.
# Memory only, the last LOG_KEEP downloads, never written to disk. Every line
# is scrubbed of the Deezer ARL and anything that looks like a credential.
# ---------------------------------------------------------------------------
LOG_LINES = 400          # raw output lines kept per download (the tail)
LOG_LINE_CHARS = 1000    # a single line longer than this is cut
LOG_TRACKS = 500         # per-track failures / skips kept per download
LOG_KEEP = 10            # downloads whose log is kept

HIDDEN = "[hidden]"
_SECRET_RES = [
    # arl=…, "arl": "…", arl: …
    re.compile(r"(?i)(\barl[\"']?\s*[=:]\s*[\"']?)[^\s\"',;&]+"),
    re.compile(r"(?i)(\b(?:authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+"),
    re.compile(r"(?i)(\bbearer\s+)[\w.~+/=-]+"),
    re.compile(r"()\b[0-9a-fA-F]{120,}\b"),  # an ARL is 192 hex chars
]


def scrub(text, secrets=()):
    """The text with known secrets and credential-looking values hidden."""
    text = str(text)
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, HIDDEN)
    for pattern in _SECRET_RES:
        text = pattern.sub(lambda m: m.group(1) + HIDDEN, text)
    return text


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")  # terminal colours from GoFi's logger


def _clip(text, limit=LOG_LINE_CHARS):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


class DownloadLog:
    def __init__(self, stream_id, url, fmt, source):
        self._lock = threading.Lock()
        self.id = stream_id
        self.url = url
        self.format = fmt
        self.source = source                # "yt-dlp" | "deezer"
        self.state = "queued"
        self.message = ""                   # the error the window showed
        self.result = None                  # the result event
        self.failures = []                  # [{"artist", "title", "reason", "detail"?, "count"?}]
        self.skipped = []                   # [{"artist", "title", "reason"}]
        self.errors = []                    # [{"message", "line"}] classified error lines
        self.lines = collections.deque(maxlen=LOG_LINES)
        self.dropped = 0                    # lines that fell off the front

    def _secrets(self):
        return (deezer_arl,)

    def _clean(self, text):
        return _clip(scrub(text, self._secrets()))

    def line(self, text):
        text = self._clean(_ANSI_RE.sub("", text).rstrip())
        with self._lock:
            if len(self.lines) == self.lines.maxlen:
                self.dropped += 1
            self.lines.append(text)

    def error(self, message, line=""):
        with self._lock:
            if len(self.errors) < LOG_TRACKS:
                self.errors.append({"message": self._clean(message), "line": self._clean(line)})

    def track(self, event):
        """A wax-gofi track event: one track failed or was skipped."""
        entry = {key: self._clean(event[key]) for key in ("artist", "title", "reason", "detail")
                 if isinstance(event.get(key), str) and event.get(key)}
        if isinstance(event.get("count"), int) and event["count"] > 0:
            entry["count"] = event["count"]
        entry.setdefault("reason", "")
        entry["reason"] = gofi_reason(entry["reason"])
        m = _GOFI_TRACK_N_RE.match(entry.get("title", ""))
        if m:
            entry["title"] = tr("log.track_n", n=m.group(1))
        target = self.skipped if event.get("status") == "skipped" else self.failures
        with self._lock:
            if len(target) < LOG_TRACKS:
                target.append(entry)

    def event(self, event):
        """An event that went to the window."""
        with self._lock:
            if event.get("type") == "error":
                self.message = self._clean(event.get("message", ""))
            elif event.get("type") == "result":
                self.result = {k: event[k] for k in ("saved", "failed", "total", "skipped")
                               if isinstance(event.get(k), int)}

    def tracker_failures(self, tracker):
        """yt-dlp: on a playlist each failed item is one failure, by number."""
        if not tracker.playlist:
            return
        seen = set()
        with self._lock:
            for err in tracker.errors:
                if err["item"] in seen or len(self.failures) >= LOG_TRACKS:
                    continue
                seen.add(err["item"])
                self.failures.append({"title": tr("log.track_n", n=err["item"]),
                                      "reason": err["message"], "detail": self._clean(err["line"])})

    def snapshot(self):
        with self._lock:
            return {
                "id": self.id, "url": self._clean(self.url), "format": self.format,
                "source": self.source, "state": self.state, "message": self.message,
                "result": dict(self.result) if self.result else None,
                "failures": [dict(f) for f in self.failures],
                "skipped": [dict(f) for f in self.skipped],
                "errors": [dict(e) for e in self.errors],
                "lines": list(self.lines), "dropped": self.dropped,
            }


download_logs = collections.OrderedDict()
_logs_lock = threading.Lock()


def remember_log(log):
    with _logs_lock:
        download_logs[log.id] = log
        while len(download_logs) > LOG_KEEP:
            download_logs.popitem(last=False)
    return log


def find_log(stream_id):
    with _logs_lock:
        return download_logs.get(stream_id)


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"output_path": DEFAULT_FOLDER}


def save_config(data):
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f)


def saved_language():
    """The language the user picked, or None while they have not."""
    return normalize_language(load_config().get("language"))


def init_language():
    """At launch: the saved choice, else the system's language (Russian or English)."""
    set_language(saved_language() or system_language())


# The yt-dlp a download runs is resolved per download: a user copy in the app
# data dir that keeps itself current, or the bundled one if that is newer.
ytdlp_manager = ytdlp_update.YtdlpManager(
    base=find_ytdlp,
    bundled=lambda: bundled_binary("yt-dlp"),
    load_config=load_config,
    save_config=save_config,
)
UPDATED_MESSAGE = "err.updated"  # a key


def pick_folder():
    """Open a native folder picker for the current OS. Returns a path or ""."""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(
                ["osascript",
                 "-e", 'tell application "Finder" to activate',
                 "-e", 'POSIX path of (choose folder with prompt "Select output folder:")'],
                capture_output=True, text=True,
            )
            return r.stdout.strip()
        if IS_WINDOWS:
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
                "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", ps],
                capture_output=True, text=True,
            )
            return r.stdout.strip()
        # Linux / other: try zenity
        r = subprocess.run(
            ["zenity", "--file-selection", "--directory"],
            capture_output=True, text=True,
        )
        return r.stdout.strip()
    except Exception:
        return ""


# The server only ever talks to its own window. A page in the user's browser
# can still reach 127.0.0.1 (directly, or via DNS rebinding under its own
# hostname), so requests with a foreign Host or Origin are refused outright.
ALLOWED_HOSTS = {HOST, f"{HOST}:{PORT}", "localhost", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {URL, f"http://localhost:{PORT}"}


@app.before_request
def only_local_window():
    if request.host not in ALLOWED_HOSTS:
        return jsonify({"error": "host not allowed"}), 403
    origin = request.headers.get("Origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        return jsonify({"error": "origin not allowed"}), 403
    return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/logo.png")
def logo():
    return send_from_directory(os.path.join(RES, "templates"), "logo.png")


@app.route("/dm-mono.woff2")
def font():
    return send_from_directory(os.path.join(RES, "templates"), "dm-mono.woff2")


@app.route("/jetbrains-mono-cyrillic.woff2")
def font_cyrillic():
    return send_from_directory(os.path.join(RES, "templates"), "jetbrains-mono-cyrillic.woff2")


@app.route("/status")
def status():
    cfg = load_config()
    return jsonify({"status": "running", "output_path": cfg.get("output_path", DEFAULT_FOLDER),
                    "ytdlp_version": ytdlp_manager.known_version(),
                    "version": APP_VERSION})


@app.route("/language", methods=["GET", "POST"])
def language():
    """GET: the language the window should use. POST {language, save?}: switch it.

    A choice is saved in the settings (save omitted or true); the page posts
    save=false for the language it guessed from the system, so only a switch
    the user made is remembered.
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        code = normalize_language(data.get("language"))
        if code is None:
            return jsonify({"error": "unknown language"}), 400
        set_language(code)
        if data.get("save", True):
            cfg = load_config()
            cfg["language"] = code
            save_config(cfg)
    return jsonify({"language": get_language(), "saved": saved_language() is not None})


@app.route("/get-folder")
def get_folder():
    cfg = load_config()
    return jsonify({"path": cfg.get("output_path", DEFAULT_FOLDER)})


@app.route("/choose-folder", methods=["POST"])
def choose_folder():
    path = pick_folder()
    if path:
        cfg = load_config()
        cfg["output_path"] = path
        save_config(cfg)
        return jsonify({"path": path})
    return jsonify({"path": load_config().get("output_path", DEFAULT_FOLDER)})


@app.route("/deezer-session", methods=["GET", "POST"])
def deezer_session():
    global deezer_arl
    if request.method == "POST":
        value = (request.get_json(silent=True) or {}).get("arl", "")
        if not isinstance(value, str) or len(value.strip()) != 192:
            return jsonify({"error": tr("api.arl_length")}), 400
        deezer_arl = value.strip()
    return jsonify({"available": bool(deezer_arl)})


# ---------------------------------------------------------------------------
# Child processes. yt-dlp spawns ffmpeg (and the standalone yt-dlp.exe is a
# PyInstaller bootloader with a child of its own), so stopping a download means
# stopping a whole process tree, not one pid.
# ---------------------------------------------------------------------------
CREATE_NO_WINDOW = 0x08000000


def child_popen_kwargs():
    if IS_WINDOWS:
        return {"creationflags": CREATE_NO_WINDOW}  # no console window
    # its own session = its own process group, killed as one
    return {"start_new_session": True}


def kill_tree(proc, grace=3.0):
    """Stop a child and everything it started. Blocks until it is gone (or gave up)."""
    if proc is None or proc.poll() is not None:
        return
    if IS_WINDOWS:
        # terminate() only ends the top process; ffmpeg would keep running
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
        try:
            proc.wait(grace)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return
    try:
        group = os.getpgid(proc.pid) == proc.pid
    except OSError:
        group = False
    try:
        if group:
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        pass
    # whatever is left of the group (ffmpeg outliving yt-dlp, or a stuck child)
    try:
        if group:
            os.killpg(proc.pid, signal.SIGKILL)
        elif proc.poll() is None:
            proc.kill()
    except OSError:
        pass
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        pass


# ---------------------------------------------------------------------------
# Downloads run one at a time, in the order they were added, on one worker
# thread in the server. The queue lives here and not in the page, so it keeps
# going while the window is hidden in the menu bar. Each link keeps its own
# stream id: its events wait in its own queue until the window reads them.
# A stream ends with __DONE__:done | error | cancelled.
# ---------------------------------------------------------------------------
class Download:
    def __init__(self, stream_id, url, fmt, output_path, is_spotify):
        self.id = stream_id
        self.url = url
        self.fmt = fmt
        self.output_path = output_path
        self.is_spotify = is_spotify
        self.events = queue.Queue()
        self.state = "queued"           # queued | running | done | error | cancelled
        self.cancelled = False
        self.proc = None
        self.temp_dir = os.path.join(TEMP_DIR, f"run-{stream_id}")
        self.entry = {"id": stream_id, "url": url, "format": fmt, "status": "queued", "filename": ""}
        self.log = remember_log(DownloadLog(stream_id, url, fmt, "deezer" if is_spotify else "yt-dlp"))
        self._counted = True            # begin_download() was called for it

    def emit(self, event):
        self.events.put("__WAX__:" + json.dumps(event))

    def release(self):
        """Stop counting it as a download in progress (once)."""
        if self._counted:
            self._counted = False
            ytdlp_manager.end_download()

    def close(self, status):
        self.state = status
        self.log.state = status
        self.entry["status"] = status
        self.release()
        self.events.put(f"__DONE__:{status}")
        # if the window never subscribed (or dropped off), do not keep it forever
        reaper = threading.Timer(STREAM_TTL, active_streams.pop, (self.id, None))
        reaper.daemon = True  # must not hold the app open on quit
        reaper.start()

    def remove_partials(self):
        # finished files are already moved into the output folder; only the
        # half-written ones of this run live here
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class DownloadQueue:
    def __init__(self, runner):
        self._runner = runner
        self._cond = threading.Condition()
        self._pending = collections.deque()
        self._items = {}
        self._current = None
        self._worker = None

    def submit(self, item):
        with self._cond:
            self._items[item.id] = item
            self._pending.append(item)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, daemon=True, name="wax-downloads")
                self._worker.start()
            self._cond.notify()
            return len(self._pending) - 1 + (self._current is not None)

    def _work(self):
        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait()
                item = self._pending.popleft()
                item.state = "running"
                self._current = item
            try:
                self._runner(item)
            except Exception:
                pass  # the runner reports its own errors; the queue must go on
            finally:
                with self._cond:
                    self._current = None
                    self._items.pop(item.id, None)

    def started(self, item, proc):
        """The runner spawned item's process. False if it was cancelled meanwhile."""
        with self._cond:
            item.proc = proc
            cancelled = item.cancelled
        if cancelled:
            kill_tree(proc)
        return not cancelled

    def cancel(self, stream_id):
        """Cancel a queued or running download. False if there is none by that id."""
        with self._cond:
            item = self._items.get(stream_id)
            if item is None or item.cancelled or item.state not in ("queued", "running"):
                return False
            item.cancelled = True
            waiting = item.state == "queued"
            if waiting:
                self._pending.remove(item)
                self._items.pop(item.id, None)
            proc = item.proc
        if waiting:
            item.close("cancelled")
        else:
            kill_tree(proc)        # the runner then ends the stream as cancelled
            item.remove_partials()  # also here: on quit the runner may not get to it
        return True

    def cancel_all(self):
        with self._cond:
            ids = [i.id for i in self._pending] + ([self._current.id] if self._current else [])
        for stream_id in reversed(ids):  # queued first, so nothing new starts
            self.cancel(stream_id)

    def pending(self):
        """Downloads running or waiting to run."""
        with self._cond:
            cur = self._current
            return len(self._pending) + (cur is not None and cur.state == "running" and not cur.cancelled)

    def order(self):
        with self._cond:
            return ([self._current.id] if self._current else []) + [i.id for i in self._pending]


@app.route("/download", methods=["POST"])
def download():
    data = request.json
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp3_320")
    output_path = data.get("output_path") or load_config().get("output_path", DEFAULT_FOLDER)
    output_path = os.path.expanduser(output_path)
    if url:
        used_output_dirs.add(os.path.abspath(output_path))

    if not url:
        return jsonify({"error": tr("api.url_required")}), 400

    if not isinstance(fmt, str) or fmt not in FORMATS:
        return jsonify({"error": tr("api.unknown_format")}), 400
    is_spotify = spotify_link(url)
    if is_spotify:
        if fmt not in SPOTIFY_FORMATS:
            return jsonify({"error": tr("api.spotify_formats")}), 400
        if not find_gofi():
            return jsonify({"error": tr("api.no_gofi")}), 503
        if not deezer_arl:
            return jsonify({"error": tr("api.connect_first")}), 400
    elif fmt in VIDEO_FORMATS and not youtube_video_link(url):
        return jsonify({"error": tr("api.youtube_only")}), 400

    # counted from the moment it is queued: an update must not swap yt-dlp
    # between now and when the worker picks the binary for it
    ytdlp_manager.begin_download()
    stream_id = next(_stream_ids)
    item = Download(stream_id, url, fmt, output_path, is_spotify)
    active_streams[stream_id] = item.events
    history.insert(0, item.entry)
    if len(history) > 10:
        history.pop()
    ahead = downloads.submit(item)
    return jsonify({"stream_id": stream_id, "ahead": ahead})


@app.route("/cancel/<int:stream_id>", methods=["POST"])
def cancel(stream_id):
    return jsonify({"cancelled": downloads.cancel(stream_id)})


def run_download(item):
    """Run one queued download to the end, reporting into its stream."""
    q = item.events
    entry = item.entry
    log = item.log
    log.state = "running"
    proc = None
    workdir = None
    status = "error"
    entry["status"] = "downloading"

    def send(event):
        log.event(event)
        q.put("__WAX__:" + json.dumps(event))

    try:
        os.makedirs(item.temp_dir, exist_ok=True)
        gofi = find_gofi() if item.is_spotify else None
        if item.is_spotify:
            if not gofi:
                raise RuntimeError("deezer downloader is not installed in this build")
            workdir = tempfile.TemporaryDirectory(prefix="wax-gofi-")
            child_cmd = [gofi]
        else:
            child_cmd = build_ytdlp_command(item.url, item.fmt, item.output_path,
                                            ffmpeg_location=FFMPEG_LOCATION, temp_dir=item.temp_dir)
        if item.cancelled:
            raise _Cancelled()
        proc = subprocess.Popen(
            child_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if item.is_spotify else None,
            cwd=workdir.name if workdir else None,
            text=True,
            errors="replace",  # a stray non-UTF-8 byte must not kill the reader
            bufsize=1,
            **child_popen_kwargs(),
        )
        active_procs.add(proc)
        if not downloads.started(item, proc):
            raise _Cancelled()
        if item.is_spotify:
            payload = {"url": item.url if "://" in item.url else "https://" + item.url,
                       "quality": QUALITY_TAGS[item.fmt],
                       "output": os.path.abspath(item.output_path), "arl": deezer_arl}
            json.dump(payload, proc.stdin)
            proc.stdin.close()
        tracker = None if item.is_spotify else YtdlpProgress(
            item.output_path, item.temp_dir, video=item.fmt in VIDEO_FORMATS)
        for line in proc.stdout:
            line = line.rstrip()
            log.line(line)
            if item.is_spotify:
                if line.startswith("WAX_EVENT:"):
                    event = gofi_event(line[len("WAX_EVENT:"):])
                    if event is not None:
                        if event.get("type") == "track":
                            log.track(event)  # for the details screen only
                            continue
                        if event.get("type") == "result":
                            event.setdefault("folder", os.path.abspath(item.output_path))
                        send(event)
                continue
            if line.startswith("ERROR:"):
                log.error(friendly_error(line), line)
            for event in tracker.feed(line):
                send(event)
            merged = _MERGE_RE.match(line)
            if merged:
                entry["filename"] = os.path.basename(merged.group(1))
            elif "Destination:" in line:
                entry["filename"] = os.path.basename(line.split("Destination:", 1)[1].strip())
            elif "has already been downloaded" in line:
                fname = line.split("]", 1)[-1].strip().split(" has already")[0].strip()
                if fname:
                    entry["filename"] = os.path.basename(fname)
        proc.wait()
        active_procs.discard(proc)
        item.release()
        if item.cancelled:
            raise _Cancelled()
        ok = proc.returncode == 0
        if tracker is not None:
            log.tracker_failures(tracker)
            events, ok = tracker.finish(proc.returncode)
            if not ok and tracker.outdated and update_after_failure(q):
                events.insert(-1, {"type": "error", "message": tr(UPDATED_MESSAGE)})
            for event in events:
                if event.get("type") == "result" and "paths" in event:
                    event["paths"] = [p for p in event["paths"] if os.path.isfile(p)]
                    if not event["paths"]:  # nothing of it on disk: the page falls back to the output folder
                        event.pop("paths")
                        event.pop("folder", None)
                send(event)
        status = "done" if ok else "error"
    except _Cancelled:
        status = "cancelled"
    except Exception as e:
        status = "cancelled" if item.cancelled else "error"
        if status == "error":
            log.line(f"wax: {e}")
            log.error(friendly_error(str(e)), str(e))
            send({"type": "error", "message": friendly_error(str(e))})
    finally:
        if proc is not None and proc.poll() is None:
            kill_tree(proc)
        active_procs.discard(proc)
        if workdir:
            workdir.cleanup()
        item.remove_partials()
        item.close(status)


class _Cancelled(Exception):
    pass


downloads = DownloadQueue(run_download)


def update_after_failure(q):
    """A download failed like a stale extractor: update yt-dlp right away.

    The download is already over; this only delays its final error while the
    status line says what is going on. True if a newer yt-dlp is now in use.
    """
    if not ytdlp_manager.force_due():
        return False  # checked a moment ago, or another download is running
    q.put("__WAX__:" + json.dumps({"type": "status", "message": tr("st.updating")}))
    return ytdlp_manager.check(force=True) == "updated"


@app.route("/progress/<int:stream_id>")
def progress(stream_id):
    q = active_streams.get(stream_id)
    if q is None:
        return Response("data: Stream not found\n\n", mimetype="text/event-stream")

    def generate():
        # GeneratorExit (client gone) must clean up too, or entries pile up.
        try:
            while True:
                try:
                    line = q.get(timeout=30)
                except queue.Empty:
                    yield "data: \n\n"
                    continue
                yield f"data: {line}\n\n"
                if line is not None and line.startswith("__DONE__:"):
                    break
        finally:
            active_streams.pop(stream_id, None)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/log/<int:stream_id>")
def get_log(stream_id):
    """The log of one of the last downloads (memory only, scrubbed)."""
    log = find_log(stream_id)
    if log is None:
        return jsonify({"error": "no log for this download"}), 404
    response = jsonify(log.snapshot())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/history")
def get_history():
    return jsonify(history)


def _port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((HOST, PORT)) == 0


# ---------------------------------------------------------------------------
# "show in folder": reveal a saved file (or folder) in Finder / Explorer.
# Only places inside an output folder this app was told to write to qualify,
# so the bridge cannot be used to open arbitrary paths.
# ---------------------------------------------------------------------------
used_output_dirs = set()   # every output folder a download was started with


def reveal_roots():
    roots = set(used_output_dirs)
    roots.add(os.path.abspath(os.path.expanduser(load_config().get("output_path", DEFAULT_FOLDER))))
    return roots


def reveal_allowed(path, roots):
    """Is path (a file or folder that exists) inside one of the roots?"""
    if not isinstance(path, str) or not path or "\0" in path:
        return False
    try:
        real = os.path.realpath(path)
        if not os.path.exists(real):
            return False
        for root in roots:
            root = os.path.realpath(root)
            if os.path.commonpath([real, root]) == root:
                return True
    except (ValueError, OSError):
        pass
    return False


def reveal_command(path, is_dir, platform):
    """argv that shows path in the system file manager (never run through a shell)."""
    if platform == "darwin":
        return ["/usr/bin/open", path] if is_dir else ["/usr/bin/open", "-R", path]
    if platform == "win32":
        path = os.path.normpath(path)
        return ["explorer", path] if is_dir else ["explorer", "/select,", path]
    return ["xdg-open", path if is_dir else os.path.dirname(path)]


def reveal_path(path, roots=None, platform=None, popen=subprocess.Popen):
    """Reveal path if allowed. True if a file manager was started."""
    roots = reveal_roots() if roots is None else roots
    if not reveal_allowed(path, roots):
        return False
    real = os.path.realpath(path)
    cmd = reveal_command(real, os.path.isdir(real), platform or sys.platform)
    try:
        popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              **child_popen_kwargs())
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Clipboard: a copied link is offered in the empty url field. Read natively
# because navigator.clipboard is unreliable in the system webviews.
# ---------------------------------------------------------------------------
def parse_clipboard_url(text):
    """The text as a link if it is exactly one http(s) link, else ''."""
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if not text or len(text) > 2048 or re.search(r"\s", text):
        return ""
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}", host, re.I):
        return ""
    return text


def read_clipboard_text():
    if sys.platform == "darwin":
        cmds = [["/usr/bin/pbpaste"]]
    elif IS_WINDOWS:
        cmds = [["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"]]
    else:
        cmds = [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"]]
    for cmd in cmds:
        if not shutil.which(cmd[0]) and not os.path.isabs(cmd[0]):
            continue
        try:
            kwargs = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}
            r = subprocess.run(cmd, capture_output=True, timeout=3, stdin=subprocess.DEVNULL, **kwargs)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return r.stdout[:4096].decode("utf-8", errors="replace")
    return ""


CLIPBOARD_MAX = 1 << 20  # 1 MiB of text is plenty for a log


def clipboard_write_commands(platform=None):
    platform = platform or sys.platform
    if platform == "darwin":
        return [["/usr/bin/pbcopy"]]
    if platform == "win32":
        # the text comes on stdin, never on the command line
        return [["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
                 "Set-Clipboard -Value ([Console]::In.ReadToEnd())"]]
    return [["wl-copy"], ["xclip", "-selection", "clipboard", "-in"]]


def write_clipboard_text(text, platform=None, run=subprocess.run):
    """Put text on the system clipboard. True if a clipboard tool took it."""
    if not isinstance(text, str):
        return False
    data = text[:CLIPBOARD_MAX].encode("utf-8")
    for cmd in clipboard_write_commands(platform):
        if not shutil.which(cmd[0]) and not os.path.isabs(cmd[0]):
            continue
        try:
            kwargs = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}
            # no pipes back: wl-copy / xclip stay behind to serve the clipboard
            r = run(cmd, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5, **kwargs)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return True
    return False


# ---------------------------------------------------------------------------
# Windows has no transparent window here, so the modal's 64px corners would
# show as black squares. The native window is clipped to a rounded rectangle
# instead (SetWindowRgn works in physical pixels, hence the DPI math).
# ---------------------------------------------------------------------------
WINDOW_RADIUS = 64


def round_region_params(width_px, height_px, dpi=96):
    """(right, bottom, ellipse) for CreateRoundRectRgn(0, 0, right, bottom, ellipse, ellipse).

    width/height are the window's physical size; the region API wants the
    bottom-right corner one pixel past the edge, and the corner *diameter*.
    """
    scale = (dpi or 96) / 96.0
    return int(width_px) + 1, int(height_px) + 1, max(2, round(2 * WINDOW_RADIUS * scale))


def _window_handle(window):
    """HWND of the pywebview window, or 0."""
    import ctypes

    try:
        return int(window.native.Handle.ToInt64())  # WinForms form
    except Exception:
        pass
    user32 = ctypes.windll.user32
    found = []
    pid = os.getpid()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _):
        owner = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        buf = ctypes.create_unicode_buffer(8)
        user32.GetWindowTextW(hwnd, buf, 8)
        if owner.value == pid and buf.value == "WAX" and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else 0


def apply_round_corners(window):
    """Clip the window to a rounded rect. Windows only; never raises."""
    if sys.platform != "win32" or window is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = _window_handle(window)
        if not hwnd:
            return False
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
        gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
        rect = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return False
        try:
            dpi = user32.GetDpiForWindow(wintypes.HWND(hwnd))
        except Exception:
            dpi = 96
        right, bottom, d = round_region_params(rect.right - rect.left, rect.bottom - rect.top, dpi)
        region = gdi32.CreateRoundRectRgn(0, 0, right, bottom, d, d)
        user32.SetWindowRgn(wintypes.HWND(hwnd), region, True)  # the window owns the region now
        return True
    except Exception:
        return False


def schedule_round_corners(window):
    """After the window changed size: the native resize settles a moment later."""
    if sys.platform != "win32":
        return
    for delay in (0.15, 0.6):
        t = threading.Timer(delay, apply_round_corners, (window,))
        t.daemon = True
        t.start()


QUIT_TITLE = "quit.title"      # keys; asked in the window's language
QUIT_MESSAGE = "quit.message"


def quit_needs_confirmation(pending, confirmed=False, can_ask=True):
    """Ask before quitting? Only while something is running or queued, once."""
    return pending > 0 and not confirmed and can_ask


class Api:
    """Bridge exposed to the UI as window.pywebview.api."""

    def __init__(self):
        self.window = None
        self.menu_bar_ready = False
        self._quit_confirmed = False
        self._asking = threading.Lock()

    def resize(self, height):
        # The window is a fixed-width 600px modal; only its height follows the screen.
        if self.window:
            self.window.resize(WINDOW_WIDTH, int(height))
            schedule_round_corners(self.window)

    def reveal(self, path):
        """Show a saved file (or folder) in Finder / Explorer. True if it was opened."""
        return reveal_path(path)

    def clipboard_url(self):
        """The link on the clipboard if it holds exactly one, else ''."""
        return parse_clipboard_url(read_clipboard_text())

    def copy_text(self, text):
        """Copy text (the download log) to the clipboard. True if it worked."""
        return write_clipboard_text(text)

    def open_url(self, url):
        if url.startswith(("http://", "https://")):
            _open_external(url)

    def platform(self):
        """'darwin' | 'win32' | 'linux' — the UI draws its window controls to match."""
        return sys.platform

    def minimize(self):
        if self.window:
            self.window.minimize()

    def open_window(self):
        if self.window:
            self.window.restore()
            self.window.show()

    def close_window(self):
        if self.window and self.menu_bar_ready:
            self.window.hide()  # downloads keep running in the menu bar
        else:
            self.quit()

    def stop_downloads(self):
        downloads.cancel_all()
        for p in list(active_procs):
            kill_tree(p)

    def _needs_confirmation(self):
        return quit_needs_confirmation(downloads.pending(), self._quit_confirmed, self.window is not None)

    def quit(self):
        if self._needs_confirmation():
            self._confirm_then_quit()
        else:
            self._quit_now()

    def _quit_now(self):
        self._quit_confirmed = True
        self.stop_downloads()
        if self.window:
            # destroy() straight from a js_api call hangs the app: pywebview's
            # bridge thread then waits forever for a return value the stopped
            # event loop can no longer deliver, and it is not a daemon thread.
            threading.Timer(0.1, self.window.destroy).start()

    def _confirm_then_quit(self):
        """Ask on a thread of its own and return at once.

        The dialog is never opened from the GUI thread (the macOS menu, the
        closing event) nor awaited by a js_api call: pywebview's Cocoa and GTK
        dialogs post to the GUI loop and block until it answers, which on that
        very loop would never happen.
        """
        if not self._asking.acquire(blocking=False):
            return  # already asking

        def ask():
            try:
                ok = self.window.create_confirmation_dialog(tr(QUIT_TITLE), tr(QUIT_MESSAGE))
            except Exception:
                ok = False
            finally:
                self._asking.release()
            if ok:
                self._quit_now()

        threading.Thread(target=ask, daemon=True).start()

    def on_closing(self):
        """The window is about to close (Cmd+Q, Alt+F4, taskbar, destroy())."""
        if self._needs_confirmation():
            self._confirm_then_quit()
            return False  # cancels this close; a confirmed quit closes again
        self._quit_confirmed = True
        self.stop_downloads()
        return None


def _open_external(url):
    """Open a link in the user's real browser, never inside the modal."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["/usr/bin/open", url])
        elif IS_WINDOWS:
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", url])
    except Exception:
        pass


def _wait_for_server():
    for _ in range(50):  # up to ~10s
        if _port_in_use():
            return True
        time.sleep(0.2)
    return False


def _icon_path():
    """The app icon file, in the bundle or in the source checkout."""
    for p in (
        os.path.join(RES, "app_icon.png"),
        os.path.join(os.path.dirname(RES), "packaging", "app_icon.png"),
        os.path.join(os.path.dirname(RES), "packaging", "icon_source.png"),
    ):
        if os.path.isfile(p):
            return p
    return None


def _set_dock_icon():
    """Without this macOS shows a generic Python rocket while the app runs."""
    path = _icon_path()
    if sys.platform != "darwin" or not path:
        return
    try:
        from AppKit import NSApplication, NSImage

        image = NSImage.alloc().initWithContentsOfFile_(path)
        if image:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        pass


# Cocoa objects must be retained for the lifetime of the application.
_status_item = None
_status_delegate = None


def _install_menu_bar(api):
    """Install a native macOS status menu while the main thread owns Cocoa."""
    global _status_item, _status_delegate
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSImage, NSMenu, NSMenuItem, NSStatusBar, NSVariableStatusItemLength
        from Foundation import NSObject

        class WaxStatusDelegate(NSObject):
            def openWindow_(self, sender):
                api.open_window()

            def hideWindow_(self, sender):
                api.close_window()

            def quitApp_(self, sender):
                api.quit()  # asks first while downloads run; never blocks this thread

        icon_path = os.path.join(RES, "templates", "menu-bar-w.png")
        icon = NSImage.alloc().initWithContentsOfFile_(icon_path)
        if icon is not None:
            icon.setSize_((18, 18))
        else:
            # Keep the menu available even if a build is missing the asset.
            icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_("opticaldisc", "Wax")
        if icon is None:
            return False
        icon.setTemplate_(True)
        delegate = WaxStatusDelegate.alloc().init()
        menu = NSMenu.alloc().init()
        for title, selector in ((tr("menu.open"), "openWindow:"), (tr("menu.hide"), "hideWindow:")):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, "")
            item.setTarget_(delegate)
            menu.addItem_(item)
        menu.addItem_(NSMenuItem.separatorItem())
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(tr("menu.quit"), "quitApp:", "")
        item.setTarget_(delegate)
        menu.addItem_(item)
        status = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        status.button().setImage_(icon)
        status.button().setToolTip_("Wax")
        status.setMenu_(menu)
        _status_item, _status_delegate = status, delegate
        return True
    except Exception:
        # Without an icon, closing must still exit rather than hide the only UI.
        return False


def main():
    # A second launch does not start another server — it just shows a window on
    # the one that is already running.
    if not _port_in_use():
        threading.Thread(
            target=lambda: app.run(host=HOST, port=PORT, debug=False), daemon=True
        ).start()
        _wait_for_server()
        # leftovers of a run that was killed with the app last time
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
        ytdlp_manager.start()  # only the instance that runs the downloads

    # easy_drag would move the window on *any* mouse drag, so text could never be
    # selected in the url/folder fields. Instead only the modal backdrop itself
    # (.pywebview-drag-region, direct target only) drags the window.
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    _set_dock_icon()

    init_language()
    api = Api()
    api.window = webview.create_window(
        "WAX",
        URL,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        frameless=True,
        easy_drag=False,
        resizable=False,
        transparent=sys.platform == "darwin",  # Windows falls back to square black
        background_color="#000000",
        js_api=api,
    )
    api.window.events.closing += api.on_closing
    if sys.platform == "win32":
        api.window.events.shown += lambda: schedule_round_corners(api.window)
    api.menu_bar_ready = _install_menu_bar(api)
    webview.start()


if __name__ == "__main__":
    main()
