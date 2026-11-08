"""Smoke check for the WAX UI: python3 ytdlp-gui/test_ui.py"""
import inspect
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax


def main():
    c = wax.app.test_client()

    page = c.get("/").get_data(as_text=True)
    assert page.startswith("<!DOCTYPE html>"), "index.html not rendered"
    for marker in ('id="urlInput"', 'id="folderRow"', 'id="actionBtn"', 'id="progress"', 'id="infoRow"'):
        assert marker in page, f"missing {marker}"

    assert c.get("/logo.png").status_code == 200, "logo.png is not served"

    # the chip cycles through formats; every one of them must exist server-side
    ui_formats = re.findall(r'\["(\w+)", "[^"]+"\]', page)
    assert ui_formats, "no formats found in the UI"
    unknown = [f for f in ui_formats if f not in wax.FORMATS]
    assert not unknown, f"UI offers formats the backend does not know: {unknown}"

    # every window height the UI asks for must be one the design defines
    call = re.search(r"  resize\((.+?)\);", page)
    assert call, "the UI never calls resize()"
    heights = {int(h) for h in re.findall(r"\d+", call.group(1))}
    assert {260, 336, 396, 432}.issubset(heights), f"unexpected window heights: {heights}"

    # the window is dragged by the modal backdrop only — easy_drag would hijack
    # every mouse drag and make the url/folder fields unselectable
    assert "pywebview-drag-region" in page, "no drag region in the modal"
    src = inspect.getsource(wax.main)
    assert "easy_drag=False" in src, "easy_drag must stay off"
    assert "DRAG_REGION_DIRECT_TARGET_ONLY" in src, "drag region must be direct-target only"

    # the font is served locally: @import needs a network the app may not have
    assert "@font-face" in page and "dm-mono.woff2" in page, "font is not local"
    assert c.get("/dm-mono.woff2").status_code == 200, "dm-mono.woff2 is not served"

    # window controls: markup on the page, actions on the bridge
    for marker in ('id="winTools"', 'id="winClose"', 'id="winMin"'):
        assert marker in page, f"missing {marker}"
    for method in ("platform", "minimize", "quit"):
        assert callable(getattr(wax.Api, method, None)), f"bridge lacks {method}()"

    # one download pass, one file left behind, one track per link
    dl = inspect.getsource(wax.download)
    assert "expanduser" in dl, "download() lost expanduser"
    cmd = wax.build_ytdlp_command("https://example.com/a", "mp3_320", "/out", ytdlp=["yt-dlp"])
    assert "-k" not in cmd, "-k keeps the source video next to the mp3"
    for flag in ("bestaudio/best", "--newline", "--no-playlist", "--ignore-config",
                 "--embed-metadata", "--embed-thumbnail"):
        assert flag in cmd, f"yt-dlp command lost {flag}"
    assert cmd[-2:] == ["--", "https://example.com/a"], "the link must come after --"
    assert cmd[cmd.index("-o") + 1] == wax.OUTPUT_TEMPLATE
    assert cmd[cmd.index("-P") + 1] == "/out"
    # WAV cannot carry a cover; asking for one would fail the download
    assert "--embed-thumbnail" not in wax.build_ytdlp_command("u", "wav", "/out", ytdlp=["y"])
    # "AAC" is AAC in an .m4a container, which can hold tags and a cover
    assert wax.FORMATS["aac"][wax.FORMATS["aac"].index("--audio-format") + 1] == "m4a"

    # quitting must not leave yt-dlp/ffmpeg running (Api.window is None here,
    # so nothing tries to close a window)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    wax.active_procs.add(child)
    wax.Api().quit()
    assert child.wait(5) != 0, "Api.quit() left the child process running"
    wax.active_procs.discard(child)

    # a typed field is not a link until it parses as one
    assert "isLikelyUrl" in page, "url validation is missing"

    print("ok — routes, formats, heights, drag region, window controls, yt-dlp flags")


if __name__ == "__main__":
    main()
