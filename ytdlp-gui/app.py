import itertools
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import webview
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

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


YTDLP_CMD = find_ytdlp()
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


def build_ytdlp_command(url, fmt, output_path, ytdlp=None, ffmpeg_location=None):
    fmt = fmt if fmt in FORMATS else "mp3_320"
    cmd = list(ytdlp or YTDLP_CMD) + FORMATS[fmt] + [
        # one download pass (no separate video+audio + merge), one progress ramp
        "-f", "bestaudio/best",
        "--newline",      # progress updates as lines, not \r-glued into one blob
        "--no-playlist",  # a track link with ?list= context is still one track
        "--no-colors",
        "--ignore-config",       # a user's own yt-dlp config must not move files elsewhere
        "--no-abort-on-error",   # a dead track in a playlist skips, the rest still downloads
        "-o", OUTPUT_TEMPLATE,
        "-P", output_path,
        "-P", f"temp:{TEMP_DIR}",
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


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"output_path": DEFAULT_FOLDER}


def save_config(data):
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f)


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
    return jsonify({"status": "running", "output_path": cfg.get("output_path", DEFAULT_FOLDER)})


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
        gofi = find_gofi()
        if not gofi:
            return jsonify({"error": "Deezer downloader is not installed in this build"}), 503
        if not deezer_arl:
            return jsonify({"error": "Connect Deezer first"}), 400

    os.makedirs(TEMP_DIR, exist_ok=True)
    cmd = build_ytdlp_command(url, fmt, output_path, ffmpeg_location=FFMPEG_LOCATION)

    q = queue.Queue()
    stream_id = next(_stream_ids)
    active_streams[stream_id] = q

    entry = {"id": stream_id, "url": url, "format": fmt, "status": "downloading", "filename": ""}
    history.insert(0, entry)
    if len(history) > 10:
        history.pop()

    def run():
        proc = None
        workdir = None
        try:
            kwargs = {}
            if IS_WINDOWS:
                # don't pop a console window for the child process
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            workdir = tempfile.TemporaryDirectory(prefix="wax-gofi-") if is_spotify else None
            child_cmd = [gofi] if is_spotify else cmd
            proc = subprocess.Popen(
                child_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if is_spotify else None,
                cwd=workdir.name if workdir else None,
                text=True,
                bufsize=1,
                **kwargs,
            )
            active_procs.add(proc)
            if is_spotify:
                payload = {"url": url if "://" in url else "https://" + url,
                           "quality": {"mp3_128": "128", "mp3_320": "320", "flac": "flac"}[fmt],
                           "output": os.path.abspath(output_path), "arl": deezer_arl}
                json.dump(payload, proc.stdin)
                proc.stdin.close()
            for line in proc.stdout:
                line = line.rstrip()
                if is_spotify:
                    if line.startswith("WAX_EVENT:"):
                        q.put("__WAX__:" + line[len("WAX_EVENT:"):])
                else:
                    q.put(line)
                if "Destination:" in line:
                    parts = line.split("Destination:")
                    if len(parts) > 1:
                        entry["filename"] = os.path.basename(parts[1].strip())
                elif "has already been downloaded" in line:
                    fname = line.split("]")[-1].strip().split(" has already")[0].strip()
                    if fname:
                        entry["filename"] = os.path.basename(fname)
            proc.wait()
            status = "done" if proc.returncode == 0 else "error"
            entry["status"] = status
        except Exception as e:
            status = "error"
            entry["status"] = status
            q.put(f"ERROR: {e}")
        finally:
            active_procs.discard(proc)
            if is_spotify and workdir:
                workdir.cleanup()
            q.put(f"__DONE__:{status}")
            # if the window never subscribed (or dropped off), do not keep it forever
            reaper = threading.Timer(STREAM_TTL, active_streams.pop, (stream_id, None))
            reaper.daemon = True  # must not hold the app open on quit
            reaper.start()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"stream_id": stream_id})


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


class Api:
    """Bridge exposed to the UI as window.pywebview.api."""

    def __init__(self):
        self.window = None
        self.menu_bar_ready = False

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
            self.window.hide()
        else:
            self.quit()

    def stop_downloads(self):
        for p in list(active_procs):
            try:
                p.terminate()
            except Exception:
                pass

    def quit(self):
        self.stop_downloads()
        if self.window:
            # destroy() straight from a js_api call hangs the app: pywebview's
            # bridge thread then waits forever for a return value the stopped
            # event loop can no longer deliver, and it is not a daemon thread.
            threading.Timer(0.1, self.window.destroy).start()


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
                api.quit()

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
    api.window.events.closing += api.stop_downloads
    api.menu_bar_ready = _install_menu_bar(api)
    webview.start()


if __name__ == "__main__":
    main()
