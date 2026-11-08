"""Focused tests for routing, credentials, and partial download reporting."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax


class SpotifyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = wax.app.test_client()
        self.old_arl = wax.deezer_arl
        wax.deezer_arl = "a" * 192

    def tearDown(self):
        wax.deezer_arl = self.old_arl

    def test_routing_and_formats(self):
        self.assertTrue(wax.spotify_link("open.spotify.com/track/abc"))
        self.assertFalse(wax.spotify_link("https://open.spotify.com.evil.test/track/abc"))
        response = self.client.post("/download", json={"url":"https://open.spotify.com/track/abc", "format":"wav"})
        self.assertEqual(response.status_code, 400)

    def test_session_does_not_expose_credential_or_accept_cross_origin(self):
        response = self.client.get("/deezer-session")
        self.assertEqual(response.json, {"available":True})
        response = self.client.post("/deezer-session", json={"arl":"b" * 192}, headers={"Origin":"https://example.org"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(wax.deezer_arl, "a" * 192)

    @unittest.skipIf(os.name == "nt", "test helper uses a Unix shebang")
    def test_partial_result_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = os.path.join(directory, "helper")
            with open(helper, "w") as f:
                f.write("#!/usr/bin/env python3\nimport json,sys\nrequest=json.load(sys.stdin)\n"
                        "assert request['quality']=='flac' and len(request['arl'])==192\n"
                        "print('WAX_EVENT:'+json.dumps({'type':'result','saved':1,'failed':1,'total':2}))\n"
                        "sys.exit(1)\n")
            os.chmod(helper, 0o700)
            with patch.object(wax, "find_gofi", return_value=helper):
                response = self.client.post("/download", json={"url":"https://open.spotify.com/album/abc", "format":"flac", "output_path":directory})
                self.assertEqual(response.status_code, 200)
                stream = self.client.get("/progress/%d" % response.json["stream_id"]).get_data(as_text=True)
            self.assertIn('"failed": 1', stream)
            self.assertIn("__DONE__:error", stream)
            self.assertNotIn(wax.deezer_arl, stream)


if __name__ == "__main__":
    unittest.main()
