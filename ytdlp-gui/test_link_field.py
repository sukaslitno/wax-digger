"""The link field: python3 ytdlp-gui/test_link_field.py

An (x) and Esc clear it; a link done with is replaced by the next copied one;
a cleared or downloaded link is not offered from the clipboard again, also
after a restart.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")


def read_page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class HandledLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = patch.object(wax, "CONFIG_PATH", os.path.join(self.tmp.name, "config.json"))
        self.config.start()

    def tearDown(self):
        self.config.stop()
        self.tmp.cleanup()

    def test_remembered_across_launches_as_hashes(self):
        url = "https://youtu.be/387cVDfL04k?si=abc"
        self.assertFalse(wax.link_handled(url))
        wax.remember_handled_link(url)
        self.assertTrue(wax.link_handled(url))
        self.assertTrue(wax.link_handled(" " + url + " "))
        self.assertFalse(wax.link_handled("https://youtu.be/other"))
        with open(wax.CONFIG_PATH) as f:
            text = f.read()
        self.assertNotIn("youtu", text, "the settings file must not keep the links themselves")
        self.assertEqual(json.loads(text)["output_path"], wax.DEFAULT_FOLDER, "other settings survive")

    def test_short_list_without_repeats(self):
        for i in range(wax.HANDLED_MAX + 20):
            wax.remember_handled_link(f"https://a.com/{i}")
        wax.remember_handled_link("https://a.com/50")
        wax.remember_handled_link("https://a.com/50")
        kept = wax.load_config()[wax.HANDLED_KEY]
        self.assertEqual(len(kept), wax.HANDLED_MAX)
        self.assertEqual(len(set(kept)), len(kept))
        self.assertEqual(kept[-1], wax.link_digest("https://a.com/50"))
        self.assertFalse(wax.link_handled("https://a.com/0"), "the oldest ones are dropped")

    def test_garbage_is_ignored(self):
        wax.save_config({"output_path": "/x", wax.HANDLED_KEY: "not a list"})
        self.assertFalse(wax.link_handled("https://a.com/x"))
        wax.remember_handled_link("https://a.com/x")
        self.assertTrue(wax.link_handled("https://a.com/x"))
        for bad in ("", "   ", None, 5):
            wax.remember_handled_link(bad)
        self.assertEqual(len(wax.load_config()[wax.HANDLED_KEY]), 1)

    def test_clipboard_skips_handled_links(self):
        api = wax.Api()
        with patch.object(wax, "read_clipboard_text", return_value="https://a.bandcamp.com/x\n"):
            self.assertEqual(api.clipboard_url(), "https://a.bandcamp.com/x")
            api.dismiss_link("https://a.bandcamp.com/x")
            self.assertEqual(api.clipboard_url(), "")
        with patch.object(wax, "read_clipboard_text", return_value="https://a.bandcamp.com/y"):
            self.assertEqual(api.clipboard_url(), "https://a.bandcamp.com/y")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PageLogic(unittest.TestCase):
    """The field's own script, run against a tiny stand-in for the DOM."""

    def run_page(self, script):
        page = read_page()
        block = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        src = "\n".join([
            """
            const listeners = {};
            const on = (name) => (type, fn) => { (listeners[name + ":" + type] ||= []).push(fn); };
            const fire = (name, type, e = {}) => {
              e.preventDefault ||= () => {}; e.stopPropagation ||= () => { e.stopped = true; };
              for (const fn of listeners[name + ":" + type] || []) fn(e);
              return e;
            };
            const window = { addEventListener: on("window") };
            const urlEl = { value: "", selectionStart: 0, selectionEnd: 0, addEventListener: on("url"),
                            focus() {}, select() { this.selected = true; } };
            const nodes = { urlClear: { addEventListener: on("clear") } };
            const el = (id) => nodes[id];
            let info = false, details = false, phase = "idle", posting = "", handledLink = "";
            const seenLinks = new Set();
            let clipboard = "", dismissed = [];
            const api = (name, arg) => name === "clipboard_url" ? Promise.resolve(clipboard)
                                     : name === "dismiss_link" ? (dismissed.push(arg), null) : null;
            const edited = () => {}, validate = () => {}, render = () => {}, probeLater = () => {};
            const tick = () => new Promise((r) => setTimeout(r, 5));
            """,
            block("function isLikelyUrl", "// While busy"),
            block("function setUrl", "// The link in a drop"),
            block("// ===== clearing the link field", "// When the window opens"),
            block("// When the window opens", "// ===== window controls"),
            script,
        ])
        r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_clear_esc_replace(self):
        out = self.run_page("""
        (async () => {
          const out = {};
          // a copied link fills the empty field
          clipboard = "https://a.bandcamp.com/one";
          await prefillFromClipboard(); out.filled = urlEl.value;
          // something typed is never replaced
          urlEl.value = "https://typed.com/x"; clipboard = "https://a.bandcamp.com/two";
          await prefillFromClipboard(); out.typedKept = urlEl.value;
          // a downloaded link is replaced by the next copied one
          urlEl.value = "https://a.bandcamp.com/one"; seenLinks.add("https://a.bandcamp.com/one"); phase = "done";
          await prefillFromClipboard(); out.replaced = urlEl.value;
          // not while it is the one being handed to the server, nor while cancelling
          urlEl.value = "https://a.bandcamp.com/one"; posting = urlEl.value; phase = "busy";
          clipboard = "https://a.bandcamp.com/three";
          await prefillFromClipboard(); out.postingKept = urlEl.value;
          posting = ""; phase = "cancelled";
          await prefillFromClipboard(); out.cancelKept = urlEl.value;
          phase = "idle";
          // the (x) clears and dismisses
          urlEl.value = "https://a.bandcamp.com/three"; dismissed = [];
          fire("clear", "click"); out.cleared = urlEl.value; out.dismissed = dismissed.slice();
          clipboard = "https://a.bandcamp.com/three";
          await prefillFromClipboard(); out.notBack = urlEl.value;
          // Esc with text clears and does not reach the window; on an empty field it does
          urlEl.value = "https://x.com/y";
          out.escStopped = !!fire("url", "keydown", { key: "Escape" }).stopped; out.escCleared = urlEl.value;
          out.escEmptyPasses = !fire("url", "keydown", { key: "Escape" }).stopped;
          // deleting a filled link by hand, key by key, dismisses the whole link
          clipboard = "https://a.bandcamp.com/four"; dismissed = [];
          await prefillFromClipboard();
          for (const v of ["https://a.bandcamp.com/fou", "h", ""]) { urlEl.value = v; fire("url", "input"); }
          out.handDismissed = dismissed.slice();
          // the first click on a link put in from outside selects it whole, the next does not
          setUrl("https://a.bandcamp.com/five"); urlEl.selected = false;
          fire("url", "mouseup"); out.firstClick = !!urlEl.selected;
          urlEl.selected = false; fire("url", "mouseup"); out.secondClick = !!urlEl.selected;
          console.log(JSON.stringify(out));
        })();""")
        self.assertEqual(out["filled"], "https://a.bandcamp.com/one")
        self.assertEqual(out["typedKept"], "https://typed.com/x")
        self.assertEqual(out["replaced"], "https://a.bandcamp.com/two")
        self.assertEqual(out["postingKept"], "https://a.bandcamp.com/one")
        self.assertEqual(out["cancelKept"], "https://a.bandcamp.com/one")
        self.assertEqual(out["cleared"], "")
        self.assertIn("https://a.bandcamp.com/three", out["dismissed"])
        self.assertEqual(out["notBack"], "")
        self.assertTrue(out["escStopped"])
        self.assertEqual(out["escCleared"], "")
        self.assertTrue(out["escEmptyPasses"])
        self.assertEqual(out["handDismissed"], ["https://a.bandcamp.com/four"])
        self.assertTrue(out["firstClick"])
        self.assertFalse(out["secondClick"])

    def test_markup(self):
        page = read_page()
        field = page[page.index('<label class="input" id="urlInput"'):page.index("</label>", page.index('id="urlInput"'))]
        self.assertLess(field.index('id="url"'), field.index('id="urlClear"'))
        self.assertLess(field.index('id="urlClear"'), field.index('id="fmtChip"'))
        self.assertIn('data-i18n-aria="aria.clear"', field)
        self.assertTrue(re.search(r'el\("urlClear"\)\.hidden = !urlEl\.value;', page))
        self.assertTrue(callable(wax.Api.dismiss_link))


if __name__ == "__main__":
    unittest.main()
