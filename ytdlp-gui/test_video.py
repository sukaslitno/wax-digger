"""YouTube links saved as video: format choice, naming, progress, validation."""
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

T = "/tmp/wax-digger-temp/run-1"
# Trimmed from a real run of build_ytdlp_command(<youtube link>, "video_1080", ...)
# with yt-dlp 2026.10.26 (the video only has 240p, so 1080p falls back to it).
VIDEO_LINES = f"""\
[youtube] jNQXAC9IVRw: Downloading webpage
[info] jNQXAC9IVRw: Downloading 1 format(s): 133+140
[info] Writing video thumbnail 37 to: {T}/./jawed - Me at the zoo [240p].webp
[ThumbnailsConvertor] Converting thumbnail "{T}/./jawed - Me at the zoo [240p].webp" to jpg
[download] Destination: {T}/./jawed - Me at the zoo [240p].f133.mp4
[download]   0.2% of  422.93KiB at  Unknown B/s ETA Unknown
[download]  60.0% of  422.93KiB at    8.68MiB/s ETA 00:00
[download] 100.0% of  422.93KiB at    8.68MiB/s ETA 00:00
[download] 100% of  422.93KiB in 00:00:00 at 5.80MiB/s
[download] Destination: {T}/./jawed - Me at the zoo [240p].f140.m4a
[download]  42.0% of  302.04KiB at   14.10MiB/s ETA 00:00
[download] 100% of  302.04KiB in 00:00:00 at 5.44MiB/s
[Merger] Merging formats into "{T}/./jawed - Me at the zoo [240p].mp4"
Deleting original file {T}/./jawed - Me at the zoo [240p].f140.m4a (pass -k to keep)
Deleting original file {T}/./jawed - Me at the zoo [240p].f133.mp4 (pass -k to keep)
[Metadata] Adding metadata to "{T}/./jawed - Me at the zoo [240p].mp4"
[EmbedThumbnail] mutagen: Adding thumbnail to "{T}/./jawed - Me at the zoo [240p].mp4"
[MoveFiles] Moving file "{T}/./jawed - Me at the zoo [240p].mp4" to "/out/jawed - Me at the zoo [240p].mp4"
""".splitlines()


def feed_all(tracker, lines):
    events = []
    for line in lines:
        events += tracker.feed(line)
    return events


class FormatTests(unittest.TestCase):
    def test_qualities_low_to_high(self):
        self.assertEqual(list(wax.VIDEO_FORMATS), ["video_360", "video_480", "video_720",
                                                   "video_1080", "video_1440", "video_2160"])

    def test_selector_per_quality(self):
        # up to 1080p H.264 + AAC (plays in QuickTime); above it YouTube has no H.264
        expected = {
            "video_360": "res:360,vcodec:h264,acodec:aac",
            "video_480": "res:480,vcodec:h264,acodec:aac",
            "video_720": "res:720,vcodec:h264,acodec:aac",
            "video_1080": "res:1080,vcodec:h264,acodec:aac",
            "video_1440": "res:1440,acodec:aac",
            "video_2160": "res:2160,acodec:aac",
        }
        for fmt, sort in expected.items():
            self.assertEqual(wax.FORMATS[fmt], ["-f", "bv*+ba/b", "-S", sort, "--merge-output-format", "mp4"])

    def test_video_command(self):
        cmd = wax.build_ytdlp_command("https://youtu.be/x", "video_720", "/out", ytdlp=["yt-dlp"])
        self.assertNotIn("-x", cmd)
        self.assertNotIn("bestaudio/best", cmd)
        self.assertEqual(cmd.count("-f"), 1)
        for flag in ("--no-playlist", "--embed-metadata", "--embed-thumbnail", "--ignore-config"):
            self.assertIn(flag, cmd)
        self.assertNotIn(wax.SQUARE_COVER, cmd, "a video keeps its 16:9 thumbnail")
        for rule in wax.VIDEO_RULES:
            self.assertIn(rule, cmd)
        self.assertEqual(cmd[-2:], ["--", "https://youtu.be/x"])

    def test_audio_commands_are_unchanged(self):
        for fmt in ("mp3_128", "mp3_320", "flac", "wav", "aac"):
            cmd = wax.build_ytdlp_command("https://youtu.be/x", fmt, "/out", ytdlp=["yt-dlp"])
            select = "bestaudio[acodec^=mp4a]/bestaudio/best" if fmt == "aac" else "bestaudio/best"
            self.assertEqual(cmd[1:1 + len(wax.FORMATS[fmt]) + 2], wax.FORMATS[fmt] + ["-f", select])
            for absent in ("-S", "--merge-output-format", *wax.VIDEO_RULES):
                self.assertNotIn(absent, cmd)

    def test_youtube_links(self):
        for url in ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "youtube.com/watch?v=x",
                    "https://m.youtube.com/watch?v=x", "https://youtu.be/x",
                    "https://www.youtube.com/shorts/x", "https://www.youtube.com/playlist?list=x"):
            self.assertTrue(wax.youtube_video_link(url), url)
        for url in ("https://music.youtube.com/watch?v=x", "https://open.spotify.com/track/x",
                    "https://x.bandcamp.com/track/y", "https://youtube.com.evil.example/x",
                    "ftp://youtube.com/x", ""):
            self.assertFalse(wax.youtube_video_link(url), url)


