"""The per-download log behind the details screen: python3 ytdlp-gui/test_details.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax

ARL = "0123456789abcdef" * 12  # 192 hex chars, like a real one


def helper_script(directory, body):
    """A fake child process (wax-gofi or yt-dlp) running the given Python body."""
    path = os.path.join(directory, "helper")
    with open(path, "w") as f:
        f.write("#!/usr/bin/env python3\nimport json, sys\n"
                "def ev(**e): print('WAX_EVENT:' + json.dumps(e), flush=True)\n" + body)
    os.chmod(path, 0o700)
    return path


@unittest.skipIf(os.name == "nt", "test helpers use a Unix shebang")
class DownloadLogTests(unittest.TestCase):
    def setUp(self):
        self.client = wax.app.test_client()
        self.old_arl = wax.deezer_arl
        wax.deezer_arl = ARL
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        wax.deezer_arl = self.old_arl
        self.tmp.cleanup()

    def run_spotify(self, body):
        helper = helper_script(self.tmp.name, "request = json.load(sys.stdin)\n" + body)
        with patch.object(wax, "find_gofi", return_value=helper):
            r = self.client.post("/download", json={"url": "https://open.spotify.com/album/abc",
                                                    "format": "mp3_320", "output_path": self.tmp.name})
            self.assertEqual(r.status_code, 200)
            sid = r.json["stream_id"]
            stream = self.client.get(f"/progress/{sid}").get_data(as_text=True)
        return sid, stream

    def run_ytdlp(self, body):
        helper = helper_script(self.tmp.name, body)
        with patch.object(wax, "build_ytdlp_command", return_value=[helper]):
            r = self.client.post("/download", json={"url": "https://example.com/a", "format": "mp3_320",
                                                    "output_path": self.tmp.name})
            sid = r.json["stream_id"]
            stream = self.client.get(f"/progress/{sid}").get_data(as_text=True)
        return sid, stream

    def test_per_track_failures_are_structured(self):
        sid, stream = self.run_spotify(
            "ev(type='status', message='Finding tracks on Deezer…')\n"
            "print('gofi: a stray line')\n"
            "print('\\x1b[31mERR\\x1b[0m need user auth')\n"
            "ev(type='track', status='failed', artist='A', title='Gone', reason='not found on deezer')\n"
            "ev(type='track', status='failed', artist='', title='', reason='not found on deezer', count=2)\n"
            "ev(type='track', status='skipped', artist='A', title='Old', reason='already there')\n"
            "ev(type='track', status='failed', artist='A', title='Hq', reason='not available in mp3 320 for this account',"
            " detail=\"your account can't stream mp3_320 tracks\")\n"
            "ev(type='result', saved=2, failed=4, total=6, skipped=1)\n"
            "sys.exit(1)\n")
        self.assertIn("__DONE__:error", stream)
        self.assertNotIn('"type": "track"', stream, "track events are for the log, not the stream")
        log = self.client.get(f"/log/{sid}").json
        self.assertEqual(log["state"], "error")
        self.assertEqual(log["source"], "deezer")
        self.assertEqual(log["result"], {"saved": 2, "failed": 4, "total": 6, "skipped": 1})
        self.assertEqual(log["failures"], [
            {"artist": "A", "title": "Gone", "reason": "not found on deezer"},
            {"reason": "not found on deezer", "count": 2},
            {"artist": "A", "title": "Hq", "reason": "not available in mp3 320 for this account",
             "detail": "your account can't stream mp3_320 tracks"},
        ])
        self.assertEqual(log["skipped"], [{"artist": "A", "title": "Old", "reason": "already there"}])
        self.assertIn("gofi: a stray line", log["lines"])
        self.assertIn("ERR need user auth", log["lines"], "terminal colours must be stripped")
        self.assertTrue(any(line.startswith("WAX_EVENT:") for line in log["lines"]))

    def test_arl_never_reaches_the_log(self):
        sid, _ = self.run_spotify(
            "print('request arl ' + request['arl'])\n"
            "print('cookie arl=' + request['arl'][:40] + '; path=/')\n"
            "ev(type='error', message='bad session ' + request['arl'])\n"
            "ev(type='track', status='failed', artist='A', title='T', reason='x', detail='token ' + request['arl'])\n"
            "sys.exit(1)\n")
        response = self.client.get(f"/log/{sid}")
        body = response.get_data(as_text=True)
        self.assertNotIn(ARL, body)
        self.assertNotIn(ARL[:40], body)
        self.assertIn(wax.HIDDEN, body)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_raw_output_is_capped_to_the_tail(self):
        sid, _ = self.run_ytdlp(
            f"for i in range({wax.LOG_LINES + 150}): print('line', i)\n"
            "print('x' * 5000)\n"
            "print('ERROR: [youtube] abc: Video unavailable')\n"
            "sys.exit(1)\n")
        log = self.client.get(f"/log/{sid}").json
        self.assertEqual(len(log["lines"]), wax.LOG_LINES)
        self.assertEqual(log["dropped"], 152)
        self.assertEqual(log["lines"][-1], "ERROR: [youtube] abc: Video unavailable")
        self.assertTrue(all(len(line) <= wax.LOG_LINE_CHARS + 1 for line in log["lines"]))
        self.assertEqual(log["errors"], [{"message": "this one is gone or unavailable",
                                          "line": "ERROR: [youtube] abc: Video unavailable"}])
        self.assertEqual(log["message"], "this one is gone or unavailable")
        self.assertEqual(log["source"], "yt-dlp")

    def test_ytdlp_playlist_failures_by_item(self):
        sid, _ = self.run_ytdlp(
            "print('[download] Downloading item 1 of 3')\n"
            "print('[download] 100% of 1MiB')\n"
            "print('[ExtractAudio] Destination: /x/a.mp3')\n"
            "print('[download] Downloading item 2 of 3')\n"
            "print('ERROR: [youtube] b: Private video')\n"
            "print('[download] Downloading item 3 of 3')\n"
            "print('ERROR: [youtube] c: Video unavailable')\n"
            "sys.exit(1)\n")
        log = self.client.get(f"/log/{sid}").json
        self.assertEqual([(f["title"], f["reason"]) for f in log["failures"]],
                         [("track 2", "this one is private"), ("track 3", "this one is gone or unavailable")])
        self.assertEqual(log["result"]["saved"], 1)

    def test_only_the_last_downloads_are_kept(self):
        logs = [wax.remember_log(wax.DownloadLog(10_000 + i, "u", "mp3_320", "yt-dlp"))
                for i in range(wax.LOG_KEEP + 3)]
        self.assertLessEqual(len(wax.download_logs), wax.LOG_KEEP)
        self.assertIsNone(wax.find_log(logs[0].id))
        self.assertIs(wax.find_log(logs[-1].id), logs[-1])
        self.assertEqual(self.client.get(f"/log/{logs[0].id}").status_code, 404)

    def test_log_is_local_only(self):
        log = wax.remember_log(wax.DownloadLog(20_000, "u", "mp3_320", "yt-dlp"))
        self.assertEqual(self.client.get(f"/log/{log.id}").status_code, 200)
        self.assertEqual(self.client.get(f"/log/{log.id}", headers={"Host": "evil.test"}).status_code, 403)
        self.assertEqual(self.client.get(f"/log/{log.id}", headers={"Origin": "https://evil.test"}).status_code, 403)


class ScrubTests(unittest.TestCase):
    def test_patterns(self):
        self.assertEqual(wax.scrub("x arl=abcdef123 y"), "x arl=[hidden] y")
        self.assertEqual(wax.scrub('{"arl": "abcdef123"}'), '{"arl": "[hidden]"}')
        self.assertEqual(wax.scrub("Cookie: sid=1; arl=2"), "Cookie: [hidden]")
        self.assertEqual(wax.scrub("Authorization: Bearer abc.def"), "Authorization: [hidden]")
        self.assertEqual(wax.scrub("raw " + ARL), "raw [hidden]")
        self.assertEqual(wax.scrub("s3cretvalue!", ["s3cretvalue!"]), "[hidden]")
        self.assertEqual(wax.scrub("[download] 50% of 3MiB"), "[download] 50% of 3MiB")


class ClipboardTests(unittest.TestCase):
    def test_copy_text_feeds_stdin_without_a_shell(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0)

        self.assertTrue(wax.write_clipboard_text("log ✓ ёж", platform="darwin", run=run))
        cmd, kwargs = calls[0]
        self.assertEqual(cmd, ["/usr/bin/pbcopy"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["input"], "log ✓ ёж".encode("utf-8"))
        self.assertFalse(wax.write_clipboard_text(None, platform="darwin", run=run))

    def test_commands_per_platform(self):
        win = wax.clipboard_write_commands("win32")[0]
        self.assertEqual(win[0], "powershell")
        self.assertIn("Set-Clipboard", win[-1])
        self.assertEqual([c[0] for c in wax.clipboard_write_commands("linux")], ["wl-copy", "xclip"])

    def test_bridge_has_copy_text(self):
        self.assertTrue(callable(getattr(wax.Api, "copy_text", None)))

    def test_failed_tool_is_not_success(self):
        def run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1)
        self.assertFalse(wax.write_clipboard_text("x", platform="darwin", run=run))


if __name__ == "__main__":
    unittest.main()
