"""Show in folder, clipboard link, DRM hint, versions, Windows rounded corners."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")


class RevealTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.file = os.path.join(self.root, "Album", "01 a.mp3")
        os.makedirs(os.path.dirname(self.file))
        open(self.file, "w").close()

    def test_only_inside_the_roots(self):
        self.assertTrue(wax.reveal_allowed(self.file, {self.root}))
        self.assertTrue(wax.reveal_allowed(os.path.dirname(self.file), {self.root}))
        self.assertTrue(wax.reveal_allowed(self.root, {self.root}))
        self.assertFalse(wax.reveal_allowed(self.root, {self.file}))
        self.assertFalse(wax.reveal_allowed("/etc/passwd", {self.root}))
        self.assertFalse(wax.reveal_allowed(self.root + "-evil", {self.root}))

    def test_dotdot_symlink_missing_and_junk_are_refused(self):
        self.assertFalse(wax.reveal_allowed(os.path.join(self.root, "..", "etc"), {self.root}))
        link = os.path.join(self.root, "out")
        os.symlink("/etc", link)
        self.assertFalse(wax.reveal_allowed(link, {self.root}))
        self.assertFalse(wax.reveal_allowed(os.path.join(self.root, "nope.mp3"), {self.root}))
        for bad in (None, 5, "", "a\0b", ["x"]):
            self.assertFalse(wax.reveal_allowed(bad, {self.root}))

    def test_commands_per_platform(self):
        self.assertEqual(wax.reveal_command("/o/a.mp3", False, "darwin"), ["/usr/bin/open", "-R", "/o/a.mp3"])
        self.assertEqual(wax.reveal_command("/o/A", True, "darwin"), ["/usr/bin/open", "/o/A"])
        self.assertEqual(wax.reveal_command("/o/a.mp3", False, "linux"), ["xdg-open", "/o"])
        self.assertEqual(wax.reveal_command("/o/A", True, "linux"), ["xdg-open", "/o/A"])
        self.assertEqual(wax.reveal_command("C:/o/a.mp3", False, "win32")[:2], ["explorer", "/select,"])
        self.assertEqual(wax.reveal_command("C:/o/A", True, "win32")[0], "explorer")

    def test_reveal_path_runs_argv_without_a_shell(self):
        calls = []
        popen = lambda cmd, **kw: calls.append((cmd, kw))
        self.assertTrue(wax.reveal_path(self.file, {self.root}, "darwin", popen))
        cmd, kw = calls[0]
        self.assertEqual(cmd, ["/usr/bin/open", "-R", self.file])
        self.assertFalse(kw.get("shell"))
        self.assertFalse(wax.reveal_path("/etc/passwd", {self.root}, "darwin", popen))
        self.assertEqual(len(calls), 1)

    def test_bridge_reveal_refuses_foreign_paths(self):
        self.assertFalse(wax.Api().reveal("/etc/passwd"))
        self.assertFalse(wax.Api().reveal(None))


class ResultPathTests(unittest.TestCase):
    def test_paths_come_from_the_final_audio_files(self):
        out, tmp = "/out", "/tmp/run-1"
        t = wax.YtdlpProgress(out, tmp)
        for line in [
            "[download] Destination: /tmp/run-1/A - E/01 One.webm",
            "[ExtractAudio] Destination: /tmp/run-1/A - E/01 One.mp3",
            '[MoveFiles] Moving file "/tmp/run-1/A - E/01 One.mp3" to "/out/A - E/01 One.mp3"',
            '[MoveFiles] Moving file "/tmp/run-1/A - E/01 One.jpg" to "/out/A - E/01 One.jpg"',
            "[ExtractAudio] Destination: /tmp/run-1/A - E/02 Two.mp3",
            "[download] /out/A - E/03 Three.mp3 has already been downloaded",
        ]:
            t.feed(line)
        events, ok = t.finish(0)
        self.assertEqual(events[-1]["paths"], ["/out/A - E/01 One.mp3", "/out/A - E/02 Two.mp3",
                                               "/out/A - E/03 Three.mp3"])
        self.assertEqual(events[-1]["folder"], "/out/A - E")

    def test_single_track_folder_is_the_output_folder_and_no_paths_without_a_folder(self):
        t = wax.YtdlpProgress("/out", "/tmp/r")
        t.feed("[ExtractAudio] Destination: /tmp/r/Artist - Title.flac")
        self.assertEqual(t.finish(0)[0][-1]["folder"], "/out")
        t = wax.YtdlpProgress()
        t.feed("[ExtractAudio] Destination: /tmp/r/Artist - Title.flac")
        self.assertNotIn("paths", t.finish(0)[0][-1])


class ClipboardTests(unittest.TestCase):
    def test_exactly_one_link(self):
        ok = ["https://youtu.be/abc", "  http://a.bandcamp.com/track/x?y=1\n", "HTTPS://Example.com/x"]
        for text in ok:
            self.assertEqual(wax.parse_clipboard_url(text), text.strip())
        bad = ["", "   ", "hello", "youtu.be/abc", "ftp://x.com/a", "file:///etc/passwd", "javascript:alert(1)",
               "https://a.com/x https://b.com/y", "see https://a.com", "https://a.com/x\nhttps://b.com",
               "https://localhost/x", "https://", "http://999", "x" * 3000, None, 7]
        for text in bad:
            self.assertEqual(wax.parse_clipboard_url(text), "", repr(text)[:40])

    def test_bridge_returns_the_parsed_link(self):
        real = wax.read_clipboard_text
        try:
            wax.read_clipboard_text = lambda: "https://youtu.be/abc\n"
            self.assertEqual(wax.Api().clipboard_url(), "https://youtu.be/abc")
            wax.read_clipboard_text = lambda: "just words"
            self.assertEqual(wax.Api().clipboard_url(), "")
        finally:
            wax.read_clipboard_text = real


class VersionTests(unittest.TestCase):
    def test_status_serves_the_app_version(self):
        data = wax.app.test_client().get("/status").get_json()
        self.assertEqual(data["version"], wax.APP_VERSION)
        self.assertRegex(wax.APP_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertIn("ytdlp_version", data)


class RoundCornersTests(unittest.TestCase):
    def test_region_math(self):
        # 100% scale: window 600x432 -> region box one pixel past the edge, 128px corner diameter
        self.assertEqual(wax.round_region_params(600, 432, 96), (601, 433, 128))
        # 150% scale: a 900x648 physical window, corner diameter 192
        self.assertEqual(wax.round_region_params(900, 648, 144), (901, 649, 192))
        self.assertEqual(wax.round_region_params(1200, 864, 192), (1201, 865, 256))
        self.assertEqual(wax.round_region_params(600, 260, 0), (601, 261, 128))  # unknown dpi -> 96

    def test_never_touches_other_platforms(self):
        if sys.platform != "win32":
            self.assertFalse(wax.apply_round_corners(object()))
            wax.schedule_round_corners(object())  # no-op, no threads, no errors


class PageTests(unittest.TestCase):
    def test_copy_is_lowercase_and_new_states_exist(self):
        page = open(PAGE, encoding="utf-8").read()
        for label in ("show in folder", "grab the wax", "add to queue", "mp3 320k", "deezer arl"):
            self.assertIn(label, page)
        for label in ("GRAB THE WAX", "ADD TO QUEUE", "MP3 128K"):
            self.assertNotIn(label, page)
        for label in ("Deezer ARL", "Deezer session", "Connect Deezer"):
            self.assertNotIn(label, page)
        self.assertIn("let fmtIndex = 1;", page)
        self.assertNotIn("setTimeout(reset, 2000)", page)
        for bridge in ("reveal", "clipboard_url"):
            self.assertTrue(callable(getattr(wax.Api, bridge)))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_drm_message_and_drop_parsing_in_the_page_js(self):
        page = open(PAGE, encoding="utf-8").read()
        block = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        strings = re.search(r'<script type="application/json" id="strings">(.*?)</script>', page, re.S).group(1)
        src = "\n".join([
            "const document = { getElementById: () => ({ textContent: %s }) };" % json.dumps(strings),
            block("const STRINGS", "const msg"),
            'lang = "en";',   # not the language of the machine running the test
            block("const UNSUPPORTED", "function validate"),
            block("function isLikelyUrl", "// While busy"),
            block("function hostOf", "function validate"),
            block("function linkFromDrop", "// Without these"),
        ])
        probe = src + """
        const out = {
          apple: drmMessage(drmService(hostOf("https://music.apple.com/us/album/x"))),
          appleRu: (lang = "ru", drmMessage("apple music")),
          amazon: drmService(hostOf("music.amazon.co.uk/albums/x")),
          deezer: drmService(hostOf("https://www.deezer.com/track/1")),
          fine: [drmService(hostOf("https://open.spotify.com/track/1")), drmService(hostOf("https://x.bandcamp.com/a"))],
          drop1: linkFromDrop("# comment\\r\\nhttps://youtu.be/abc\\r\\n", ""),
          drop2: linkFromDrop("", "  https://a.com/x \\nmore"),
          drop3: linkFromDrop("file:///home/u/a.mp3", ""),
          drop4: linkFromDrop("", "just words"),
        };
        console.log(JSON.stringify(out));"""
        r = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["apple"], "apple music is drm-locked — try bandcamp, youtube or spotify")
        self.assertEqual(out["appleRu"], "apple music под drm — попробуй bandcamp, youtube или spotify")
        self.assertEqual(out["amazon"], "amazon music")
        self.assertEqual(out["deezer"], "deezer")
        self.assertEqual(out["fine"], ["", ""])
        self.assertEqual(out["drop1"], "https://youtu.be/abc")
        self.assertEqual(out["drop2"], "https://a.com/x")
        self.assertEqual(out["drop3"], "")
        self.assertEqual(out["drop4"], "")


if __name__ == "__main__":
    unittest.main()
