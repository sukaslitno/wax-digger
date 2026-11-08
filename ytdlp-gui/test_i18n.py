"""English and Russian: python3 ytdlp-gui/test_i18n.py

Every key is in both languages, with the same placeholders; the page keeps no
visible text outside its dictionary; the server answers in the chosen language.
"""
import json
import os
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax
import i18n

PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")
with open(PAGE_PATH, encoding="utf-8") as f:
    PAGE = f.read()

PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
BRANDS = {"GitHub", "X", "Telegram"}   # names, not copy


def page_strings():
    m = re.search(r'<script type="application/json" id="strings">(.*?)</script>', PAGE, re.S)
    return json.loads(m.group(1))


def forms(value):
    return value if isinstance(value, list) else [value]


def placeholders(value):
    return {name for form in forms(value) for name in PLACEHOLDER_RE.findall(form)}


class DictionaryShape:
    """Both languages hold the same keys, placeholders and plural forms."""
    en = ru = None
    lowercase_exempt = ()

    def test_same_keys(self):
        self.assertEqual(set(self.en) - set(self.ru), set(), "missing in ru")
        self.assertEqual(set(self.ru) - set(self.en), set(), "missing in en")

    def test_same_placeholders(self):
        for key in self.en:
            for form in forms(self.ru[key]):
                self.assertEqual(set(PLACEHOLDER_RE.findall(form)) - placeholders(self.en[key]), set(), key)
            self.assertEqual(placeholders(self.en[key]), placeholders(self.ru[key]), key)

    def test_plural_forms(self):
        for key in self.en:
            if isinstance(self.en[key], list):
                self.assertEqual(len(self.en[key]), 2, key)
                self.assertEqual(len(self.ru[key]), 3, key)
                self.assertIn("n", placeholders(self.en[key]), f"{key}: a plural needs {{n}}")
            else:
                self.assertIsInstance(self.ru[key], str, key)

    def test_no_empty_text(self):
        for lang in (self.en, self.ru):
            for key, value in lang.items():
                for form in forms(value):
                    self.assertTrue(form.strip(), key)

    def test_all_copy_is_lowercase(self):
        for lang in (self.en, self.ru):
            for key, value in lang.items():
                if key.startswith(self.lowercase_exempt):
                    continue
                for form in forms(value):
                    self.assertEqual(form, form.lower(), key)

    def test_russian_is_russian(self):
        # a Russian string left in English is a missed translation (brand words and codes aside)
        same = [k for k in self.en if self.en[k] == self.ru[k]]
        self.assertEqual(same, ["deezer.arl"] if "deezer.arl" in self.en else [])


class ServerDictionaryTests(DictionaryShape, unittest.TestCase):
    en = i18n.STRINGS["en"]
    ru = i18n.STRINGS["ru"]
    lowercase_exempt = ("menu.",)  # the macOS menu is capitalised like the system's own

    def test_keys_used_by_the_server_exist(self):
        with open(os.path.join(os.path.dirname(PAGE_PATH), "..", "app.py"), encoding="utf-8") as f:
            source = f.read()
        used = set(re.findall(r'\btr\(\s*"([\w.]+)"', source))
        used |= {key for _, key in wax.ERROR_MESSAGES} | {wax.GENERIC_ERROR, wax.UPDATED_MESSAGE,
                                                          wax.QUIT_TITLE, wax.QUIT_MESSAGE}
        used |= set(wax._GOFI_MESSAGES.values()) | set(wax._GOFI_REASONS.values())
        used |= {key for _, key, _ in wax._GOFI_PATTERNS}
        self.assertEqual(used - set(self.en), set())

    def test_every_go_message_and_reason_is_mapped(self):
        # what wax-gofi can say (vendor/gofi/cmd/wax-gofi) must be known here
        go_dir = os.path.join(os.path.dirname(PAGE_PATH), "..", "..", "vendor", "gofi", "cmd", "wax-gofi")
        if not os.path.isdir(go_dir):
            self.skipTest("no vendored wax-gofi")
        with open(os.path.join(go_dir, "reasons.go"), encoding="utf-8") as f:
            reasons = re.findall(r'^\s*reason\w+\s*=\s*"([^"]+)"', f.read(), re.M)
        self.assertTrue(reasons)
        for reason in reasons:
            self.assertIn(reason, wax._GOFI_REASONS, f"unmapped reason: {reason}")
        with open(os.path.join(go_dir, "main.go"), encoding="utf-8") as f:
            fails = re.findall(r'fail\("([^"]+?)"', f.read())
        for text in fails:
            sample = text + "x" if text.endswith(": ") else text   # a detail follows the colon
            self.assertIsNotNone(wax.gofi_text(sample)[1], f"unmapped error: {text}")


