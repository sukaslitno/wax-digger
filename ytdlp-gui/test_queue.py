"""Download queue, cancel and the quit confirmation."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax

POSIX = os.name == "posix"


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def item(n, url="https://example.com/x"):
    wax.ytdlp_manager.begin_download()  # as /download does
    return wax.Download(n, url, "mp3_128", tempfile.gettempdir(), False)


def drain(download):
    lines = []
    while True:
        line = download.events.get(timeout=10)
        lines.append(line)
        if line.startswith("__DONE__:"):
            return lines


class QueueTests(unittest.TestCase):
    def tearDown(self):
        self.assertFalse(wax.ytdlp_manager.busy(), "a download was left counted as running")

    def test_runs_one_at_a_time_in_order(self):
        ran, running, overlap = [], [], []
        gate = threading.Event()

        def runner(d):
            running.append(d.id)
            if len(running) > 1:
                overlap.append(list(running))
            gate.wait(5)
            ran.append(d.id)
            running.remove(d.id)
            d.release()
            d.close("done")

        q = wax.DownloadQueue(runner)
        items = [item(n) for n in (11, 12, 13)]
        self.assertEqual([q.submit(i) for i in items], [0, 1, 2], "how many are ahead of each")
        time.sleep(0.1)
        self.assertEqual(q.order(), [11, 12, 13])
        self.assertEqual(q.pending(), 3)
        gate.set()
        for i in items:
            self.assertEqual(drain(i)[-1], "__DONE__:done")
        self.assertEqual(ran, [11, 12, 13])
        self.assertEqual(overlap, [])
        self.assertEqual(q.pending(), 0)

    def test_cancel_a_queued_one_skips_it(self):
        gate = threading.Event()
        ran = []

        def runner(d):
            gate.wait(5)
            ran.append(d.id)
            d.release()
            d.close("done")

        q = wax.DownloadQueue(runner)
        a, b, c = item(21), item(22), item(23)
        for i in (a, b, c):
            q.submit(i)
        time.sleep(0.1)
        self.assertTrue(q.cancel(22))
        self.assertFalse(q.cancel(22), "a second cancel is a no-op")
        self.assertFalse(q.cancel(999))
        self.assertEqual(drain(b), ["__DONE__:cancelled"])
        self.assertEqual(q.order(), [21, 23])
        gate.set()
        drain(a), drain(c)
        self.assertEqual(ran, [21, 23])

    def test_failing_runner_does_not_stop_the_queue(self):
        def runner(d):
            d.release()
            d.close("error")
            if d.id == 31:
                raise RuntimeError("boom")

        q = wax.DownloadQueue(runner)
        a, b = item(31), item(32)
        q.submit(a), q.submit(b)
        self.assertEqual(drain(b)[-1], "__DONE__:error")


@unittest.skipUnless(POSIX, "process groups")
class KillTreeTests(unittest.TestCase):
    def test_kills_the_child_and_its_children(self):
        # a parent with two grandchildren, like yt-dlp running ffmpeg
        proc = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60 & wait"], **wax.child_popen_kwargs())
        time.sleep(0.2)
        self.assertTrue(group_alive(proc.pid))
        wax.kill_tree(proc)
        self.assertIsNotNone(proc.poll())
        time.sleep(0.1)
        self.assertFalse(group_alive(proc.pid), "a grandchild outlived the cancel")

    def test_child_that_ignores_sigterm(self):
        proc = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 60 & wait"], **wax.child_popen_kwargs())
        time.sleep(0.2)
        start = time.time()
        wax.kill_tree(proc, grace=0.5)
        self.assertLess(time.time() - start, 3)
        time.sleep(0.1)
        self.assertFalse(group_alive(proc.pid))

    def test_finished_process_is_fine(self):
        proc = subprocess.Popen(["true"], **wax.child_popen_kwargs())
        proc.wait()
        wax.kill_tree(proc)
        wax.kill_tree(None)


# A stand-in for yt-dlp: says it downloads, leaves a .part file in the temp
# dir it was given, starts a grandchild (like ffmpeg) and keeps going.
FAKE_YTDLP = r'''#!/usr/bin/env python3
import os, subprocess, sys, time
args = sys.argv[1:]
temp = [a[5:] for a in args if a.startswith("temp:")][0]
out = args[args.index("-P") + 1]
url = args[-1]
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "log"), "a") as f:
    f.write("start " + url + "\n")
os.makedirs(temp, exist_ok=True)
open(os.path.join(temp, "Artist - Title.webm.part"), "w").write("x")
if "slow" in url:
    subprocess.Popen(["sleep", "60"])
    print("[download] Destination: " + os.path.join(temp, "Artist - Title.webm"), flush=True)
    for i in range(600):
        print("[download]  %.1f%% of 3.00MiB at 1.00MiB/s ETA 00:10" % (i / 10), flush=True)
        time.sleep(0.1)
os.remove(os.path.join(temp, "Artist - Title.webm.part"))
open(os.path.join(out, url.rsplit("/", 1)[-1] + ".mp3"), "w").write("x")
print("[ExtractAudio] Destination: x.mp3", flush=True)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "log"), "a") as f:
    f.write("end " + url + "\n")
'''


@unittest.skipUnless(POSIX, "uses a shebang script")
class CancelThroughServerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.out = os.path.join(self.dir, "out")
        os.makedirs(self.out)
        self.fake = os.path.join(self.dir, "yt-dlp")
        with open(self.fake, "w") as f:
            f.write(FAKE_YTDLP)
        os.chmod(self.fake, 0o700)
        patcher = patch.object(wax.ytdlp_manager, "current", return_value=[self.fake])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = wax.app.test_client()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def start(self, url):
        r = self.client.post("/download", json={"url": url, "format": "mp3_128", "output_path": self.out})
        return r.json["stream_id"]

    def stream(self, stream_id):
        body = self.client.get(f"/progress/{stream_id}").get_data(as_text=True)
        return [l[6:] for l in body.splitlines() if l.startswith("data: ") and l[6:].strip()]

    def log(self):
        try:
            with open(os.path.join(self.dir, "log")) as f:
                return f.read().split("\n")[:-1]
        except OSError:
            return []

    def test_cancel_mid_download(self):
        sid = self.start("https://example.com/slow")
        item = None
        for _ in range(100):
            item = wax.downloads._items.get(sid)
            if item is not None and item.proc is not None and os.path.isdir(item.temp_dir) and os.listdir(item.temp_dir):
                break
            time.sleep(0.05)
        pgid = item.proc.pid
        time.sleep(0.3)
        self.assertTrue(wax.ytdlp_manager.busy())
        self.assertEqual(self.client.post(f"/cancel/{sid}").json, {"cancelled": True})
        lines = self.stream(sid)
        self.assertEqual(lines[-1], "__DONE__:cancelled")
        events = [json.loads(l[8:]) for l in lines if l.startswith("__WAX__:")]
        self.assertFalse([e for e in events if e["type"] in ("error", "result")],
                         "a cancel is not an error")
        self.assertFalse(group_alive(pgid), "yt-dlp or its child kept running")
        self.assertFalse(os.path.exists(item.temp_dir), "partial files were left behind")
        self.assertEqual(os.listdir(self.out), [])
        self.assertFalse(wax.ytdlp_manager.busy(), "download counting is off after a cancel")
        self.assertEqual(self.client.post(f"/cancel/{sid}").json, {"cancelled": False})

    def test_queued_links_run_in_order_and_one_can_be_cancelled(self):
        a = self.start("https://example.com/slow")
        b = self.start("https://example.com/second")
        c = self.start("https://example.com/third")
        self.assertEqual(wax.downloads.order(), [a, b, c])
        self.assertEqual(self.client.post(f"/cancel/{b}").json, {"cancelled": True})
        time.sleep(0.3)
        self.assertEqual(self.client.post(f"/cancel/{a}").json, {"cancelled": True})
        self.assertEqual(self.stream(a)[-1], "__DONE__:cancelled")
        self.assertEqual(self.stream(b), ["__DONE__:cancelled"])
        self.assertEqual(self.stream(c)[-1], "__DONE__:done")
        self.assertEqual(self.log(), ["start https://example.com/slow", "start https://example.com/third",
                                      "end https://example.com/third"])
        self.assertEqual(os.listdir(self.out), ["third.mp3"])
        self.assertFalse(wax.ytdlp_manager.busy())

    def test_two_links_finish_in_order(self):
        a = self.start("https://example.com/one")
        b = self.start("https://example.com/two")
        self.assertEqual(self.stream(a)[-1], "__DONE__:done")
        self.assertEqual(self.stream(b)[-1], "__DONE__:done")
        self.assertEqual(self.log(), ["start https://example.com/one", "end https://example.com/one",
                                      "start https://example.com/two", "end https://example.com/two"])


class QuitConfirmationTests(unittest.TestCase):
    def test_decision(self):
        ask = wax.quit_needs_confirmation
        self.assertFalse(ask(0))
        self.assertTrue(ask(1))
        self.assertTrue(ask(3))
        self.assertFalse(ask(1, confirmed=True), "asked once, not again on the second close")
        self.assertFalse(ask(1, can_ask=False), "no window to ask in")

    def test_copy_is_lowercase(self):
        for lang in ("en", "ru"):
            for key in (wax.QUIT_TITLE, wax.QUIT_MESSAGE):
                text = wax.tr(key, lang)
                self.assertEqual(text, text.lower())

    def api(self, answer):
        api = wax.Api()
        api.window = Mock()
        asked = threading.Event()

        def dialog(title, message):
            asked.set()
            return answer

        api.window.create_confirmation_dialog.side_effect = dialog
        return api, asked

    def test_idle_quit_does_not_ask(self):
        api, asked = self.api(True)
        with patch.object(wax.downloads, "pending", return_value=0), \
                patch.object(wax.downloads, "cancel_all"):
            api.quit()
            self.assertIsNone(api.on_closing(), "nothing running: let it close")
        time.sleep(0.3)
        self.assertFalse(asked.is_set())
        api.window.destroy.assert_called_once()

    def test_declined_quit_keeps_running(self):
        api, asked = self.api(False)
        with patch.object(wax.downloads, "pending", return_value=1), \
                patch.object(wax.downloads, "cancel_all") as cancel_all:
            self.assertIs(api.on_closing(), False, "the close must wait for the answer")
            self.assertTrue(asked.wait(2))
            time.sleep(0.3)
        cancel_all.assert_not_called()
        api.window.destroy.assert_not_called()
        api.window.create_confirmation_dialog.assert_called_once_with("quit wax?", wax.tr(wax.QUIT_MESSAGE))

    def test_confirmed_quit_stops_and_closes(self):
        api, asked = self.api(True)
        with patch.object(wax.downloads, "pending", return_value=2), \
                patch.object(wax.downloads, "cancel_all") as cancel_all:
            api.quit()  # returns at once: the dialog is on its own thread
            self.assertTrue(asked.wait(2))
            time.sleep(0.3)
            cancel_all.assert_called()
            api.window.destroy.assert_called_once()
            # destroy() closes the window again; that close goes through
            self.assertIsNone(api.on_closing())

    def test_one_dialog_at_a_time(self):
        api = wax.Api()
        api.window = Mock()
        release = threading.Event()
        api.window.create_confirmation_dialog.side_effect = lambda t, m: release.wait(2) and False
        with patch.object(wax.downloads, "pending", return_value=1):
            api.quit()
            api.quit()
            api.on_closing()
            time.sleep(0.2)
            release.set()
        self.assertEqual(api.window.create_confirmation_dialog.call_count, 1)

    def test_quit_never_blocks_the_caller(self):
        api = wax.Api()
        api.window = Mock()
        api.window.create_confirmation_dialog.side_effect = lambda t, m: time.sleep(1) or False
        with patch.object(wax.downloads, "pending", return_value=1):
            start = time.time()
            api.quit()
            self.assertLess(time.time() - start, 0.2)


if __name__ == "__main__":
    unittest.main()
