"""Projects & tasks: task -> project autofill, lists, renames, imports."""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402
import catalog  # noqa: E402

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None


class CatalogApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv, self.cfg, self.con = app.make_server(self.tmp.name, port=0)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.con.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return 200, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            finally:
                e.close()

    def state(self):
        return self.call("GET", "/api/state")[1]

    def test_choosing_a_task_fills_in_its_project(self):
        self.call("POST", "/api/tasks", {"name": "Tickets", "project": "ITS Support"})
        code, it = self.call("POST", "/api/items", {"name": "Help desk", "task": "Tickets"})
        self.assertEqual((code, it["project"]), (200, "ITS Support"))
        _, it2 = self.call("POST", "/api/items", {"name": "Other"})
        _, upd = self.call("PATCH", "/api/items/%s" % it2["id"], {"task": "  Tickets "})
        self.assertEqual((upd["task"], upd["project"]), ("Tickets", "ITS Support"))
        # an explicit project wins over the task's
        _, upd = self.call("PATCH", "/api/items/%s" % it2["id"], {"task": "Tickets", "project": "Elsewhere"})
        self.assertEqual(upd["project"], "Elsewhere")
        self.assertIn("Elsewhere", [p["name"] for p in self.state()["projects"]])

    def test_new_task_on_item_joins_the_items_project(self):
        _, it = self.call("POST", "/api/items", {"name": "x", "project": "Research"})
        _, upd = self.call("PATCH", "/api/items/%s" % it["id"], {"task": "Lit review"})
        self.assertEqual(upd["project"], "Research")
        tasks = {t["name"]: t["project"] for t in self.state()["tasks"]}
        self.assertEqual(tasks["Lit review"], "Research")

    def test_clearing_task_keeps_project(self):
        self.call("POST", "/api/tasks", {"name": "Tickets", "project": "ITS"})
        _, it = self.call("POST", "/api/items", {"name": "x", "task": "Tickets"})
        _, upd = self.call("PATCH", "/api/items/%s" % it["id"], {"task": None})
        self.assertEqual((upd["task"], upd["project"]), (None, "ITS"))

    def test_project_crud_and_rename_cascade(self):
        code, p = self.call("POST", "/api/projects", {"name": "  Ops   Team ", "status": "Ongoing",
                                                      "start_date": "2026-01-01"})
        self.assertEqual((code, p["name"]), (200, "Ops Team"))
        self.assertEqual(self.call("POST", "/api/projects", {"name": "Ops Team"})[0], 409)
        self.assertEqual(self.call("POST", "/api/projects", {"name": "  "})[0], 400)
        self.call("POST", "/api/tasks", {"name": "Patching", "project": "Ops Team"})
        self.call("POST", "/api/items", {"name": "a", "task": "Patching"})
        self.assertEqual(self.call("DELETE", "/api/projects/%d" % p["id"])[0], 409)   # in use
        code, upd = self.call("PATCH", "/api/projects/%d" % p["id"], {"name": "Operations", "end_date": "2026-12-31"})
        self.assertEqual((upd["name"], upd["end_date"]), ("Operations", "2026-12-31"))
        st = self.state()
        self.assertEqual({i["project"] for i in st["items"]}, {"Operations"})
        self.assertEqual({t["project"] for t in st["tasks"]}, {"Operations"})
        _, other = self.call("POST", "/api/projects", {"name": "Spare"})
        self.assertEqual(self.call("PATCH", "/api/projects/%d" % other["id"], {"name": "Operations"})[0], 409)
        self.assertEqual(self.call("DELETE", "/api/projects/%d" % other["id"])[0], 200)
        self.assertEqual(self.call("PATCH", "/api/projects/999", {"name": "x"})[0], 404)
        self.assertEqual(self.call("PATCH", "/api/projects/abc", {"name": "x"})[0], 404)

    def test_task_rename_and_move_update_items(self):
        _, t = self.call("POST", "/api/tasks", {"name": "Drop ins", "project": "Support"})
        _, it = self.call("POST", "/api/items", {"name": "a", "task": "Drop ins"})
        self.assertEqual(self.call("DELETE", "/api/tasks/%d" % t["id"])[0], 409)        # in use
        self.call("PATCH", "/api/tasks/%d" % t["id"], {"name": "Drop-in sessions", "project": "Training"})
        st = self.state()
        item = [i for i in st["items"] if i["id"] == it["id"]][0]
        self.assertEqual((item["task"], item["project"]), ("Drop-in sessions", "Training"))
        self.assertIn("Training", [p["name"] for p in st["projects"]])
        _, spare = self.call("POST", "/api/tasks", {"name": "Spare"})
        self.assertEqual(self.call("PATCH", "/api/tasks/%d" % spare["id"], {"name": "Drop-in sessions"})[0], 409)
        self.assertEqual(self.call("DELETE", "/api/tasks/%d" % spare["id"])[0], 200)

    def test_state_lists_projects_and_tasks(self):
        self.call("POST", "/api/tasks", {"name": "T1", "project": "P1"})
        st = self.state()
        self.assertEqual([p["name"] for p in st["projects"]], ["P1"])
        self.assertEqual([(t["name"], t["project"]) for t in st["tasks"]], [("T1", "P1")])
        self.assertIn("project_statuses", st["config"])


