"""yt-dlp output -> UI events, friendly errors, and file naming."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# Trimmed from a real `yt-dlp` run on a 3-track Bandcamp album (2026.10.26).
ALBUM_LINES = """\
[Bandcamp:album] Playlist "Entropy" EP: Downloading 3 items of 3
[download] Downloading item 1 of 3
[info] Writing video thumbnail 0 to: /tmp/wax-digger-temp/A - E/01 Entropy.jpg
[ThumbnailsConvertor] Not converting thumbnail "/tmp/x.jpg"; already is in target format jpg
[download] Destination: /tmp/wax-digger-temp/A - E/01 Entropy.webm
[download]   0.0% of    2.74MiB at  Unknown B/s ETA Unknown
[download]  50.0% of    2.74MiB at  1.00MiB/s ETA 00:01
[download] 100% of    2.74MiB in 00:00:00 at 3.98MiB/s
[ExtractAudio] Destination: /tmp/wax-digger-temp/A - E/01 Entropy.mp3
[Metadata] Adding metadata to "/tmp/wax-digger-temp/A - E/01 Entropy.mp3"
[EmbedThumbnail] ffmpeg: Adding thumbnail to "/tmp/wax-digger-temp/A - E/01 Entropy.mp3"
[download] Downloading item 2 of 3
ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video
[download] Downloading item 3 of 3
[download] Destination: /tmp/wax-digger-temp/A - E/03 Remix.webm
[download]  10.0% of    4.75MiB at  1.00MiB/s ETA 00:04
[download] 100% of    4.75MiB in 00:00:00 at 7.58MiB/s
[ExtractAudio] Destination: /tmp/wax-digger-temp/A - E/03 Remix.mp3
[download] Finished downloading playlist: "Entropy" EP
""".splitlines()


def feed_all(tracker, lines):
    events = []
    for line in lines:
        events += tracker.feed(line)
    return events


class ProgressTests(unittest.TestCase):
    def test_album_progress_is_overall_and_never_goes_back(self):
        t = wax.YtdlpProgress()
        events = feed_all(t, ALBUM_LINES)
        percents = [e["percent"] for e in events if e["type"] == "progress"]
        self.assertEqual(percents, sorted(percents), "the ring must not run backwards")
        # the first file finishing is a third of the album, not 100%
        first = [e for e in events if e["message"] == "converting 1 of 3"]
        self.assertTrue(first and first[0]["percent"] <= 100 / 3)
        self.assertIn("track 3 of 3", [e["message"] for e in events])
        self.assertFalse([e for e in events if e["type"] == "error"],
                         "one dead track must not fail the whole album")
        # the cover is converted before the audio download; that is not "converting"
        self.assertNotEqual(events[0]["message"], "converting 1 of 3")

    def test_partial_album_result(self):
        t = wax.YtdlpProgress()
        feed_all(t, ALBUM_LINES)
        events, ok = t.finish(1)
        self.assertFalse(ok)
        self.assertEqual(events[-1], {"type": "result", "saved": 2, "failed": 1, "total": 3})
        self.assertFalse([e for e in events if e["type"] == "error"])

    def test_whole_album_saved(self):
        t = wax.YtdlpProgress()
        feed_all(t, [l for l in ALBUM_LINES if not l.startswith("ERROR")])
        events, ok = t.finish(0)
        self.assertTrue(ok)
        self.assertEqual(events, [{"type": "result", "saved": 3, "failed": 0, "total": 3}])

    def test_stopped_midway_counts_only_files_on_disk(self):
        t = wax.YtdlpProgress()
        feed_all(t, ALBUM_LINES[:16])   # killed while track 3 downloads
        events, ok = t.finish(-15)
        self.assertFalse(ok)
        self.assertEqual(events[-1]["saved"], 1)

    def test_single_track(self):
        t = wax.YtdlpProgress()
        events = feed_all(t, [
            "[download] Destination: /tmp/x.webm",
            "[download]  42.0% of 3.00MiB at 1.00MiB/s ETA 00:02",
            "[download] 100% of 3.00MiB in 00:00:01 at 3.00MiB/s",
            "[ExtractAudio] Destination: /tmp/x.mp3",
        ])
        self.assertEqual([e["message"] for e in events],
                         ["downloading…", "downloading…", "converting…"])
        self.assertAlmostEqual(events[0]["percent"], 42 * wax.DOWNLOAD_SHARE, places=1)
        self.assertEqual(t.finish(0), ([{"type": "result", "saved": 1, "failed": 0, "total": 1}], True))

    def test_single_track_error_is_reported_once(self):
        t = wax.YtdlpProgress()
        events = t.feed("ERROR: [youtube] xyz: Sign in to confirm your age. This video may be inappropriate for some users.")
        self.assertEqual(events, [{"type": "error", "message": "this one is age-restricted"}])
        final, ok = t.finish(1)
        self.assertFalse(ok)
        self.assertEqual([e["type"] for e in final], ["result"])

    def test_failure_without_error_line_still_explains(self):
        final, ok = wax.YtdlpProgress().finish(2)
        self.assertFalse(ok)
        self.assertEqual(final[0], {"type": "error", "message": wax.GENERIC_ERROR})

    def test_already_downloaded_counts_as_saved(self):
        t = wax.YtdlpProgress()
        t.feed("[download] /music/A - B.mp3 has already been downloaded")
        self.assertEqual(t.finish(1)[0][-1]["saved"], 1)


class FriendlyErrorTests(unittest.TestCase):
    CASES = {
        "ERROR: [youtube] xyz: Private video. Sign in if you've been granted access to this video": "this one is private",
        "ERROR: [youtube] xyz: Video unavailable. This video has been removed by the uploader": "this one is gone or unavailable",
        "ERROR: [youtube] aaaaaaaaaaa: This video is unavailable": "this one is gone or unavailable",
        "ERROR: [youtube] xyz: Sign in to confirm your age. This video may be inappropriate for some users.": "this one is age-restricted",
        "ERROR: [youtube] xyz: Sign in to confirm you’re not a bot. Use --cookies-from-browser": "youtube wants a sign-in, try again later",
        "ERROR: [youtube] xyz: The uploader has not made this video available in your country": "not available in your country",
        "ERROR: Unsupported URL: https://example.com/": "this link isn't supported",
        "ERROR: [generic] x: Unable to download webpage: <urlopen error [Errno 8] nodename nor servname provided, or not known>": "no connection, check your internet",
        "ERROR: [youtube] xyz: Unable to download API page: HTTP Error 429: Too Many Requests": "too many requests, try again later",
        "ERROR: [youtube] xyz: This video is DRM protected": "this one is drm-protected",
        "ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install or provide the path using --ffmpeg-location": "ffmpeg is missing",
        "ERROR: [generic] nothing: Unable to download webpage: HTTP Error 404: Not Found": "this one is gone or unavailable",
        "ERROR: something nobody has seen before": wax.GENERIC_ERROR,
        "": wax.GENERIC_ERROR,
    }

    def test_cases(self):
        for line, expected in self.CASES.items():
            self.assertEqual(wax.friendly_error(line), expected, line)

    def test_messages_are_lowercase(self):
        for _, message in wax.ERROR_MESSAGES:
            self.assertEqual(message, message.lower())


class GofiEventTests(unittest.TestCase):
    def test_copy_matches_the_ytdlp_path(self):
        self.assertEqual(wax.gofi_event('{"type":"progress","message":"Downloading 2 of 9","percent":11.1}'),
                         {"type": "progress", "message": "track 2 of 9", "percent": 11.1})
        self.assertEqual(wax.gofi_event('{"type":"status","message":"Connecting to Deezer…"}')["message"],
                         "connecting to deezer…")
        self.assertEqual(wax.gofi_event('{"type":"result","saved":1,"failed":0,"total":1}')["saved"], 1)
        self.assertIsNone(wax.gofi_event("not json"))


@unittest.skipIf(os.name == "nt", "test helper uses a Unix shebang")
class StreamTests(unittest.TestCase):
    def test_download_stream_carries_only_wax_events(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = os.path.join(directory, "yt-dlp")
            with open(fake, "w") as f:
                f.write("#!/usr/bin/env python3\nimport sys\n"
                        f"print({chr(10).join(ALBUM_LINES)!r})\n"
                        "sys.exit(1)\n")
            os.chmod(fake, 0o700)
            client = wax.app.test_client()
            with patch.object(wax.ytdlp_manager, "current", return_value=[fake]):
                r = client.post("/download", json={"url": "https://x.bandcamp.com/album/e",
                                                   "format": "mp3_128", "output_path": directory})
                stream = client.get("/progress/%d" % r.json["stream_id"]).get_data(as_text=True)
        lines = [l[6:] for l in stream.splitlines() if l.startswith("data: ")]
        self.assertEqual(lines[-1], "__DONE__:error")
        self.assertTrue(all(l.startswith("__WAX__:") for l in lines[:-1]), lines)
        events = [json.loads(l[8:]) for l in lines[:-1]]
        self.assertEqual(events[-1], {"type": "result", "saved": 2, "failed": 1, "total": 3})


@unittest.skipUnless(yt_dlp, "yt_dlp module not importable; naming checked against real links instead")
class NamingTests(unittest.TestCase):
    """Run the real --parse-metadata rules and -o template inside yt-dlp."""

    def name(self, info):
        cmd = wax.build_ytdlp_command("u", "mp3_320", "/out", ytdlp=[])
        opts = yt_dlp.parse_options(cmd[:-2] + ["--quiet"]).ydl_opts
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.run_all_pps("pre_process", {"id": "x", "ext": "mp3", **info})
            return os.path.normpath(ydl.prepare_filename(info)).replace(os.sep, "/"), info

    def test_single_track(self):
        self.assertEqual(self.name({"title": "Some Song", "uploader": "Chan"})[0], "/out/Chan - Some Song.mp3")

    def test_title_already_has_the_artist(self):
        path, info = self.name({"title": "Rick Astley - Never Gonna Give You Up", "uploader": "Rick Astley"})
        self.assertEqual(path, "/out/Rick Astley - Never Gonna Give You Up.mp3")
        self.assertEqual(info["meta_title"], "Never Gonna Give You Up")

    def test_album(self):
        path, _ = self.name({"title": "X - Entropy", "track": "Entropy", "artist": "X", "album": "E",
                             "album_artist": "X", "track_number": 3, "playlist_index": 3, "playlist_title": "E"})
        self.assertEqual(path, "/out/X - E/03 Entropy.mp3")

    def test_playlist(self):
        path, _ = self.name({"title": "Song", "uploader": "U", "playlist_index": 7, "playlist_title": "My Mix"})
        self.assertEqual(path, "/out/My Mix/07 U - Song.mp3")

    def test_playlist_title_cannot_climb_out(self):
        path, _ = self.name({"title": "Song", "uploader": "U", "playlist_index": 7, "playlist_title": ".."})
        self.assertEqual(path, "/out/07 U - Song.mp3")


if __name__ == "__main__":
    unittest.main()
