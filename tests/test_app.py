"""Run with:  python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402
import demo_data  # noqa: E402


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.srv, cls.cfg, cls.con = app.make_server(cls.tmp, port=0)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if r.headers.get_content_type() == "application/json" else raw

    def test_item_lifecycle(self):
        it = self.call("POST", "/api/items", {"name": "Write report", "project": "Ops"})
        self.assertEqual(it["grp"], self.cfg["default_group"])
        self.assertEqual(it["status"], self.cfg["default_status"])
        self.call("POST", "/api/items/%s/start" % it["id"], {})
        st = self.call("GET", "/api/state")
        running = [s for s in st["sessions"] if s["item_id"] == it["id"] and s["end"] is None]
        self.assertEqual(len(running), 1)
        self.assertEqual([i for i in st["items"] if i["id"] == it["id"]][0]["status"], self.cfg["start_status"])
        done = self.call("PATCH", "/api/items/%s" % it["id"], {"status": self.cfg["done_status"]})
        self.assertIsNotNone(done["end_date"])
        st = self.call("GET", "/api/state")
        self.assertFalse([s for s in st["sessions"] if s["item_id"] == it["id"] and s["end"] is None])
        self.call("POST", "/api/items/%s/notes" % it["id"], {"body": "hello"})
        d = self.call("GET", "/api/items/%s/detail" % it["id"])
        self.assertEqual(d["notes"][0]["body"], "hello")
        dup = self.call("POST", "/api/items/%s/duplicate" % it["id"], {})
        self.assertTrue(dup["name"].endswith("(copy)"))
        self.call("DELETE", "/api/items/%s" % dup["id"])
        csv = self.call("GET", "/api/export/items.csv")
        self.assertIn(b"Write report", csv)

    def test_manual_session(self):
        it = self.call("POST", "/api/items", {"name": "Manual"})
        self.call("POST", "/api/items/%s/sessions" % it["id"],
                  {"start": "2026-01-05T09:00:00", "end": "2026-01-05T10:30:00"})
        d = self.call("GET", "/api/items/%s/detail" % it["id"])
        self.assertEqual(len(d["sessions"]), 1)


class DemoTest(unittest.TestCase):
    def test_demo_builds(self):
        path = os.path.join(tempfile.mkdtemp(), "demo.db")
        demo_data.build(path, days=30)
        con = app.connect(path)
        self.assertGreater(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 10)
        self.assertGreater(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 10)


class ImportHelpersTest(unittest.TestCase):
    def test_parsers(self):
        import import_monday as im
        self.assertEqual(im.to_hour("01:13 PM"), "13:13")
        self.assertEqual(im.to_secs("01:40:21"), 6021)
        self.assertEqual(im.to_clock("05:00:00 PM"), "17:00:00")
        self.assertEqual(im.to_date("2026-11-03 to 2026-11-09"), "2026-11-03")
        m = im.detect_mapping(["Name", "Start Date", "Status", "My Projects", "My Tasks", "Time tracking"], {})
        self.assertEqual(m["project"], "My Projects")
        self.assertEqual(m["tracked"], "Time tracking")


if __name__ == "__main__":
    unittest.main()
