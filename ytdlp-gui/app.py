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

HOST = "127.0.0.1"
PORT = 7337
URL = f"http://{HOST}:{PORT}"
IS_WINDOWS = os.name == "nt"

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


def spotify_link(url):
    try:
        parsed = urlsplit(url if "://" in url else "https://" + url)
        return parsed.scheme in ("http", "https") and parsed.hostname in ("open.spotify.com", "www.open.spotify.com")
    except ValueError:
        return False


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
# File layout. yt-dlp has no "if this is a playlist" switch in -o, so the name
# parts are computed into wax_* fields with --parse-metadata (each rule only
# sets its fields when its regex matches; later rules override earlier ones)
# and -o just glues them together:
#   single track  -> <out>/Artist - Title.ext
#   album         -> <out>/Artist - Album/01 Title.ext
#   playlist      -> <out>/Playlist/01 Artist - Title.ext
# The folder only uses playlist-level fields, so every track of one link lands
# in the same folder. yt-dlp sanitizes each field (":" "?" "/" … become
# full-width look-alikes, valid on macOS and Windows) and on Windows also fixes
# trailing dots/spaces and reserved names itself, so --windows-filenames (which
# would mangle "Vol. 2..." on macOS too) is not passed.
# ---------------------------------------------------------------------------
OUTPUT_TEMPLATE = "%(wax_dir|.)s/%(wax_num&{} |)s%(wax_file)s.%(ext)s"

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
    "%(playlist_index)02d:(?P<wax_num>\\d+)",
    # an album (its tracks' album == the playlist title): "Artist - Album"
    # folder, the album's own track numbers, and no artist in the file name
    "%(playlist_index|)s\n%(playlist_title|)s\n%(album_artist,playlist_uploader,artist|)s - %(album|)s"
    ":(?s)^\\d+\n(?P<wax_p>[^\n]+)\n(?P<wax_dir>[^\n]*\\w[^\n]* - (?P=wax_p))$",
    "%(playlist_index|)s\n%(playlist_title|)s\n%(album|)s\n%(track_number)02d\n%(track,title)s"
    ":(?s)^\\d+\n(?P<wax_p>[^\n]+)\n(?P=wax_p)\n(?P<wax_num>\\d+)\n(?P<wax_file>.+)$",
]

# Tags on top of what --embed-metadata maps by itself (title, artist, album,
# album_artist, track_number, upload_date as date).
TAG_RULES = [
    "%(release_year,release_date>%Y,upload_date>%Y|)s:%(meta_date)s",
    "%(track_number,playlist_index|)s:%(meta_track)s",
]

# Center-crop covers to a square (YouTube thumbnails are 16:9). Only applies
# when the thumbnail is actually converted (webp -> jpg); jpg covers from
# Bandcamp/SoundCloud are already square and are left alone.
SQUARE_COVER = 'ThumbnailsConvertor+ffmpeg_o:-vf "crop=min(iw\\,ih):min(iw\\,ih)"'
# Containers that can carry embedded cover art (WAV cannot).
COVER_FORMATS = {"mp3_128", "mp3_320", "flac", "aac"}


def build_ytdlp_command(url, fmt, output_path, ytdlp=None, ffmpeg_location=None, temp_dir=None):
    fmt = fmt if fmt in FORMATS else "mp3_320"
    cmd = list(ytdlp if ytdlp is not None else ytdlp_manager.current()) + FORMATS[fmt] + [
        # one download pass (no separate video+audio + merge), one progress ramp
        "-f", "bestaudio/best",
        "--newline",      # progress updates as lines, not \r-glued into one blob
        "--no-playlist",  # a track link with ?list= context is still one track
        "--no-colors",
        "--ignore-config",       # a user's own yt-dlp config must not move files elsewhere
        "--no-abort-on-error",   # a dead track in a playlist skips, the rest still downloads
        "-o", OUTPUT_TEMPLATE,
        "-P", output_path,
        # each run gets its own temp dir, so a cancelled run's partial files
        # can be removed without touching anything else
        "-P", f"temp:{temp_dir or TEMP_DIR}",
        "--embed-metadata",
    ]
    for rule in NAMING_RULES + TAG_RULES:
        cmd += ["--parse-metadata", rule]
    if fmt in COVER_FORMATS:
        cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg", "--ppa", SQUARE_COVER]
    if ffmpeg_location:
        cmd += ["--ffmpeg-location", ffmpeg_location]
    cmd += ["--", url]
    return cmd


