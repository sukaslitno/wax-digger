"""Requests that do not come from Wax's own window are refused."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax


class LocalOnlyTest(unittest.TestCase):
    def setUp(self):
        self.client = wax.app.test_client()

    def test_own_window_is_allowed(self):
        for host in ("127.0.0.1:7337", "localhost:7337", "localhost"):
            self.assertEqual(self.client.get("/status", headers={"Host": host}).status_code, 200, host)
        r = self.client.get("/status", headers={"Origin": wax.URL})
        self.assertEqual(r.status_code, 200)

    def test_rebound_host_is_refused(self):
        r = self.client.post("/download", json={"url": "https://example.com/x"},
                             headers={"Host": "evil.example:7337"})
        self.assertEqual(r.status_code, 403)

    def test_foreign_origin_is_refused(self):
        for origin in ("https://evil.example", "chrome-extension://abc", "null"):
            r = self.client.post("/choose-folder", headers={"Origin": origin})
            self.assertEqual(r.status_code, 403, origin)

    def test_no_cors_headers(self):
        r = self.client.get("/status", headers={"Origin": "chrome-extension://abc"})
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)


if __name__ == "__main__":
    unittest.main()
