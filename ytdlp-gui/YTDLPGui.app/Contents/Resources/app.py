import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

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
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    for p in YTDLP_EXTRA_PATHS:
        candidate = os.path.join(p, "yt-dlp")
        if os.path.isfile(candidate):
            return [candidate]
    # fallback: run as python module
    return [sys.executable, "-m", "yt_dlp"]

YTDLP_CMD = find_ytdlp()

CONFIG_PATH = os.path.expanduser("~/.ytdlpgui_config.json")
DEFAULT_FOLDER = os.path.expanduser("~/Downloads")

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
    script = 'tell application "Finder" to set f to choose folder\nPOSIX path of f'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    path = result.stdout.strip()
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
    tmp_dir = "/tmp/ytdlp-gui-temp"
    os.makedirs(tmp_dir, exist_ok=True)
    cmd = YTDLP_CMD + fmt_args + [
        "-k",
        "-o", "%(title)s.%(ext)s",
        "-P", output_path,
        "-P", f"temp:{tmp_dir}",
        url,
    ]

    stream_id = threading.get_ident()
    q = queue.Queue()
    active_streams[id(q)] = q

    entry = {"id": id(q), "url": url, "format": fmt, "status": "downloading", "filename": ""}
    history.insert(0, entry)
    if len(history) > 10:
        history.pop()

    def run():
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7337, debug=False)
