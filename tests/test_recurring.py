"""Weekly repeating groups."""
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402
import recurring  # noqa: E402


def add(con, iid, name, grp, start, hour="09:00", status="Done", project="P", task="T"):
    con.execute("INSERT INTO items(id,name,grp,start_date,start_hour,priority,end_date,end_hour,project,task,"
                "status,tracked_offset,position) VALUES (?,?,?,?,?,'Low','2026-01-01','10:00',?,?,?,3600,0)",
                (iid, name, grp, start, hour, project, task, status))


class RecurringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = app.connect(os.path.join(self.tmp.name, "w.db"))
        self.cfg = app.load_config(self.tmp.name)
        self.con.executemany("INSERT INTO groups VALUES (?,?)", [("Meetings", 0), ("Other", 1)])
        # last week (Mon 9 Nov 2026 .. Sun 15 Nov)
        add(self.con, "a", "Standup", "Meetings", "2026-11-09", "08:30")
        add(self.con, "b", "Team sync", "Meetings", "2026-11-11", "14:00", project="Ops", task="Sync")
        add(self.con, "c", "Old one", "Meetings", "2026-11-02")          # two weeks ago: not copied
        add(self.con, "d", "Elsewhere", "Other", "2026-11-10")          # other group: not copied
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def week(self, start, end, grp="Meetings"):
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM items WHERE grp=? AND start_date BETWEEN ? AND ? ORDER BY start_date", (grp, start, end))]

    def test_copies_previous_week_into_new_week(self):
        recurring.set_enabled(self.con, "Meetings", True)
        self.assertEqual(recurring.run(self.con, self.cfg, app.new_id, today=dt.date(2026, 11, 18)), {"Meetings": 2})
        got = self.week("2026-11-16", "2026-11-22")
        self.assertEqual([(i["name"], i["start_date"], i["start_hour"]) for i in got],
                         [("Standup", "2026-11-16", "08:30"), ("Team sync", "2026-11-18", "14:00")])
        sync = got[1]
        self.assertEqual((sync["project"], sync["task"], sync["priority"]), ("Ops", "Sync", "Low"))
        self.assertEqual((sync["status"], sync["end_date"], sync["tracked_offset"]),
                         (self.cfg["default_status"], None, 0))
        self.assertEqual(self.week("2026-11-16", "2026-11-22", "Other"), [])

    def test_runs_once_per_week_and_skips_existing_names(self):
        recurring.set_enabled(self.con, "Meetings", True)
        add(self.con, "x", "standup ", "Meetings", "2026-11-17")   # already added by hand this week
        self.con.commit()
        self.assertEqual(recurring.run(self.con, self.cfg, app.new_id, today=dt.date(2026, 11, 16)), {"Meetings": 1})
        # deleting a copy doesn't bring it back the same week
        self.con.execute("DELETE FROM items WHERE name='Team sync' AND start_date='2026-11-18'")
        self.assertEqual(recurring.run(self.con, self.cfg, app.new_id, today=dt.date(2026, 11, 20)), {})
        self.assertEqual(len(self.week("2026-11-16", "2026-11-22")), 1)

    def test_next_week_copies_from_latest_week_with_items(self):
        recurring.set_enabled(self.con, "Meetings", True)
        # nothing in the week of 16 Nov; on 23 Nov the source is still the week of 9 Nov
        self.assertEqual(recurring.run(self.con, self.cfg, app.new_id, today=dt.date(2026, 11, 23)), {"Meetings": 2})
        self.assertEqual([i["start_date"] for i in self.week("2026-11-23", "2026-11-29")], ["2026-11-23", "2026-11-25"])

    def test_disabled_group_untouched_and_sunday_weeks(self):
        self.assertEqual(recurring.run(self.con, self.cfg, app.new_id, today=dt.date(2026, 11, 18)), {})
        self.assertEqual(recurring.week_start(dt.date(2026, 11, 18), "sunday"), dt.date(2026, 11, 15))
        self.assertEqual(recurring.week_start(dt.date(2026, 11, 15), "sunday"), dt.date(2026, 11, 15))
        self.assertEqual(recurring.week_start(dt.date(2026, 11, 15), "monday"), dt.date(2026, 11, 9))


class RecurringApiTest(unittest.TestCase):
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

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, method="POST", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            finally:
                e.close()

    def test_toggle_and_state(self):
        grp = self.cfg["groups"][0]
        last_week = (recurring.week_start(dt.date.today()) - dt.timedelta(days=7)).isoformat()
        self.post("/api/items", {"name": "Weekly", "grp": grp, "start_date": last_week})
        code, r = self.post("/api/groups/repeat", {"name": grp, "enabled": True})
        self.assertEqual((code, r["enabled"], r["created"]), (200, True, 1))
        with urllib.request.urlopen(self.base + "/api/state") as resp:
            st = json.loads(resp.read())
        self.assertEqual(st["repeat_groups"], [grp])
        self.assertEqual(sum(1 for i in st["items"] if i["name"] == "Weekly"), 2)
        self.assertEqual(self.post("/api/groups/repeat", {"name": grp, "enabled": False})[1]["created"], 0)
        self.assertEqual(self.post("/api/groups/repeat", {"name": "nope", "enabled": True})[0], 404)


if __name__ == "__main__":
    unittest.main()