class PageDictionaryTests(DictionaryShape, unittest.TestCase):
    strings = page_strings()
    en = strings["en"]
    ru = strings["ru"]

    def test_only_two_languages(self):
        self.assertEqual(set(self.strings), {"en", "ru"})
        self.assertEqual(set(self.strings), set(i18n.LANGUAGES))

    def test_keys_used_by_the_page_exist(self):
        script = PAGE[PAGE.index("<script>\n// Links"):]
        used = set(re.findall(r'\bt\(\s*"([\w.]+)"', script))
        used |= set(re.findall(r'\bmsg\(\s*"([\w.]+)"', script))
        used |= set(re.findall(r'data-i18n(?:-placeholder|-aria)?="([\w.]+)"', PAGE))
        # keys picked by a condition: t(cond ? "a" : "b")
        for cond in re.findall(r'\b(?:t|msg)\(([^()]*\?[^()]*)\)', script):
            # other quoted words in a condition ("done", "error") are not keys
            used |= {k for k in re.findall(r'"([\w.]+)"', cond) if k in self.en or "." in k}
        self.assertEqual(used - set(self.en), set())
        used |= set(re.findall(r'"([\w.]+)"', script)) & set(self.en)   # keys held in a variable
        unused = set(self.en) - used
        self.assertEqual(unused, set(), "keys nobody asks for")

    def test_no_visible_text_outside_the_dictionary(self):
        found = []

        class Scan(HTMLParser):
            skip = 0

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "title"):
                    self.skip += 1
                for name, value in attrs:
                    if name in ("placeholder", "aria-label", "title", "alt") and value and value not in BRANDS and name != "alt":
                        found.append(f"<{tag} {name}={value!r}>")

            def handle_endtag(self, tag):
                if tag in ("script", "style", "title"):
                    self.skip -= 1

            def handle_data(self, data):
                if not self.skip and data.strip():
                    found.append(data.strip())

        Scan().feed(PAGE)
        self.assertEqual(found, [], "text that belongs in the dictionary")

    def test_no_english_sentences_in_the_script(self):
        script = PAGE[PAGE.index("<script>\n// Links"):]
        script = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
        script = re.sub(r"(^|\s)//[^\n]*", "", script)
        words = re.compile(r"^[a-z][a-z'’-]*( [a-z'’.,!?…:-]+)+$", re.I)
        allowed = {"btn btn--full ", "apple music", "amazon music"}   # class names, brands
        found = []
        for literal in re.findall(r'"([^"\\\n]*)"|`([^`\\\n]*)`', script):
            text = "".join(literal)
            if text in allowed or "${" in text or "/" in text:
                continue
            if words.match(text.strip()):
                found.append(text)
        self.assertEqual(found, [], "sentences in the script outside the dictionary")

    def test_the_switch_is_an_existing_component(self):
        # a white button-large in the info row, no styles of its own
        self.assertIn('class="btn btn--white btn--brand" id="langBtn"', PAGE)


