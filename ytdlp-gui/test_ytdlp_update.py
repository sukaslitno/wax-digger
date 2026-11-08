"""The user copy of yt-dlp: picking a binary, throttling, updating safely."""
import json
import os
import platform
import shutil
import sys
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax
import ytdlp_update as yu

DAY = 24 * 3600


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def fake_version(cmd):
    """Fake binaries are text files holding their --version output."""
    try:
        with open(cmd[0]) as f:
            return yu.parse_version(f.read())
    except OSError:
        return None


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(yu.parse_version("2026.10.26\n"), (2026, 10, 26, 0))
        self.assertEqual(yu.parse_version("2026.01.05.183202"), (2026, 1, 5, 183202))
        for junk in ("", None, "Usage: yt-dlp", "1.2", "stable@2026.10.26"):
            self.assertIsNone(yu.parse_version(junk), junk)

    def test_compare(self):
        self.assertTrue(yu.is_newer((2026, 10, 26, 0), (2025, 9, 5, 0)))
        self.assertTrue(yu.is_newer((2025, 10, 1, 0), (2025, 9, 30, 0)))   # numeric, not text
        self.assertTrue(yu.is_newer((2026, 1, 5, 1), (2026, 1, 5, 0)))     # nightly build
        self.assertFalse(yu.is_newer((2026, 1, 5, 0), (2026, 1, 5, 0)))
        self.assertTrue(yu.is_newer((2025, 1, 1, 0), None))                # anything beats broken
        self.assertFalse(yu.is_newer(None, (2025, 1, 1, 0)))

    def test_format(self):
        self.assertEqual(yu.format_version((2026, 1, 5, 0)), "2026.01.05")
        self.assertEqual(yu.format_version((2026, 1, 5, 7)), "2026.01.05.7")
        self.assertIsNone(yu.format_version(None))


