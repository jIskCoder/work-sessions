"""End-to-end test of import_monday.py against a synthetic monday.com export.

The workbook is generated here in the same layout monday's "Export board to Excel"
produces, so no real (personal) export is stored in the repository.
"""
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import import_monday as im  # noqa: E402

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

HEADER = ["Name", "Subitems", "Start Date", "Start Hour", "Priority", "End Date", "End Hour",
          "Time tracking", "Duration", "Client", "Workstream", "Status", "Item ID (auto generated)"]


def build_export(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "demo board"
    rows = [
        ["Demo Board"],
        [],
        ["Today"],
        HEADER,
        ["Write plan", None, dt.datetime(2026, 3, 2), "09:15 AM", "High", dt.datetime(2026, 3, 3), "05:00 PM",
         "01:30:00", 1.5, "Acme", "Planning", "Done", "101"],
        ["Subitems", "Name", "Owner", "Status", "Date", "Item ID (auto generated)"],
        [None, "Draft outline", None, "Done", dt.datetime(2026, 3, 2), "201"],
        ["Standup", None, dt.datetime(2026, 3, 2), "10:00 AM", "Low", None, None,
         "00:15:00", 0.25, "Acme", "Meetings", "Working on it", "102"],
        ["Standup", None, dt.datetime(2026, 3, 3), "10:00 AM", "Low", None, None,
         "00:20:00", 0.33, "Acme", "Meetings", "Not started", "103"],
        [None, None, "2026-03-02 to 2026-03-03", None, None, "2026-03-03"],   # group summary row
        [],
        ["Later"],
        HEADER,
        ["Read paper", None, dt.datetime(2025, 9, 1), "01:00 PM", "Medium", None, None,
         None, 0, "  Internal   Research ", None, "Not started", "104"],
    ]
    for r in rows:
        ws.append(r)
    tt = wb.create_sheet("Time tracking")
    for r in [
        ["Time Tracking"], [],
        # second "Standup" block listed first: matching must use the totals, not the order
        ["Standup"],
        ["Started By", "Ended By", "Start Date", "End Date", "Start Time", "End Time", "Duration"],
        ["Pat", "Pat", "2026-03-03", "2026-03-03", "10:00:00 AM", "10:20:00 AM", "00:20:00"],
        ["Total", None, None, None, None, None, "00:20:00"], [],
        ["Standup"],
        ["Started By", "Ended By", "Start Date", "End Date", "Start Time", "End Time", "Duration"],
        ["Pat", "Pat", "2026-03-02", "2026-03-02", "10:00:00 AM", "10:15:00 AM", "00:15:00"],
        ["Total", None, None, None, None, None, "00:15:00"], [],
        ["Write plan"],
        ["Started By", "Ended By", "Start Date", "End Date", "Start Time", "End Time", "Duration"],
        ["Pat", "Pat", "2026-03-02", "2026-03-02", "09:15:00 AM", "10:15:00 AM", "01:00:00"],
        ["Pat", "Pat", "2026-03-03", "2026-03-03", "04:30:00 PM", "05:00:00 PM", "00:30:00"],
        ["Total", None, None, None, None, None, "01:30:00"], [],
        ["Orphan task"],
        ["Started By", "Ended By", "Start Date", "End Date", "Start Time", "End Time", "Duration"],
        ["Pat", "Pat", "2026-03-04", "2026-03-04", "09:00:00 AM", "09:05:00 AM", "00:05:00"],
        ["Total", None, None, None, None, None, "00:05:00"],
    ]:
        tt.append(r)
    up = wb.create_sheet("updates")
    up.append(["Demo Board", "Updates"])
    up.append(["Item ID", "Item Name", "Content Type", "Content Type", "User", "Created At",
               "Update Content", "Likes Count", "Asset IDs", "Post ID", "Parent Post ID"])
    up.append(["101", "Write plan", "Update", None, "Pat", "02/March/2026  11:05:00 AM",
               "Agreed scope with Acme", 0, None, "9001", None])
    wb.save(path)


@unittest.skipIf(openpyxl is None, "openpyxl not installed (pip install -r requirements-dev.txt)")
class ImportMondayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.xlsx = os.path.join(self.tmp.name, "export.xlsx")
        build_export(self.xlsx)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_export(self):
        data = im.read_export(self.xlsx, {"project": "Client", "task": "Workstream"})
        self.assertEqual(data["title"], "Demo Board")
        self.assertEqual(data["mapping"]["tracked"], "Time tracking")
        self.assertEqual(data["mapping"]["id"], "Item ID (auto generated)")
        items = {i["id"]: i for i in data["items"]}
        self.assertEqual(len(items), 4)
        self.assertEqual(items["101"]["group"], "Today")
        self.assertEqual(items["104"]["group"], "Later")
        self.assertEqual(items["101"]["start_hour"], "09:15")
        self.assertEqual(items["101"]["end_hour"], "17:00")
        self.assertEqual(items["101"]["tracked"], 5400)
        self.assertEqual(items["104"]["project"], "Internal Research")   # whitespace normalised
        self.assertEqual(items["101"]["extra"], {"Duration": "1.5"})      # unmapped column kept
        self.assertEqual(data["subitems"][0]["parent"], "101")
        self.assertEqual(data["notes"][0]["created"], "2026-03-02T11:05:00")
        self.assertEqual(data["unmatched"], ["Orphan task"])
        by_item = {}
        for s in data["sessions"]:
            by_item.setdefault(s["item_id"], []).append(s)
        self.assertEqual(len(by_item["101"]), 2)
        # duplicate names matched by total, despite the sheet order
        self.assertEqual(by_item["102"][0]["start"], "2026-03-02T10:00:00")
        self.assertEqual(by_item["103"][0]["start"], "2026-03-03T10:00:00")
        self.assertEqual(by_item["103"][0]["end"], "2026-03-03T10:20:00")

    def test_auto_mapping_without_overrides(self):
        data = im.read_export(self.xlsx, {})
        self.assertNotIn("project", data["mapping"])   # "Client" isn't guessable
        self.assertEqual(data["items"][0]["extra"].get("Client"), "Acme")

    def test_cli_import_writes_db_and_config(self):
        ddir = os.path.join(self.tmp.name, "data")
        im.main([self.xlsx, "--data", ddir, "--map", "project=Client", "--map", "task=Workstream"])
        con = sqlite3.connect(os.path.join(ddir, "worksessions.db"))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 4)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 4)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM subitems").fetchone()[0], 1)
        groups = [r[0] for r in con.execute("SELECT name FROM groups ORDER BY position")]
        self.assertEqual(groups, ["Today", "Later"])
        con.close()
        with open(os.path.join(ddir, "config.json")) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["title"], "Demo Board")
        self.assertEqual(cfg["labels"], {"project": "Client", "task": "Workstream"})
        self.assertEqual(cfg["done_status"], "Done")
        self.assertEqual(cfg["start_status"], "Working on it")
        self.assertEqual([p["name"] for p in cfg["priorities"]], ["High", "Medium", "Low"])
        # a second import refuses to overwrite; --replace moves the old database aside
        with self.assertRaises(SystemExit):
            im.main([self.xlsx, "--data", ddir])
        im.main([self.xlsx, "--data", ddir, "--replace"])
        self.assertTrue(any(n.startswith("worksessions-before-import") for n in os.listdir(os.path.join(ddir, "backups"))))

    def test_dry_run_writes_nothing(self):
        ddir = os.path.join(self.tmp.name, "dry")
        im.main([self.xlsx, "--data", ddir, "--dry-run"])
        self.assertFalse(os.path.exists(os.path.join(ddir, "worksessions.db")))

    def test_bad_map_field_rejected(self):
        with self.assertRaises(SystemExit):
            im.main([self.xlsx, "--map", "colour=Status", "--dry-run"])


if __name__ == "__main__":
    unittest.main()