class PluralTests(unittest.TestCase):
    def test_russian(self):
        cases = {1: 0, 2: 1, 3: 1, 4: 1, 5: 2, 10: 2, 11: 2, 12: 2, 14: 2, 15: 2, 20: 2, 21: 0, 22: 1,
                 25: 2, 100: 2, 101: 0, 111: 2, 112: 2, 121: 0}
        for n, form in cases.items():
            self.assertEqual(i18n.plural_index("ru", n), form, n)

    def test_english(self):
        self.assertEqual([i18n.plural_index("en", n) for n in (0, 1, 2, 21)], [1, 0, 1, 1])

    def test_page_helper_matches(self):
        # the page's pluralIndex() is the same rule: run it for real if node is around
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("no node")
        start = PAGE.index("function pluralIndex")
        end = PAGE.index("function guessLanguage")
        code = PAGE[start:end] + "\nconst out = [];\nfor (const l of ['en','ru']) for (let n = 0; n < 200; n++) out.push(pluralIndex(l, n));\nconsole.log(JSON.stringify(out));"
        got = json.loads(subprocess.run([node, "-e", code], capture_output=True, text=True, check=True).stdout)
        want = [i18n.plural_index(l, n) for l in ("en", "ru") for n in range(200)]
        self.assertEqual(got, want)

    def test_tr_picks_the_form(self):
        with patch.dict(i18n.STRINGS["ru"], {"x": ["{n} трек", "{n} трека", "{n} треков"]}), \
                patch.dict(i18n.STRINGS["en"], {"x": ["{n} track", "{n} tracks"]}):
            self.assertEqual([i18n.tr("x", "ru", n=n) for n in (1, 2, 5, 21)],
                             ["1 трек", "2 трека", "5 треков", "21 трек"])
            self.assertEqual([i18n.tr("x", "en", n=n) for n in (1, 2)], ["1 track", "2 tracks"])

    def test_page_plurals_read_well(self):
        ru = page_strings()["ru"]
        got = [ru["tracks"][i18n.plural_index("ru", n)].replace("{n}", str(n)) for n in (1, 2, 5)]
        self.assertEqual(got, ["1 трек", "2 трека", "5 треков"])


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = patch.object(wax, "CONFIG_PATH", os.path.join(self.tmp.name, "config.json"))
        self.config.start()
        wax.set_language("en")
        self.client = wax.app.test_client()

    def tearDown(self):
        wax.set_language("en")
        self.config.stop()
        self.tmp.cleanup()

    def saved(self):
        try:
            with open(wax.CONFIG_PATH) as f:
                return json.load(f).get("language")
        except OSError:
            return None

    def test_starts_unsaved(self):
        self.assertEqual(self.client.get("/language").get_json(), {"language": "en", "saved": False})

    def test_a_switch_is_saved_and_changes_the_server_copy(self):
        r = self.client.post("/language", json={"language": "ru"})
        self.assertEqual(r.get_json(), {"language": "ru", "saved": True})
        self.assertEqual(self.saved(), "ru")
        self.assertEqual(wax.friendly_error("ERROR: Private video"), "это приватное")
        r = self.client.post("/download", json={"url": ""})
        self.assertEqual((r.status_code, r.get_json()["error"]), (400, "нужна ссылка"))
        self.assertEqual(self.client.post("/language", json={"language": "en"}).get_json()["language"], "en")
        self.assertEqual(self.client.post("/download", json={"url": ""}).get_json()["error"], "url is required")

    def test_a_guess_is_not_saved(self):
        r = self.client.post("/language", json={"language": "ru", "save": False})
        self.assertEqual(r.get_json(), {"language": "ru", "saved": False})
        self.assertIsNone(self.saved())
        self.assertEqual(wax.get_language(), "ru")

    def test_unknown_language_is_refused(self):
        for bad in ("de", "", None, 5):
            self.assertEqual(self.client.post("/language", json={"language": bad}).status_code, 400)
        self.assertEqual(wax.get_language(), "en")

    def test_saved_choice_wins_at_launch(self):
        wax.save_config({"output_path": "/x", "language": "ru"})
        wax.set_language("en")
        wax.init_language()
        self.assertEqual(wax.get_language(), "ru")
        wax.save_config({"output_path": "/x"})
        with patch.object(wax, "system_language", return_value="ru"):
            wax.init_language()
        self.assertEqual(wax.get_language(), "ru")
        with patch.object(wax, "system_language", return_value="en"):
            wax.init_language()
        self.assertEqual(wax.get_language(), "en")

    def test_system_language(self):
        for var, value, want in (("LANG", "ru_RU.UTF-8", "ru"), ("LANG", "en_US.UTF-8", "en"),
                                 ("LANG", "de_DE.UTF-8", "en"), ("LANG", "C", "en")):
            env = {k: "" for k in ("LC_ALL", "LC_MESSAGES", "LANG")}
            env[var] = value
            with patch.dict(os.environ, env), patch.object(i18n.sys, "platform", "linux"), patch.object(i18n.os, "name", "posix"):
                self.assertEqual(i18n.system_language(), want, value)

    def test_progress_lines_follow_the_language(self):
        wax.set_language("ru")
        t = wax.YtdlpProgress()
        self.assertEqual(t.feed("[download] Downloading item 3 of 12")[0]["message"], "трек 3 из 12")
        self.assertEqual(t.feed("[ExtractAudio] Destination: /x/a.mp3")[0]["message"], "конвертирую 3 из 12")
        self.assertEqual(wax.YtdlpProgress()._label(), "качаю…")
        wax.set_language("en")
        self.assertEqual(wax.YtdlpProgress()._label(), "downloading…")

    def test_gofi_text_in_russian(self):
        wax.set_language("ru")
        ev = lambda raw: wax.gofi_event(json.dumps(raw))
        self.assertEqual(ev({"type": "progress", "message": "downloading 2 of 9", "percent": 1})["message"], "трек 2 из 9")
        self.assertEqual(ev({"type": "status", "message": "finding track 4 of 9 on deezer…"})["message"],
                         "ищу трек 4 из 9 на deezer…")
        err = ev({"type": "error", "message": "could not connect to deezer. check your arl and connection."})
        self.assertEqual(err["code"], "go.connect")
        self.assertIn("не удалось подключиться", err["message"])
        self.assertEqual(ev({"type": "error", "message": "could not find this spotify link on deezer: 404"})["message"],
                         "не нашёл эту ссылку spotify на deezer: 404")
        self.assertEqual(ev({"type": "status", "message": "Something New"})["message"], "something new")
        self.assertEqual(wax.gofi_reason("not found on deezer"), "нет на deezer")
        self.assertEqual(wax.gofi_reason("not available in mp3 320 for this account"),
                         "недоступно в mp3 320 для этого аккаунта")
        self.assertEqual(wax.gofi_reason("something nobody knows"), "something nobody knows")
        wax.set_language("en")
        self.assertEqual(ev({"type": "error", "message": "could not connect to deezer. check your arl and connection."})
                         ["message"], "could not connect to deezer. check your arl and connection.")

    def test_track_events_are_localized_but_the_log_keeps_the_raw_line(self):
        wax.set_language("ru")
        log = wax.DownloadLog(1, "https://open.spotify.com/track/x", "mp3_320", "deezer")
        log.line("WAX_EVENT:{\"reason\": \"not found on deezer\"}")
        log.track({"type": "track", "status": "failed", "artist": "", "title": "track 5", "reason": "not found on deezer"})
        snap = log.snapshot()
        self.assertEqual(snap["failures"][0]["title"], "трек 5")
        self.assertEqual(snap["failures"][0]["reason"], "нет на deezer")
        self.assertIn("not found on deezer", snap["lines"][0])

    def test_dialog_and_menu_text(self):
        wax.set_language("ru")
        self.assertEqual(wax.tr(wax.QUIT_TITLE), "выйти из wax?")
        self.assertEqual(wax.tr("menu.quit"), "Выйти из Wax")

    def test_ui_serves_the_cyrillic_font(self):
        self.assertEqual(self.client.get("/jetbrains-mono-cyrillic.woff2").status_code, 200)
        self.assertIn("jetbrains-mono-cyrillic.woff2", self.client.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
