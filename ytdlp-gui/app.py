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

download_queue = queue.Queue()
history = []
active_streams = {}
active_procs = set()

FORMATS = {
    "mp3_128": ["-x", "--audio-format", "mp3", "--audio-quality", "5"],
    "mp3_320": ["-x", "--audio-format", "mp3", "--audio-quality", "0"],
    "flac":    ["-x", "--audio-format", "flac"],
    "wav":     ["-x", "--audio-format", "wav"],
    "aac":     ["-x", "--audio-format", "aac", "--audio-quality", "0"],
}


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


# Allow the Chrome/Firefox extension (popup + injected content script) to call the
# local server cross-origin. Extension contexts report an "Origin" like
# chrome-extension://<id>, so we echo it back when it matches.
ALLOWED_ORIGIN_PREFIXES = ("chrome-extension://", "moz-extension://")


@app.after_request
def add_cors_headers(resp):
    origin = request.headers.get("Origin", "")
    if origin.startswith(ALLOWED_ORIGIN_PREFIXES):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Vary"] = "Origin"
    return resp


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
    # This endpoint accepts secrets only from Wax itself, not browser extensions.
    origin = request.headers.get("Origin")
    if origin and origin != URL:
        return jsonify({"error": "Origin not allowed"}), 403
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

    fmt_args = FORMATS.get(fmt, FORMATS["mp3_320"])
    os.makedirs(TEMP_DIR, exist_ok=True)
    cmd = YTDLP_CMD + fmt_args + [
        # one download pass (no separate video+audio + merge), one progress ramp
        "-f", "bestaudio/best",
        "--newline",      # progress updates as lines, not \r-glued into one blob
        "--no-playlist",  # a track link with ?list= context is still one track
        "-o", "%(title)s.%(ext)s",
        "-P", output_path,
        "-P", f"temp:{TEMP_DIR}",
    ]
    if FFMPEG_LOCATION:
        cmd += ["--ffmpeg-location", FFMPEG_LOCATION]
    cmd.append(url)

    q = queue.Queue()
    active_streams[id(q)] = q

    entry = {"id": id(q), "url": url, "format": fmt, "status": "downloading", "filename": ""}
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

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"stream_id": id(q)})


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
