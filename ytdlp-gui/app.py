import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import webbrowser

from flask import Flask, Response, jsonify, render_template, request

HOST = "127.0.0.1"
PORT = 7337
URL = f"http://{HOST}:{PORT}"
IS_WINDOWS = os.name == "nt"


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
DEFAULT_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads")
TEMP_DIR = os.path.join(tempfile.gettempdir(), "wax-digger-temp")

app = Flask(__name__, template_folder=os.path.join(RES, "templates"))

download_queue = queue.Queue()
history = []
active_streams = {}

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


@app.route("/download", methods=["POST"])
def download():
    data = request.json
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp3_320")
    output_path = data.get("output_path") or load_config().get("output_path", DEFAULT_FOLDER)

    if not url:
        return jsonify({"error": "URL is required"}), 400

    fmt_args = FORMATS.get(fmt, FORMATS["mp3_320"])
    os.makedirs(TEMP_DIR, exist_ok=True)
    cmd = YTDLP_CMD + fmt_args + [
        "-k",
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
        try:
            kwargs = {}
            if IS_WINDOWS:
                # don't pop a console window for the child process
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                **kwargs,
            )
            for line in proc.stdout:
                line = line.rstrip()
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
            q.put(f"__DONE__:{status}")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"stream_id": id(q)})


@app.route("/progress/<int:stream_id>")
def progress(stream_id):
    q = active_streams.get(stream_id)
    if q is None:
        return Response("data: Stream not found\n\n", mimetype="text/event-stream")

    def generate():
        while True:
            try:
                line = q.get(timeout=30)
            except queue.Empty:
                yield "data: \n\n"
                continue
            if line is not None and line.startswith("__DONE__:"):
                yield f"data: {line}\n\n"
                active_streams.pop(stream_id, None)
                break
            yield f"data: {line}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/history")
def get_history():
    return jsonify(history)


def _port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((HOST, PORT)) == 0


def main():
    # If an instance is already running, just surface its window and exit.
    if _port_in_use():
        webbrowser.open(URL)
        return
    threading.Timer(1.2, lambda: webbrowser.open(URL)).start()
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