# ---------------------------------------------------------------------------
# yt-dlp output -> the same __WAX__ events the Spotify adapter emits.
# ---------------------------------------------------------------------------
ERROR_MESSAGES = [
    # order matters: specific causes before broad ones like "unable to download"
    (("drm",), "this one is drm-protected"),
    (("http error 429", "too many requests", "rate-limit", "rate limit"),
     "too many requests, try again later"),
    (("ffmpeg not found", "ffprobe and ffmpeg not found", "ffmpeg is not installed",
      "ffmpeg could not be found"), "ffmpeg is missing"),
    (("private video", "this video is private", "is private", "private track", "private playlist"),
     "this one is private"),
    (("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
     "this one is age-restricted"),
    (("not a bot",), "youtube wants a sign-in, try again later"),
    (("not available in your country", "not made this video available in your country",
      "blocked it in your country", "geo restrict", "geo-restrict", "geoblock", "your location"),
     "not available in your country"),
    (("sign in", "log in", "login required", "logged-in", "members-only", "join this channel",
      "requires authentication", "account cookies", "premium"),
     "this one needs a sign-in"),
    (("live event will begin", "premieres in", "is not yet available", "this live stream"),
     "this stream hasn't started yet"),
    (("unsupported url", "is not a valid url", "no suitable extractor"), "this link isn't supported"),
    (("video unavailable", "has been removed", "been terminated", "no longer available",
      "does not exist", "not found", "http error 404", "http error 410", "copyright",
      "is unavailable", "not available", "this track was deleted"),
     "this one is gone or unavailable"),
    (("http error 403", "forbidden"), "the site refused the download, try again later"),
    (("failed to resolve", "getaddrinfo", "name or service not known", "nodename nor servname",
      "temporary failure in name resolution", "no address associated", "network is unreachable",
      "connection refused", "connection reset", "timed out", "unable to connect",
      "no route to host", "urlopen error", "errno 8", "errno 11001", "remote end closed",
      "ssl", "unable to download webpage"),
     "no connection, check your internet"),
    (("no space left", "disk full"), "the disk is full"),
    (("permission denied", "read-only file system", "access is denied"), "can't write to that folder"),
]
GENERIC_ERROR = "couldn't grab this one"


def friendly_error(line):
    """Turn a raw yt-dlp ERROR line into a short lowercase message for the UI."""
    text = (line or "").lower()
    for needles, message in ERROR_MESSAGES:
        if any(n in text for n in needles):
            return message
    return GENERIC_ERROR


def extractor_outdated(line):
    """Does this yt-dlp ERROR look like a stale extractor (fixed by updating)?

    Errors with a known, unrelated cause (sign-in, bot check, private, geo,
    rate limit, network…) never count, even if yt-dlp asks for a bug report.
    """
    text = (line or "").lower()
    if any(hint in text for hint in ytdlp_update.OUTDATED_SURE):
        return True
    if friendly_error(line) not in (GENERIC_ERROR, "the site refused the download, try again later"):
        return False
    return any(hint in text for hint in ytdlp_update.OUTDATED_HINTS)


_ITEM_RE = re.compile(r"^\[download\] Downloading item (\d+) of (\d+)")
_PERCENT_RE = re.compile(r"^\[download\]\s+([\d.]+)%")
_CONVERT_RE = re.compile(r"^\[(ExtractAudio|Metadata|EmbedThumbnail|FixupM4a|FixupM3u8)\]")
# the audio file of the current item is on disk (converted, or kept as is)
_DONE_RE = re.compile(r"^\[ExtractAudio\] (Destination: |Not converting audio)|has already been downloaded")

# share of a track's slice of the ring spent downloading; the rest is ffmpeg
DOWNLOAD_SHARE = 0.9


class YtdlpProgress:
    """Feed yt-dlp stdout lines, get UI events back.

    Progress is overall: on an album each track owns 1/M of the ring, so the
    ring no longer hits 100% after the first file.
    """

    def __init__(self):
        self.index = 1          # current item, 1-based
        self.total = 1          # items in the link (1 for a single track)
        self.playlist = False
        self.file_pct = 0.0     # 0..100 within the current item
        self.failed = set()     # items that hit an ERROR
        self.finished = set()   # items whose audio file was written
        self.first_error = ""
        self.outdated = False   # an error suggests updating yt-dlp
        self.last_percent = -1.0
        self.last_message = None

    def _overall(self):
        done = (self.index - 1) + self.file_pct / 100.0
        return round(min(100.0, 100.0 * done / max(1, self.total)), 1)

    def _label(self, converting=False):
        if self.playlist:
            word = "converting" if converting else "track"
            return f"{word} {self.index} of {self.total}"
        return "converting…" if converting else "downloading…"

    def _progress(self, message):
        percent = self._overall()
        if percent == self.last_percent and message == self.last_message:
            return []
        self.last_percent, self.last_message = percent, message
        return [{"type": "progress", "percent": percent, "message": message}]

    def feed(self, line):
        line = line.strip()
        m = _ITEM_RE.match(line)
        if m:
            self.playlist = True
            self.index, self.total = int(m.group(1)), max(1, int(m.group(2)))
            self.file_pct = 0.0
            return self._progress(self._label())
        if line.startswith("ERROR:"):
            self.failed.add(self.index)
            self.outdated = self.outdated or extractor_outdated(line)
            message = friendly_error(line)
            if not self.first_error:
                self.first_error = message
            if self.playlist:
                return []  # yt-dlp moves on to the next item; reported in the result
            return [{"type": "error", "message": message}]
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
            events.append({"type": "error", "message": self.first_error or GENERIC_ERROR})
        events.append({"type": "result", "saved": saved, "failed": self.total - saved, "total": self.total})
        return events, ok


_GOFI_PROGRESS_RE = re.compile(r"^downloading (\d+) of (\d+)$", re.I)


def gofi_event(raw):
    """Normalize a wax-gofi event to the UI's copy: lowercase, 'track N of M'."""
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    message = event.get("message")
    if isinstance(message, str):
        m = _GOFI_PROGRESS_RE.match(message.strip())
        event["message"] = f"track {m.group(1)} of {m.group(2)}" if m else message.lower()
    return event


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


# The yt-dlp a download runs is resolved per download: a user copy in the app
# data dir that keeps itself current, or the bundled one if that is newer.
ytdlp_manager = ytdlp_update.YtdlpManager(
    base=find_ytdlp,
    bundled=lambda: bundled_binary("yt-dlp"),
    load_config=load_config,
    save_config=save_config,
)
UPDATED_MESSAGE = "updated yt-dlp — try again"


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
        return jsonify({"error": "Host not allowed"}), 403
    origin = request.headers.get("Origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        return jsonify({"error": "Origin not allowed"}), 403
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


@app.route("/status")
def status():
    cfg = load_config()
    return jsonify({"status": "running", "output_path": cfg.get("output_path", DEFAULT_FOLDER),
                    "ytdlp_version": ytdlp_manager.known_version()})


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
            return jsonify({"error": "ARL must be 192 characters"}), 400
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

    if not url:
        return jsonify({"error": "URL is required"}), 400

    is_spotify = spotify_link(url)
    if is_spotify:
        if fmt not in ("mp3_128", "mp3_320", "flac"):
            return jsonify({"error": "Deezer supports MP3 128, MP3 320 and FLAC"}), 400
        if not find_gofi():
            return jsonify({"error": "Deezer downloader is not installed in this build"}), 503
        if not deezer_arl:
            return jsonify({"error": "Connect Deezer first"}), 400

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
    proc = None
    workdir = None
    status = "error"
    entry["status"] = "downloading"
    try:
        os.makedirs(item.temp_dir, exist_ok=True)
        gofi = find_gofi() if item.is_spotify else None
        if item.is_spotify:
            if not gofi:
                raise RuntimeError("Deezer downloader is not installed in this build")
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
                       "quality": {"mp3_128": "128", "mp3_320": "320", "flac": "flac"}[item.fmt],
                       "output": os.path.abspath(item.output_path), "arl": deezer_arl}
            json.dump(payload, proc.stdin)
            proc.stdin.close()
        tracker = None if item.is_spotify else YtdlpProgress()
        for line in proc.stdout:
            line = line.rstrip()
            if item.is_spotify:
                if line.startswith("WAX_EVENT:"):
                    event = gofi_event(line[len("WAX_EVENT:"):])
                    if event is not None:
                        q.put("__WAX__:" + json.dumps(event))
                continue
            for event in tracker.feed(line):
                q.put("__WAX__:" + json.dumps(event))
            if "Destination:" in line:
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
            events, ok = tracker.finish(proc.returncode)
            if not ok and tracker.outdated and update_after_failure(q):
                events.insert(-1, {"type": "error", "message": UPDATED_MESSAGE})
            for event in events:
                q.put("__WAX__:" + json.dumps(event))
        status = "done" if ok else "error"
    except _Cancelled:
        status = "cancelled"
    except Exception as e:
        status = "cancelled" if item.cancelled else "error"
        if status == "error":
            q.put("__WAX__:" + json.dumps({"type": "error", "message": friendly_error(str(e))}))
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
    q.put("__WAX__:" + json.dumps({"type": "status", "message": "updating yt-dlp…"}))
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


@app.route("/history")
def get_history():
    return jsonify(history)


def _port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((HOST, PORT)) == 0


QUIT_TITLE = "quit wax?"
QUIT_MESSAGE = "a download is still running. quitting will stop it."


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
                ok = self.window.create_confirmation_dialog(QUIT_TITLE, QUIT_MESSAGE)
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
        for title, selector in (("Open Wax", "openWindow:"), ("Hide Wax", "hideWindow:")):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, "")
            item.setTarget_(delegate)
            menu.addItem_(item)
        menu.addItem_(NSMenuItem.separatorItem())
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit Wax", "quitApp:", "")
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
    api.menu_bar_ready = _install_menu_bar(api)
    webview.start()


if __name__ == "__main__":
    main()