class SyncFromItemsTest(unittest.TestCase):
    def test_lists_built_from_existing_items(self):
        with tempfile.TemporaryDirectory() as d:
            con = app.connect(os.path.join(d, "w.db"))
            rows = [("1", "a", "Tickets", "ITS"), ("2", "b", "Tickets", "ITS"), ("3", "c", "Tickets", "Other"),
                    ("4", "d", "Reading", None), ("5", "e", None, "Leave")]
            for i, (iid, name, task, proj) in enumerate(rows):
                con.execute("INSERT INTO items(id,name,task,project,position) VALUES (?,?,?,?,?)",
                            (iid, name, task, proj, i))
            catalog.sync_from_items(con)
            projects, tasks = catalog.listing(con)
            self.assertEqual([p["name"] for p in projects], ["ITS", "Other", "Leave"])
            self.assertEqual({t["name"]: t["project"] for t in tasks}, {"Tickets": "ITS", "Reading": None})
            # running again changes nothing; manual changes are kept
            con.execute("UPDATE tasks SET project='Other' WHERE name='Tickets'")
            catalog.sync_from_items(con)
            self.assertEqual(dict(con.execute("SELECT name,project FROM tasks").fetchall())["Tickets"], "Other")
            con.close()


@unittest.skipIf(openpyxl is None, "openpyxl not installed")
class ImportCatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        wb = openpyxl.Workbook()
        ws = wb.active
        for r in [["My Projects"], ["Group Title"],
                  ["Name", "Status", "Timeline - Start", "Timeline - End", "Item ID (auto generated)"],
                  ["Research  Data", "Working on it", "2025-07-01", "2026-06-30", "1"],
                  ["Support", "Ongoing", None, None, "2"],
                  ["Later projects"],
                  ["Operations", "Not started", None, None, "3"],
                  [None, None, "2025-07-01", "2026-06-30", None]]:
            ws.append(r)
        self.projects = os.path.join(d, "projects.xlsx")
        wb.save(self.projects)
        wb = openpyxl.Workbook()
        ws = wb.active
        for r in [["My Tasks"], ["All"], ["Name", "Subitems", "Status", "My Projects"],
                  ["Metadata", None, "Working on it", "Research Data"],
                  ["Tickets", None, "Ongoing", "Support"],
                  ["Unlinked", None, None, None], [], ["Done"], ["Name", "Subitems", "Status", "My Projects"]]:
            ws.append(r)
        self.tasks = os.path.join(d, "tasks.xlsx")
        wb.save(self.tasks)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_boards(self):
        import import_monday as im
        self.assertEqual(im.read_projects(self.projects), [
            {"name": "Research Data", "status": "Working on it", "start_date": "2025-07-01", "end_date": "2026-06-30"},
            {"name": "Support", "status": "Ongoing", "start_date": None, "end_date": None},
            {"name": "Operations", "status": "Not started", "start_date": None, "end_date": None}])
        self.assertEqual([(t["name"], t["project"]) for t in im.read_tasks(self.tasks)],
                         [("Metadata", "Research Data"), ("Tickets", "Support"), ("Unlinked", None)])

    def test_shared_header_export_and_name_only_items(self):
        """One header at the top, groups separated by blank rows, and an item with only a name."""
        import import_monday as im
        wb = openpyxl.Workbook()
        ws = wb.active
        for r in [["My Tasks"], ["Active"], ["Name", "Status", "My Projects"],
                  ["Alpha", "Ongoing", "P1"], ["Name only"], ["Beta", None, "P1"],
                  [], ["Archived"], ["Gamma", "Done", "P2"], ["Also name only"]]:
            ws.append(r)
        path = os.path.join(self.tmp.name, "shared.xlsx")
        wb.save(path)
        _, _, rows = im.read_board(path)
        self.assertEqual([(d["Name"], d["_group"]) for d in rows],
                         [("Alpha", "Active"), ("Name only", "Active"), ("Beta", "Active"),
                          ("Gamma", "Archived"), ("Also name only", "Archived")])

    def test_lists_only_import_into_existing_database(self):
        import import_monday as im
        ddir = os.path.join(self.tmp.name, "data")
        os.makedirs(ddir)
        con = app.connect(os.path.join(ddir, "worksessions.db"))
        con.execute("INSERT INTO items(id,name,task,project,position) VALUES ('1','x','Tickets',NULL,0)")
        con.commit()
        con.close()
        with open(os.path.join(ddir, "config.json"), "w") as f:
            json.dump({"title": "Mine"}, f)
        im.main(["--projects", self.projects, "--tasks", self.tasks, "--data", ddir])
        con = sqlite3.connect(os.path.join(ddir, "worksessions.db"))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)  # items untouched
        self.assertEqual(dict(con.execute("SELECT name,project FROM tasks").fetchall()),
                         {"Metadata": "Research Data", "Tickets": "Support", "Unlinked": None})
        self.assertEqual(con.execute("SELECT status,start_date FROM projects WHERE name='Research Data'").fetchone(),
                         ("Working on it", "2025-07-01"))
        con.close()
        with open(os.path.join(ddir, "config.json")) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["title"], "Mine")
        names = [s["name"] for s in cfg["project_statuses"]]
        self.assertIn("Ongoing", names)
        self.assertIn("Done", names)
        self.assertTrue(os.listdir(os.path.join(ddir, "backups")))

    def test_needs_some_input(self):
        import import_monday as im
        with self.assertRaises(SystemExit):
            im.main([])


if __name__ == "__main__":
    unittest.main()