class ProgressTests(unittest.TestCase):
    def test_two_streams_then_merge(self):
        t = wax.YtdlpProgress("/out", T, video=True)
        events = feed_all(t, VIDEO_LINES)
        percents = [e["percent"] for e in events]
        self.assertEqual(percents, sorted(percents), "the ring must not run backwards")
        messages = [e["message"] for e in events]
        self.assertEqual(list(dict.fromkeys(messages)),
                         ["downloading video…", "downloading audio…", "merging…"])
        # the video stream is the bigger part of the ring
        first_audio = next(e for e in events if e["message"] == "downloading audio…")
        self.assertEqual(first_audio["percent"], 75.0)
        self.assertEqual(events[-1]["percent"], 100 * wax.DOWNLOAD_SHARE)
        final, ok = t.finish(0)
        self.assertTrue(ok)
        # "show in folder" reveals the merged file, never a stream before the merge
        self.assertEqual(final, [{"type": "result", "saved": 1, "failed": 0, "total": 1,
                                  "paths": ["/out/jawed - Me at the zoo [240p].mp4"], "folder": "/out"}])

    def test_one_stream_without_merge(self):
        t = wax.YtdlpProgress("/out", T, video=True)
        events = feed_all(t, [
            "[info] x: Downloading 1 format(s): 18",
            f"[download] Destination: {T}/A - B [360p].mp4",
            "[download]  50.0% of 1.00MiB at 1.00MiB/s ETA 00:01",
            f'[Metadata] Adding metadata to "{T}/A - B [360p].mp4"',
            f'[MoveFiles] Moving file "{T}/A - B [360p].mp4" to "/out/A - B [360p].mp4"',
        ])
        self.assertEqual(events[1], {"type": "progress", "percent": 45.0, "message": "downloading video…"})
        self.assertEqual(t.finish(0)[0][-1]["paths"], ["/out/A - B [360p].mp4"])

    def test_killed_while_merging_saves_nothing(self):
        t = wax.YtdlpProgress("/out", T, video=True)
        feed_all(t, VIDEO_LINES[:13])  # up to "[Merger] Merging formats into"
        final, ok = t.finish(-15)
        self.assertFalse(ok)
        self.assertEqual(final[-1]["saved"], 0)

    def test_playlist_of_videos(self):
        t = wax.YtdlpProgress(video=True)
        lines = ["[download] Downloading item 1 of 2"] + VIDEO_LINES[1:] + \
                ["[download] Downloading item 2 of 2"] + VIDEO_LINES[1:]
        events = feed_all(t, lines)
        percents = [e["percent"] for e in events]
        self.assertEqual(percents, sorted(percents))
        self.assertIn("track 2 of 2", [e["message"] for e in events])
        self.assertLessEqual(max(e["percent"] for e in events if e["message"] == "converting 1 of 2"), 50)
        self.assertEqual(t.finish(0), ([{"type": "result", "saved": 2, "failed": 0, "total": 2}], True))

    def test_audio_tracker_ignores_video_files(self):
        t = wax.YtdlpProgress("/out", T)
        feed_all(t, VIDEO_LINES)
        self.assertNotIn("paths", t.finish(0)[0][-1])