class ManagerTestCase(unittest.TestCase):
    """A data dir, a bundled binary and a config in a temp dir; no real processes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = patch.dict(os.environ, {"WAX_DIGGER_DATA_DIR": os.path.join(self.tmp, "data")})
        env.start()
        self.addCleanup(env.stop)
        self.bundled = os.path.join(self.tmp, "bundle", "bin", "yt-dlp")
        write(self.bundled, "2026.01.01")
        self.user = yu.user_binary()
        self.config = {}
        self.clock = 1_000_000_000.0
        self.update_calls = []
        self.latest = "2026.10.26"
        for name, fake in (("run_version", fake_version), ("run_self_update", self.fake_update)):
            p = patch.object(yu, name, side_effect=fake)
            p.start()
            self.addCleanup(p.stop)
        self.m = yu.YtdlpManager(
            base=lambda: [self.bundled], bundled=lambda: self.bundled,
            load_config=lambda: dict(self.config), save_config=self.save, now=lambda: self.clock)

    def save(self, data):
        self.config = dict(data)

    def fake_update(self, path):
        self.update_calls.append(path)
        self.assertNotEqual(path, self.user, "the updater must never run on the live copy")
        if self.latest is None:
            return False
        write(path, self.latest)
        return True

    def read(self, path):
        with open(path) as f:
            return f.read()


class SelectionTests(ManagerTestCase):
    def test_bundled_without_user_copy(self):
        self.assertEqual(self.m.current(), [self.bundled])
        self.assertEqual(self.m.known_version(), "2026.01.01")

    def test_newer_user_copy_wins(self):
        write(self.user, "2026.10.26")
        self.assertEqual(self.m.current(), [self.user])

    def test_newer_bundled_wins_over_stale_user_copy(self):
        write(self.user, "2025.09.05")
        self.assertEqual(self.m.current(), [self.bundled])

    def test_tie_goes_to_bundled(self):
        write(self.user, "2026.01.01")
        self.assertEqual(self.m.current(), [self.bundled])

    def test_broken_user_copy_falls_back(self):
        write(self.user, "Segmentation fault")
        self.assertEqual(self.m.current(), [self.bundled])

    def test_user_copy_when_bundled_is_broken(self):
        write(self.bundled, "garbage")
        write(self.user, "2025.09.05")
        self.assertEqual(self.m.current(), [self.user])

    def test_version_unknown_until_resolved(self):
        self.assertIsNone(self.m.known_version())  # /status never runs a binary

    def test_status_reports_the_version(self):
        with patch.object(wax.ytdlp_manager, "known_version", return_value="2026.10.26"):
            r = wax.app.test_client().get("/status")
        self.assertEqual(r.json["ytdlp_version"], "2026.10.26")


class UpdateTests(ManagerTestCase):
    def test_seeds_from_bundled_and_updates(self):
        self.m.current()
        self.assertEqual(self.m.check(), "updated")
        self.assertEqual(self.read(self.user), "2026.10.26")
        self.assertEqual(self.m.current(), [self.user])          # no restart needed
        self.assertEqual(self.m.known_version(), "2026.10.26")
        self.assertEqual(self.config[yu.CONFIG_KEY], self.clock)
        self.assertEqual(sorted(os.listdir(yu.bin_dir())), ["yt-dlp"])  # staging cleaned up
        if os.name != "nt":
            self.assertTrue(os.access(self.user, os.X_OK))

    def test_already_current(self):
        write(self.user, "2026.10.26")
        self.m.current()
        self.assertEqual(self.m.check(), "current")
        self.assertEqual(self.m.current(), [self.user])
        self.assertIn(yu.CONFIG_KEY, self.config)

    def test_stale_user_copy_is_reseeded_from_newer_bundled(self):
        write(self.user, "2025.09.05")
        self.assertEqual(self.m.current(), [self.bundled])
        self.latest = None  # offline: the updater fails ...
        self.assertEqual(self.m.check(), "failed")
        self.assertEqual(self.read(self.user), "2025.09.05")  # ... and nothing changes
        self.clock += yu.RETRY_INTERVAL
        self.latest = "2026.01.01"  # the bundled one is already the latest
        self.assertEqual(self.m.check(), "current")
        self.assertEqual(self.read(self.user), "2026.01.01")

    def test_broken_update_keeps_previous_copy(self):
        write(self.user, "2026.05.01")
        self.latest = "Traceback (most recent call last)"
        self.assertEqual(self.m.check(), "failed")
        self.assertEqual(self.read(self.user), "2026.05.01")
        self.assertEqual(self.m.current(), [self.user])
        self.assertNotIn(yu.CONFIG_KEY, self.config)

    def test_failed_updater_keeps_previous_copy(self):
        write(self.user, "2026.05.01")
        self.latest = None
        self.assertEqual(self.m.check(), "failed")
        self.assertEqual(self.read(self.user), "2026.05.01")

    def test_nothing_to_seed_from(self):
        self.m.bundled = lambda: None  # source checkout, system yt-dlp
        self.assertEqual(self.m.check(), "skipped")
        self.assertEqual(self.update_calls, [])
        self.assertFalse(os.path.exists(self.user))


class ThrottleTests(ManagerTestCase):
    def test_once_a_day(self):
        self.config[yu.CONFIG_KEY] = self.clock - DAY + 60
        self.assertEqual(self.m.check(), "skipped")
        self.assertEqual(self.update_calls, [])
        self.clock += 60
        self.assertEqual(self.m.check(), "updated")
        self.clock += 3600
        self.assertEqual(self.m.check(), "skipped")
        self.assertEqual(len(self.update_calls), 1)

    def test_failure_retries_after_an_hour_not_every_tick(self):
        self.latest = None
        self.assertEqual(self.m.check(), "failed")
        self.clock += yu.TICK
        self.assertEqual(self.m.check(), "skipped")
        self.clock += yu.RETRY_INTERVAL
        self.assertEqual(self.m.check(), "failed")
        self.assertEqual(len(self.update_calls), 2)

    def test_forced_check_ignores_the_day_but_is_rate_limited(self):
        self.config[yu.CONFIG_KEY] = self.clock - 60
        self.assertTrue(self.m.force_due())
        self.assertEqual(self.m.check(force=True), "updated")
        self.assertFalse(self.m.force_due())
        self.assertEqual(self.m.check(force=True), "skipped")
        self.clock += yu.FORCED_INTERVAL
        self.assertEqual(self.m.check(force=True), "current")
        self.assertEqual(len(self.update_calls), 2)

    def test_garbage_timestamp_in_config(self):
        self.config[yu.CONFIG_KEY] = "yesterday"
        self.assertEqual(self.m.check(), "updated")


class BusyTests(ManagerTestCase):
    def test_no_check_during_a_download(self):
        self.m.begin_download()
        self.assertEqual(self.m.check(), "skipped")
        self.assertEqual(self.m.check(force=True), "skipped")
        self.assertFalse(self.m.force_due())
        self.assertEqual(self.update_calls, [])
        self.assertNotIn(yu.CONFIG_KEY, self.config)  # still due once it ends
        self.m.end_download()
        self.assertEqual(self.m.check(), "updated")

    def test_download_starting_mid_update_blocks_the_swap(self):
        write(self.user, "2026.05.01")
        self.m.current()
        original = self.fake_update

        def update_then_download_starts(path):
            self.m.begin_download()
            return original(path)

        yu.run_self_update.side_effect = update_then_download_starts
        self.assertEqual(self.m.check(), "failed")
        self.assertEqual(self.read(self.user), "2026.05.01")
        self.assertEqual(self.m.current(), [self.user])
        self.assertEqual(sorted(os.listdir(yu.bin_dir())), ["yt-dlp"])

    def test_a_download_never_waits_for_a_running_check(self):
        self.m.current()
        with self.m._update_lock:  # a check is in progress
            self.m.begin_download()
            self.assertEqual(self.m.current(), [self.bundled])
            self.m.end_download()
            self.assertEqual(self.m.check(), "skipped")  # no second, parallel check


class OutdatedErrorTests(unittest.TestCase):
    def test_classifier(self):
        stale = [
            "ERROR: [youtube] abc: Unable to extract uploader id; please report this issue on "
            "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue "
            "template. Confirm you are on the latest version using  yt-dlp -U",
            "ERROR: [youtube] abc: Signature extraction failed: Some formats may be missing",
            "ERROR: [youtube] abc: Requested format is not available. Use --list-formats",
            "ERROR: unable to download video data: HTTP Error 403: Forbidden",
        ]
        for line in stale:
            self.assertTrue(wax.extractor_outdated(line), line)
        not_stale = [
            "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser",
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
            "ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader",
            "ERROR: [youtube] abc: The uploader has not made this video available in your country; "
            "please report this issue",
            "ERROR: [generic] Unable to download webpage: <urlopen error [Errno 8] nodename nor "
            "servname provided>",
            "ERROR: Unsupported URL: https://example.com/",
            "ERROR: [youtube] abc: HTTP Error 429: Too Many Requests",
        ]
        for line in not_stale:
            self.assertFalse(wax.extractor_outdated(line), line)


@unittest.skipIf(os.name == "nt", "test helper uses a Unix shebang")
class FailedDownloadTests(unittest.TestCase):
    def stream(self, error_line, result):
        with tempfile.TemporaryDirectory() as directory:
            fake = os.path.join(directory, "yt-dlp")
            write(fake, "#!/usr/bin/env python3\nimport sys\n"
                        f"print({error_line!r})\nsys.exit(1)\n")
            os.chmod(fake, 0o700)
            m = wax.ytdlp_manager
            with patch.object(m, "current", return_value=[fake]), \
                    patch.object(m, "force_due", side_effect=lambda: not m.busy()), \
                    patch.object(m, "check", return_value=result) as check:
                r = wax.app.test_client().post("/download", json={
                    "url": "https://www.youtube.com/watch?v=abc", "format": "mp3_128",
                    "output_path": directory})
                body = wax.app.test_client().get("/progress/%d" % r.json["stream_id"]).get_data(as_text=True)
        lines = [l[6:] for l in body.splitlines() if l.startswith("data: ")]
        events = [json.loads(l[8:]) for l in lines if l.startswith("__WAX__:")]
        self.assertEqual(lines[-1], "__DONE__:error")
        self.assertFalse(m.busy(), "the download was not counted as finished")
        return events, check

    def test_stale_extractor_updates_and_says_try_again(self):
        events, check = self.stream("ERROR: [youtube] abc: Unable to extract nsig function", "updated")
        check.assert_called_once_with(force=True)
        self.assertIn({"type": "status", "message": "updating yt-dlp…"}, events)
        errors = [e["message"] for e in events if e["type"] == "error"]
        self.assertEqual(errors[-1], "updated yt-dlp — try again")
        self.assertEqual(events[-1]["type"], "result")

    def test_no_newer_version_keeps_the_real_error(self):
        events, check = self.stream("ERROR: [youtube] abc: Unable to extract nsig function", "current")
        check.assert_called_once_with(force=True)
        errors = [e["message"] for e in events if e["type"] == "error"]
        self.assertEqual(errors[-1], wax.tr(wax.GENERIC_ERROR))

    def test_bot_check_does_not_update(self):
        events, check = self.stream("ERROR: [youtube] abc: Sign in to confirm you're not a bot", "updated")
        check.assert_not_called()
        self.assertNotIn("updating yt-dlp…", [e.get("message") for e in events])

    def test_ui_text_is_lowercase(self):
        for lang in ("en", "ru"):
            text = wax.tr(wax.UPDATED_MESSAGE, lang)
            self.assertEqual(text, text.lower())


# Real network, real binaries: WAX_REAL_UPDATE_TEST=1 python -m pytest test_ytdlp_update.py
OLD_RELEASE = "2025.09.05"


@unittest.skipUnless(os.environ.get("WAX_REAL_UPDATE_TEST") == "1", "set WAX_REAL_UPDATE_TEST=1 (network)")
@unittest.skipUnless(sys.platform.startswith("linux") and platform.machine() == "x86_64", "uses yt-dlp_linux")
class RealUpdateTest(unittest.TestCase):
    def test_old_release_updates_itself(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        old = os.path.join(tmp, "bundle", "yt-dlp")
        os.makedirs(os.path.dirname(old))
        url = f"https://github.com/yt-dlp/yt-dlp/releases/download/{OLD_RELEASE}/yt-dlp_linux"
        with urllib.request.urlopen(url, timeout=120) as r, open(old, "wb") as f:
            shutil.copyfileobj(r, f)
        yu.make_executable(old)
        config = {}
        with patch.dict(os.environ, {"WAX_DIGGER_DATA_DIR": os.path.join(tmp, "data")}):
            m = yu.YtdlpManager(base=lambda: [old], bundled=lambda: old,
                                load_config=lambda: dict(config), save_config=config.update)
            self.assertEqual(m.current(), [old])
            self.assertEqual(m.known_version(), OLD_RELEASE)
            self.assertEqual(m.check(), "updated")
            self.assertEqual(m.current(), [yu.user_binary()])
            self.assertTrue(yu.is_newer(yu.run_version([yu.user_binary()]), yu.parse_version(OLD_RELEASE)))
            print("\nupdated", OLD_RELEASE, "->", m.known_version())
            self.assertEqual(m.check(force=True), "current")


if __name__ == "__main__":
    unittest.main()
