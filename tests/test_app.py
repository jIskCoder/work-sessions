"""Run with:  python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
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
        cls.srv.server_close()
        cls.con.close()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if r.headers.get_content_type() == "application/json" else raw

    def call_error(self, method, path, body=None, raw_body=None):
        data = raw_body if raw_body is not None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, method=method, data=data,
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(req)
        error = raised.exception
        try:
            return error.code, json.loads(error.read())
        finally:
            error.close()

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

    def test_detail_mutations_and_csv_formula_escaping(self):
        it = self.call("POST", "/api/items", {"name": "=HYPERLINK(\"https://example.test\")"})
        self.call("POST", "/api/groups", {"name": "Later"})
        self.call("PATCH", "/api/items/%s" % it["id"], {"grp": "Later", "project": "Ops"})
        self.call("POST", "/api/items/%s/sessions" % it["id"],
                  {"start": "2026-01-01T09:00:00", "end": "2026-01-01T10:00:00"})
        detail = self.call("GET", "/api/items/%s/detail" % it["id"])
        session_id = detail["sessions"][0]["id"]
        self.call("PATCH", "/api/sessions/%s" % session_id, {"end": "2026-01-01T11:00:00"})
        self.call("DELETE", "/api/sessions/%s" % session_id)
        self.call("POST", "/api/items/%s/subitems" % it["id"], {"name": "Child"})
        detail = self.call("GET", "/api/items/%s/detail" % it["id"])
        subitem_id = detail["subitems"][0]["id"]
        self.call("PATCH", "/api/subitems/%s" % subitem_id, {"status": "Done"})
        self.call("DELETE", "/api/subitems/%s" % subitem_id)
        csv = self.call("GET", "/api/export/items.csv")
        self.assertIn(b"'=HYPERLINK", csv)
        self.assertIn(b"Later", csv)

    def test_invalid_requests_return_client_errors(self):
        code, response = self.call_error("POST", "/api/items", raw_body=b"not json")
        self.assertEqual((code, response["error"]), (400, "invalid JSON body"))
        code, response = self.call_error("PATCH", "/api/items/missing", raw_body=b"[]")
        self.assertEqual((code, response["error"]), (400, "JSON body must be an object"))
        code, response = self.call_error("POST", "/api/items/missing/sessions", body={})
        self.assertEqual((code, response["error"]), (404, "no item"))
        code, response = self.call_error("GET", "/api/missing")
        self.assertEqual((code, response["error"]), (404, "not found"))


class DemoTest(unittest.TestCase):
    def test_demo_builds(self):
        path = os.path.join(tempfile.mkdtemp(), "demo.db")
        demo_data.build(path, days=30)
        con = app.connect(path)
        self.assertGreater(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 10)
        self.assertGreater(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 10)
        con.close()


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


class SecurityHelpersTest(unittest.TestCase):
    def test_static_path_must_be_contained(self):
        self.assertEqual(app.static_path("index.html"), os.path.join(app.STATIC, "index.html"))
        self.assertIsNone(app.static_path("../static-private/secret.txt"))

    def test_csv_formula_values_are_escaped(self):
        for formula in ("=SUM(A1:A2)", "+1+1", "-1+1", "@SUM(A1:A2)", "\t=SUM(A1:A2)"):
            self.assertEqual(app.csv_cell(formula), "'" + formula)
        self.assertEqual(app.csv_cell("normal text"), "normal text")
        self.assertEqual(app.csv_cell(42), 42)


class AppHelpersTest(unittest.TestCase):
    def test_configuration_database_backup_and_state(self):
        with tempfile.TemporaryDirectory() as ddir:
            with open(os.path.join(ddir, "config.json"), "w") as f:
                json.dump({"labels": {"project": "Client"}, "groups": ["Inbox"]}, f)
            cfg = app.load_config(ddir)
            self.assertEqual(cfg["labels"], {"project": "Client", "task": "Task"})
            con = app.connect(os.path.join(ddir, "worksessions.db"))
            app.ensure_groups(con, cfg)
            self.assertEqual(app.state(con, cfg)["groups"], ["Inbox"])
            app.backup(os.path.join(ddir, "worksessions.db"), ddir)
            self.assertTrue(os.listdir(os.path.join(ddir, "backups")))
            con.close()


if __name__ == "__main__":
    unittest.main()