@unittest.skipUnless(yt_dlp, "yt_dlp module not importable")
class NamingTests(unittest.TestCase):
    """The real -o template and --parse-metadata rules, run inside yt-dlp."""

    def name(self, info, fmt="video_720"):
        cmd = wax.build_ytdlp_command("u", fmt, "/out", ytdlp=[])
        opts = yt_dlp.parse_options(cmd[:-2] + ["--quiet"]).ydl_opts
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.run_all_pps("pre_process", {"id": "x", "ext": "mp4", **info})
            info = ydl.run_all_pps("video", info)  # after the format is chosen
            return os.path.normpath(ydl.prepare_filename(info)).replace(os.sep, "/")

    def test_tag_is_the_height(self):
        self.assertEqual(self.name({"title": "Some Song", "uploader": "Chan", "width": 1280, "height": 720,
                                    "aspect_ratio": 1.78}), "/out/Chan - Some Song [720p].mp4")

    def test_tag_is_what_was_downloaded_not_what_was_asked(self):
        self.assertEqual(self.name({"title": "Me at the zoo", "uploader": "jawed", "width": 320, "height": 240,
                                    "aspect_ratio": 1.33}, "video_1080"), "/out/jawed - Me at the zoo [240p].mp4")

    def test_vertical_short_is_named_by_its_width(self):
        self.assertEqual(self.name({"title": "Clip", "uploader": "U", "width": 1080, "height": 1920,
                                    "aspect_ratio": 0.56}, "video_1080"), "/out/U - Clip [1080p].mp4")

    def test_unknown_height_falls_back_to_the_choice(self):
        self.assertEqual(self.name({"title": "Clip", "uploader": "U"}, "video_480"), "/out/U - Clip [480p].mp4")

    def test_playlist(self):
        self.assertEqual(self.name({"title": "Song", "uploader": "U", "playlist_index": 7, "playlist_title": "My Mix",
                                    "width": 1280, "height": 720, "aspect_ratio": 1.78}),
                         "/out/My Mix/007 - U - Song [720p].mp4")

    def test_title_already_has_the_artist(self):
        self.assertEqual(self.name({"title": "Rick Astley - Never Gonna Give You Up", "uploader": "Rick Astley",
                                    "width": 1920, "height": 1080, "aspect_ratio": 1.78}, "video_1080"),
                         "/out/Rick Astley - Never Gonna Give You Up [1080p].mp4")


class DownloadValidationTests(unittest.TestCase):
    def setUp(self):
        self.client = wax.app.test_client()

    def post(self, url, fmt):
        with patch.object(wax.downloads, "submit", return_value=0) as submit, \
                patch.object(wax.ytdlp_manager, "begin_download"):
            r = self.client.post("/download", json={"url": url, "format": fmt, "output_path": "/tmp"})
        return r, submit

    def test_video_for_youtube(self):
        for url in ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "https://youtu.be/x",
                    "https://www.youtube.com/shorts/x"):
            r, submit = self.post(url, "video_720")
            self.assertEqual(r.status_code, 200, url)
            self.assertEqual(submit.call_args[0][0].fmt, "video_720")

    def test_video_refused_elsewhere(self):
        for url in ("https://music.youtube.com/watch?v=x", "https://x.bandcamp.com/track/y"):
            r, submit = self.post(url, "video_1080")
            self.assertEqual(r.status_code, 400, url)
            self.assertFalse(submit.called)

    def test_spotify_refuses_video(self):
        r, submit = self.post("https://open.spotify.com/track/abc", "video_1080")
        self.assertEqual((r.status_code, r.get_json()["error"]), (400, "deezer supports mp3 128k, mp3 320k and flac"))
        self.assertFalse(submit.called)

    def test_unknown_format(self):
        for fmt in ("video_999", "mp4", "", None, ["mp3_320"]):
            r, submit = self.post("https://youtu.be/x", fmt)
            self.assertEqual(r.status_code, 400, fmt)
            self.assertFalse(submit.called)

    def test_audio_still_default(self):
        with patch.object(wax.downloads, "submit", return_value=0) as submit, \
                patch.object(wax.ytdlp_manager, "begin_download"):
            r = self.client.post("/download", json={"url": "https://youtu.be/x", "output_path": "/tmp"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(submit.call_args[0][0].fmt, "mp3_320")


class PageTests(unittest.TestCase):
    def test_chips(self):
        page = wax.app.test_client().get("/").get_data(as_text=True)
        # the audio / video chip is the same in-field chip as the format one
        self.assertIn('class="btn btn--white input__chip input__chip--lead" id="modeChip" hidden', page)
        offered = re.findall(r'\["(video_\d+)", "(\d+p)"\]', page)
        self.assertEqual(offered, [(f, f"{h}p") for f, h in wax.VIDEO_FORMATS.items()])
        self.assertIn('"mode.audio": "audio"', page)
        self.assertIn('"mode.video": "video"', page)


if __name__ == "__main__":
    unittest.main()
