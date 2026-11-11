"""Source quality: python3 ytdlp-gui/test_quality.py

Lossy formats ask for an exact bitrate; a link is probed before the download,
and the format chip offers only what its source can back, best first.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")

# trimmed -J output of real links (yt-dlp 2025, -f bestaudio/best)
YOUTUBE = {"extractor_key": "Youtube", "format_id": "251", "acodec": "opus", "ext": "webm", "abr": 147.595,
           "formats": [{"format_id": "140", "acodec": "mp4a.40.2", "abr": 129.487},
                       {"format_id": "251", "acodec": "opus", "abr": 147.595}]}
SOUNDCLOUD = {"extractor_key": "Soundcloud", "format_id": "hls_aac_160k", "acodec": "mp4a.40.2", "ext": "m4a",
              "abr": 160, "formats": [{"format_id": "hls_aac_160k"}]}
BANDCAMP_TRACK = {"extractor_key": "Bandcamp", "format_id": "mp3-128", "acodec": "mp3", "ext": "mp3", "abr": 128,
                  "formats": [{"format_id": "mp3-128"}]}
BANDCAMP_ALBUM = {"_type": "playlist", "extractor_key": "BandcampAlbum", "entries": [BANDCAMP_TRACK]}
# SoundCloud's "download" format: the uploader's own file, no codec given
ORIGINAL_WAV = {"extractor_key": "Soundcloud", "format_id": "download", "ext": "wav",
                "formats": [{"format_id": "download"}]}


class Commands(unittest.TestCase):
    def test_lossy_formats_ask_an_exact_bitrate(self):
        # "0" is LAME's VBR scale (~245k); "320K" is a constant 320
        for fmt, rate in (("mp3_128", "128K"), ("mp3_320", "320K"), ("aac", "256K")):
            args = wax.FORMATS[fmt]
            self.assertEqual(args[args.index("--audio-quality") + 1], rate, fmt)

    def test_aac_prefers_an_aac_stream(self):
        cmd = wax.build_ytdlp_command("https://youtu.be/x", "aac", "/out", ytdlp=["y"])
        self.assertEqual(cmd[cmd.index("-f") + 1], "bestaudio[acodec^=mp4a]/bestaudio/best")
        cmd = wax.build_ytdlp_command("https://youtu.be/x", "mp3_320", "/out", ytdlp=["y"])
        self.assertEqual(cmd[cmd.index("-f") + 1], "bestaudio/best")

    def test_probe_command_downloads_nothing(self):
        cmd = wax.probe_command("https://a.bandcamp.com/album/x", ytdlp=["y"])
        for flag in ("--skip-download", "-J", "--ignore-config", "--no-playlist"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("-f") + 1], "bestaudio/best")
        self.assertEqual(cmd[cmd.index("--playlist-items") + 1], "1")
        self.assertEqual(cmd[-2:], ["--", "https://a.bandcamp.com/album/x"])


class Sources(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(wax.source_quality(YOUTUBE), {"kind": "lossy", "kbps": 148, "site": "youtube"})
        self.assertEqual(wax.source_quality(SOUNDCLOUD), {"kind": "lossy", "kbps": 160, "site": "soundcloud"})
        self.assertEqual(wax.source_quality(BANDCAMP_TRACK), {"kind": "mp3", "kbps": 128, "site": "bandcamp"})
        self.assertEqual(wax.source_quality(BANDCAMP_ALBUM), {"kind": "mp3", "kbps": 128, "site": "bandcamp"})
        self.assertEqual(wax.source_quality(ORIGINAL_WAV)["kind"], "lossless")
        self.assertEqual(wax.source_quality({**YOUTUBE, "acodec": "flac", "abr": None})["kind"], "lossless")

    def test_unknown_sources(self):
        for info in ({}, {"entries": []}, {"_type": "playlist", "entries": [None]},
                     {"formats": [{}], "acodec": "none", "ext": "mp4"}, {"formats": [{}]}):
            self.assertIsNone(wax.source_quality(info), info)

    def test_offered_formats(self):
        offer = lambda info: wax.offered_formats(wax.source_quality(info))
        # bandcamp: its 128k mp3 is all there is, copied as is
        self.assertEqual(offer(BANDCAMP_ALBUM), (["mp3_128"], "mp3_128"))
        self.assertEqual(offer({**BANDCAMP_TRACK, "abr": 320}), (["mp3_320"], "mp3_320"))
        self.assertEqual(offer({**BANDCAMP_TRACK, "abr": None}), (["mp3_128", "mp3_320"], "mp3_320"))
        # a lossy stream: no flac / wav, mp3 320 keeps the most of it
        self.assertEqual(offer(YOUTUBE), (["mp3_128", "mp3_320", "aac"], "mp3_320"))
        self.assertEqual(offer(SOUNDCLOUD), (["mp3_128", "mp3_320", "aac"], "mp3_320"))
        # a lossless original: everything, flac first
        self.assertEqual(offer(ORIGINAL_WAV), (list(wax.AUDIO_FORMATS), "flac"))
        self.assertEqual(wax.offered_formats(None), (list(wax.AUDIO_FORMATS), None))
        for formats, best in (offer(YOUTUBE), offer(BANDCAMP_ALBUM), offer(ORIGINAL_WAV)):
            self.assertIn(best, formats)
            self.assertTrue(set(formats) <= set(wax.AUDIO_FORMATS))


class Probe(unittest.TestCase):
    def run_with(self, stdout=b"", returncode=0, raises=None):
        def run(cmd, **kwargs):
            self.assertEqual(kwargs["timeout"], wax.PROBE_TIMEOUT)
            if raises:
                raise raises
            return SimpleNamespace(returncode=returncode, stdout=stdout)
        return run

    def test_probe_source(self):
        r = wax.probe_source("u", run=self.run_with(json.dumps(BANDCAMP_ALBUM).encode()), ytdlp=["y"])
        self.assertEqual(r, {"formats": ["mp3_128"], "best": "mp3_128",
                             "source": {"kind": "mp3", "kbps": 128, "site": "bandcamp"}})

    def test_failures_are_unknown(self):
        unknown = {"formats": None, "best": None, "source": None}
        for run in (self.run_with(b"", returncode=1), self.run_with(b"not json"), self.run_with(b"[1]"),
                    self.run_with(raises=subprocess.TimeoutExpired("y", 30)), self.run_with(raises=OSError())):
            self.assertEqual(wax.probe_source("u", run=run, ytdlp=["y"]), unknown)

    def test_route(self):
        c = wax.app.test_client()
        none = {"formats": None, "best": None, "source": None}
        with patch.object(wax, "probe_source") as probe:
            for body in ({}, {"url": ""}, {"url": 5}, {"url": "not a link"},
                         {"url": "https://open.spotify.com/track/1"}, {"url": "https://a.com/" + "x" * 3000}):
                self.assertEqual(c.post("/probe", json=body).get_json(), none, body)
            probe.assert_not_called()

    def test_route_caches_answers_not_failures(self):
        c = wax.app.test_client()
        wax._probe_cache.clear()
        answer = {"formats": ["mp3_128"], "best": "mp3_128", "source": {"kind": "mp3", "kbps": 128, "site": "bandcamp"}}
        with patch.object(wax, "probe_source", return_value=answer) as probe:
            for _ in range(3):
                self.assertEqual(c.post("/probe", json={"url": "https://a.bandcamp.com/x"}).get_json(), answer)
            self.assertEqual(probe.call_count, 1)
        with patch.object(wax, "probe_source", return_value={"formats": None, "best": None, "source": None}) as probe:
            for _ in range(2):
                c.post("/probe", json={"url": "https://offline.example.com/x"})
            self.assertEqual(probe.call_count, 2)
        self.assertFalse(wax.ytdlp_manager.busy(), "a probe must release the yt-dlp update lock")
        wax._probe_cache.clear()


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PageLogic(unittest.TestCase):
    """The chip narrows to the probe's formats and the status line says why."""

    def run_page(self, script):
        page = open(PAGE, encoding="utf-8").read()
        block = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        strings = re.search(r'<script type="application/json" id="strings">(.*?)</script>', page, re.S).group(1)
        src = "\n".join([
            "const document = { getElementById: () => ({ textContent: %s }) };" % json.dumps(strings),
            block("const STRINGS", "const msg"),
            'lang = "en";',
            block("const FORMATS", "const el ="),
            'const urlEl = { value: "" }; let phase = "idle"; let urlInvalid = false;',
            block("let fmtKey", "let phase"),
            block("function isSpotify", "// Ask the server"),
            block("// Why the chip", "// Returns the bridge"),
            block("function isLikelyUrl", "// While busy"),
            block("function hostOf", "function validate"),
            script,
        ])
        r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_chip_and_note(self):
        out = self.run_page("""
        const keys = () => availableFormats().map(([k]) => k);
        const out = {};
        urlEl.value = "https://a.bandcamp.com/album/x";
        out.before = keys();
        probes.set(urlEl.value, {formats: ["mp3_128"], best: "mp3_128", source: {kind: "mp3", kbps: 128, site: "bandcamp"}});
        out.bandcamp = keys(); fitFormat(); out.bandcampFmt = chosenFormat()[1]; out.bandcampNote = qualityNote();
        urlEl.value = "https://youtu.be/x";
        probes.set(urlEl.value, {formats: ["mp3_128", "mp3_320", "aac"], best: "mp3_320", source: {kind: "lossy", kbps: 148, site: "youtube"}});
        fmtKey = "wav"; fitFormat();
        out.youtube = keys(); out.youtubeFmt = chosenFormat()[1]; out.youtubeNote = qualityNote();
        videoMode = true; out.videoNote = qualityNote(); videoMode = false;
        urlEl.value = "https://open.spotify.com/track/1"; fmtKey = "aac"; fitFormat();
        out.spotify = keys(); out.spotifyFmt = fmtKey;
        urlEl.value = "https://x.com/pending"; probes.set(urlEl.value, Promise.resolve());
        out.pending = keys(); out.pendingNote = qualityNote();
        console.log(JSON.stringify(out));""")
        self.assertEqual(out["before"], ["mp3_128", "mp3_320", "flac", "wav", "aac"])
        self.assertEqual(out["bandcamp"], ["mp3_128"])
        self.assertEqual(out["bandcampFmt"], "mp3 128k")
        self.assertEqual(out["bandcampNote"], "bandcamp gives only mp3 128k for free, 320 and flac after purchase")
        self.assertEqual(out["youtube"], ["mp3_128", "mp3_320", "aac"])
        self.assertEqual(out["youtubeFmt"], "mp3 320k")
        self.assertEqual(out["youtubeNote"], "the source streams ~148k: the ceiling, flac and wav won't add to it")
        self.assertEqual(out["videoNote"], "")
        self.assertEqual(out["spotify"], ["mp3_128", "mp3_320", "flac"])
        self.assertEqual(out["spotifyFmt"], "mp3_320")
        self.assertEqual(out["pending"], ["mp3_128", "mp3_320", "flac", "wav", "aac"])
        self.assertEqual(out["pendingNote"], "")

    def test_each_link_starts_from_its_best(self):
        page = open(PAGE, encoding="utf-8").read()
        probed = page[page.index("function probed"):page.index("// Wait (a little)")]
        out = self.run_page("const render = () => {}; const busyAction = () => 'cancel';\n" + probed + """
        const BANDCAMP = {formats: ["mp3_128"], best: "mp3_128", source: {kind: "mp3", kbps: 128, site: "bandcamp"}};
        const YOUTUBE = {formats: ["mp3_128", "mp3_320", "aac"], best: "mp3_320", source: {kind: "lossy", kbps: 148, site: "youtube"}};
        const out = {};
        const arrive = (url, p) => { urlEl.value = url; probes.set(url, p); probed(url, p); return fmtKey; };
        out.bandcamp = arrive("https://a.bandcamp.com/x", BANDCAMP);
        fmtTouchedFor = "https://a.bandcamp.com/x";            // picked by hand there...
        out.youtube = arrive("https://youtu.be/x", YOUTUBE);    // ...the next link still gets its best
        fmtKey = "aac"; fmtTouchedFor = "https://youtu.be/x";
        out.handKept = arrive("https://youtu.be/x", YOUTUBE);   // a hand pick on the same link stays
        fmtTouchedFor = "";
        urlEl.value = "https://youtu.be/other"; probed("https://youtu.be/x", YOUTUBE);
        out.staleIgnored = fmtKey;                              // an answer for a link no longer there
        phase = "busy"; fmtKey = "mp3_128";
        out.busyKept = arrive("https://youtu.be/y", YOUTUBE);   // the chip belongs to the running download
        console.log(JSON.stringify(out));""")
        self.assertEqual(out, {"bandcamp": "mp3_128", "youtube": "mp3_320", "handKept": "aac",
                               "staleIgnored": "aac", "busyKept": "mp3_128"})


if __name__ == "__main__":
    unittest.main()
