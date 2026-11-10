"""Requests from other web pages must not reach the API (CSRF / DNS rebinding)."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402


class RequestOriginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv, cls.cfg, cls.con = app.make_server(cls.tmp.name, port=0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.con.close()
        cls.tmp.cleanup()

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def item_count(self):
        return self.con.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def test_same_origin_browser_request_allowed(self):
        origin = "http://127.0.0.1:%d" % self.port
        status, _ = self.request("POST", "/api/items", {"Origin": origin, "Sec-Fetch-Site": "same-origin",
                                                        "Content-Type": "application/json"}, b'{"name":"ok"}')
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/api/state", {"Host": "localhost:%d" % self.port})
        self.assertEqual(status, 200)

    def test_cross_origin_post_refused(self):
        before = self.item_count()
        status, body = self.request("POST", "/api/items", {"Origin": "https://evil.example",
                                                           "Content-Type": "application/json"}, b'{"name":"x"}')
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "cross-origin request refused")
        self.assertEqual(self.item_count(), before)

    def test_simple_form_post_refused(self):
        # what an HTML form or no-cors fetch on another site can send without a preflight
        before = self.item_count()
        status, body = self.request("POST", "/api/items", {"Content-Type": "text/plain"}, b'{"name":"x"}')
        self.assertEqual(status, 403)
        self.assertIn("application/json", json.loads(body)["error"])
        self.assertEqual(self.item_count(), before)

    def test_sec_fetch_site_cross_site_refused(self):
        status, _ = self.request("GET", "/api/state", {"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_dns_rebinding_host_refused(self):
        status, body = self.request("GET", "/api/state", {"Host": "attacker.example:%d" % self.port})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "unexpected Host header")
        status, _ = self.request("GET", "/api/state", {"Host": "127.0.0.1:1"})
        self.assertEqual(status, 403)

    def test_origin_with_wrong_port_or_scheme_refused(self):
        for origin in ("http://127.0.0.1:1", "https://127.0.0.1:%d" % self.port, "null"):
            status, _ = self.request("DELETE", "/api/items/none", {"Origin": origin})
            self.assertEqual(status, 403, origin)


class RequestRejectionUnitTest(unittest.TestCase):
    def test_rules(self):
        ok = {"Host": "127.0.0.1:8765"}
        self.assertIsNone(app.request_rejection("GET", ok, 8765))
        self.assertIsNone(app.request_rejection("GET", {"Host": "[::1]:8765"}, 8765))
        self.assertIsNotNone(app.request_rejection("GET", {}, 8765))
        self.assertIsNone(app.request_rejection("PATCH", dict(ok, **{"Content-Type": "application/json; charset=utf-8"}), 8765))
        self.assertIsNotNone(app.request_rejection("PATCH", ok, 8765))
        self.assertIsNone(app.request_rejection("DELETE", ok, 8765))


if __name__ == "__main__":
    unittest.main()
