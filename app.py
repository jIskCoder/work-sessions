#!/usr/bin/env python3
"""Work Sessions — a small, local time-tracking board with a report dashboard.

A free, self-hosted alternative to a monday.com time-tracking board. It runs on
your own machine with nothing but the Python standard library.

    python3 app.py                 # start and open the browser
    python3 app.py --demo          # start with generated sample data (separate demo database)
    python3 app.py --port 9000 --no-browser
    python3 app.py --data /path/to/folder

Data lives in ./data (or --data / WORKSESSIONS_DATA): worksessions.db (SQLite),
config.json (your groups, statuses, labels) and backups/.
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import shutil
import sqlite3
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

__version__ = "1.0.0"

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

ITEM_FIELDS = ["name", "grp", "start_date", "start_hour", "priority", "end_date",
               "end_hour", "project", "task", "status"]

DEFAULT_CONFIG = {
    "title": "Work Sessions",
    "port": 8765,
    "week_starts": "monday",            # or "sunday"
    "labels": {"project": "Project", "task": "Task"},
    "groups": ["To do today", "Tasks", "Meetings", "Done"],
    "default_group": "To do today",
    "statuses": [
        {"name": "Not started", "tone": "neutral"},
        {"name": "Working on it", "tone": "warning"},
        {"name": "Done", "tone": "success"},
    ],
    "default_status": "Not started",
    "start_status": "Working on it",     # set when a timer starts on a not-started item
    "done_status": "Done",               # stops timers and stamps the end date/hour
    "priorities": [
        {"name": "Critical", "tone": "critical"},
        {"name": "High", "tone": "high"},
        {"name": "Medium", "tone": "moderate"},
        {"name": "Low", "tone": "low"},
    ],
    "default_priority": "Low",
    "report_basis": "start",            # "start" (monday's dashboard rule) or "logged"
    "history_year_from_month": 1,       # first month shown on the previous-year chart (1-12)
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, grp TEXT, start_date TEXT, start_hour TEXT,
  priority TEXT, end_date TEXT, end_hour TEXT, project TEXT, task TEXT, status TEXT,
  tracked_offset INTEGER NOT NULL DEFAULT 0, created TEXT, position INTEGER, extra TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, start TEXT NOT NULL, end TEXT);
CREATE INDEX IF NOT EXISTS ix_sessions_item ON sessions(item_id);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, created TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS subitems (
  id TEXT PRIMARY KEY, parent TEXT NOT NULL, name TEXT, status TEXT, date TEXT);
CREATE TABLE IF NOT EXISTS groups (name TEXT PRIMARY KEY, position INTEGER);
"""

lock = threading.Lock()


# ---------------------------------------------------------------- utilities
def now():
    return dt.datetime.now().replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def parse(s):
    return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def data_dir(arg=None):
    d = arg or os.environ.get("WORKSESSIONS_DATA") or os.path.join(HERE, "data")
    os.makedirs(d, exist_ok=True)
    return d


def load_config(ddir):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = os.path.join(ddir, "config.json")
    if os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def connect(db_path):
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(items)")]
    if "extra" not in cols:  # databases from v0
        con.execute("ALTER TABLE items ADD COLUMN extra TEXT")
    return con


def ensure_groups(con, cfg):
    have = {r[0] for r in con.execute("SELECT name FROM groups")}
    pos = con.execute("SELECT COALESCE(MAX(position),-1) FROM groups").fetchone()[0]
    for g in cfg["groups"]:
        if g not in have:
            pos += 1
            con.execute("INSERT INTO groups VALUES (?,?)", (g, pos))
    for (g,) in con.execute("SELECT DISTINCT grp FROM items WHERE grp IS NOT NULL").fetchall():
        if g not in have and g not in cfg["groups"]:
            pos += 1
            con.execute("INSERT OR IGNORE INTO groups VALUES (?,?)", (g, pos))
    con.commit()


def backup(db_path, ddir):
    """Keep one copy per day in <data>/backups. Never deletes anything."""
    if not os.path.exists(db_path):
        return
    bdir = os.path.join(ddir, "backups")
    os.makedirs(bdir, exist_ok=True)
    target = os.path.join(bdir, "worksessions-%s.db" % dt.date.today().isoformat())
    if not os.path.exists(target):
        shutil.copy2(db_path, target)


def item_row(con, item_id):
    r = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return dict(r) if r else None


def new_id(con):
    base = int(now().timestamp() * 1000)
    while con.execute("SELECT 1 FROM items WHERE id=?", ("L%d" % base,)).fetchone():
        base += 1
    return "L%d" % base


def state(con, cfg):
    items = [dict(r) for r in con.execute("SELECT * FROM items ORDER BY position, rowid")]
    sessions = [dict(r) for r in con.execute("SELECT id,item_id,start,end FROM sessions")]
    groups = [r["name"] for r in con.execute("SELECT name FROM groups ORDER BY position")]
    notes = {r[0]: r[1] for r in con.execute("SELECT item_id, COUNT(*) FROM notes GROUP BY item_id")}
    for it in items:
        it["notes"] = notes.get(it["id"], 0)
        it.pop("extra", None)
    return {"items": items, "sessions": sessions, "groups": groups, "now": iso(now()),
            "config": cfg, "version": __version__}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    con = None
    cfg = None

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def parts(self):
        return urlparse(self.path).path.strip("/").split("/")

    # ---- GET
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.static("index.html")
        if path.startswith("/static/"):
            return self.static(path[len("/static/"):])
        con = self.con
        with lock:
            if path == "/api/state":
                return self.send(200, state(con, self.cfg))
            p = self.parts()
            if len(p) == 4 and p[:2] == ["api", "items"] and p[3] == "detail":
                iid = p[2]
                it = item_row(con, iid)
                if not it:
                    return self.send(404, {"error": "no item"})
                it["extra"] = json.loads(it["extra"]) if it.get("extra") else {}
                return self.send(200, {
                    "item": it,
                    "sessions": [dict(r) for r in con.execute(
                        "SELECT * FROM sessions WHERE item_id=? ORDER BY start DESC", (iid,))],
                    "notes": [dict(r) for r in con.execute(
                        "SELECT * FROM notes WHERE item_id=? ORDER BY created DESC", (iid,))],
                    "subitems": [dict(r) for r in con.execute(
                        "SELECT * FROM subitems WHERE parent=?", (iid,))]})
            if path == "/api/export/items.csv":
                return self.csv_items()
            if path == "/api/export/sessions.csv":
                return self.csv_sessions()
        self.send(404, {"error": "not found"})

    def static(self, name):
        fp = os.path.normpath(os.path.join(STATIC, name))
        if not fp.startswith(STATIC) or not os.path.isfile(fp):
            return self.send(404, {"error": "not found"})
        ctype = {"html": "text/html; charset=utf-8", "js": "application/javascript",
                 "css": "text/css", "png": "image/png"}.get(fp.rsplit(".", 1)[-1], "application/octet-stream")
        with open(fp, "rb") as f:
            self.send(200, f.read(), ctype)

    # ---- POST
    def do_POST(self):
        path = urlparse(self.path).path
        p = self.parts()
        data = self.body()
        con, cfg = self.con, self.cfg
        with lock:
            if path == "/api/items":
                iid = new_id(con)
                t = now()
                vals = {f: data.get(f) for f in ITEM_FIELDS}
                vals["name"] = vals["name"] or "New item"
                vals["grp"] = vals["grp"] or cfg["default_group"]
                vals["status"] = vals["status"] or cfg["default_status"]
                vals["priority"] = vals["priority"] or cfg["default_priority"]
                vals["start_date"] = vals["start_date"] or t.date().isoformat()
                vals["start_hour"] = vals["start_hour"] or t.strftime("%H:%M")
                top = con.execute("SELECT COALESCE(MIN(position),0)-1 FROM items").fetchone()[0]
                con.execute("INSERT INTO items(id,%s,tracked_offset,created,position) VALUES (?,%s,0,?,?)"
                            % (",".join(ITEM_FIELDS), ",".join("?" * len(ITEM_FIELDS))),
                            [iid] + [vals[f] for f in ITEM_FIELDS] + [iso(t), top])
                con.execute("INSERT OR IGNORE INTO groups VALUES (?,(SELECT COALESCE(MAX(position),0)+1 FROM groups))",
                            (vals["grp"],))
                con.commit()
                return self.send(200, item_row(con, iid))
            if path == "/api/groups":
                name = (data.get("name") or "").strip()
                if name:
                    pos = con.execute("SELECT COALESCE(MIN(position),0)-1 FROM groups").fetchone()[0]
                    con.execute("INSERT OR IGNORE INTO groups VALUES (?,?)", (name, pos))
                    con.commit()
                return self.send(200, {"ok": True})
            if len(p) == 4 and p[:2] == ["api", "items"]:
                iid, action = p[2], p[3]
                it = item_row(con, iid)
                if not it:
                    return self.send(404, {"error": "no item"})
                t = now()
                if action == "start":
                    if not con.execute("SELECT 1 FROM sessions WHERE item_id=? AND end IS NULL", (iid,)).fetchone():
                        con.execute("INSERT INTO sessions(item_id,start,end) VALUES (?,?,NULL)", (iid, iso(t)))
                    if it["status"] in (None, "", cfg["default_status"]):
                        con.execute("UPDATE items SET status=? WHERE id=?", (cfg["start_status"], iid))
                elif action == "stop":
                    con.execute("UPDATE sessions SET end=? WHERE item_id=? AND end IS NULL", (iso(t), iid))
                elif action == "duplicate":
                    nid = new_id(con)
                    con.execute("INSERT INTO items(id,name,grp,start_date,start_hour,priority,end_date,end_hour,"
                                "project,task,status,tracked_offset,created,position) "
                                "SELECT ?,name||' (copy)',grp,?,?,priority,NULL,NULL,project,task,?,0,?,"
                                "(SELECT COALESCE(MIN(position),0)-1 FROM items) FROM items WHERE id=?",
                                (nid, t.date().isoformat(), t.strftime("%H:%M"), cfg["default_status"], iso(t), iid))
                    con.commit()
                    return self.send(200, item_row(con, nid))
                elif action == "notes":
                    con.execute("INSERT INTO notes(item_id,created,body) VALUES (?,?,?)",
                                (iid, iso(t), data.get("body", "")))
                elif action == "sessions":
                    if not data.get("start"):
                        return self.send(400, {"error": "start required"})
                    con.execute("INSERT INTO sessions(item_id,start,end) VALUES (?,?,?)",
                                (iid, data["start"], data.get("end")))
                elif action == "subitems":
                    con.execute("INSERT INTO subitems VALUES (?,?,?,?,?)",
                                (new_id(con), iid, data.get("name", ""), data.get("status"), data.get("date")))
                else:
                    return self.send(404, {"error": "unknown action"})
                con.commit()
                return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    # ---- PATCH
    def do_PATCH(self):
        p = self.parts()
        data = self.body()
        con, cfg = self.con, self.cfg
        with lock:
            if len(p) == 3 and p[:2] == ["api", "items"]:
                iid = p[2]
                sets = {f: data[f] for f in ITEM_FIELDS if f in data}
                if sets.get("status") and sets["status"] == cfg["done_status"]:
                    it = item_row(con, iid)
                    t = now()
                    if it and not it["end_date"]:
                        sets.setdefault("end_date", t.date().isoformat())
                        sets.setdefault("end_hour", t.strftime("%H:%M"))
                    con.execute("UPDATE sessions SET end=? WHERE item_id=? AND end IS NULL", (iso(t), iid))
                if sets:
                    con.execute("UPDATE items SET %s WHERE id=?" % ",".join("%s=?" % k for k in sets),
                                list(sets.values()) + [iid])
                    if sets.get("grp"):
                        con.execute("INSERT OR IGNORE INTO groups VALUES (?,(SELECT COALESCE(MAX(position),0)+1 FROM groups))",
                                    (sets["grp"],))
                    con.commit()
                return self.send(200, item_row(con, iid))
            if len(p) == 3 and p[1] in ("sessions", "subitems"):
                allowed = ("start", "end") if p[1] == "sessions" else ("name", "status", "date")
                sets = {k: data[k] for k in allowed if k in data}
                if sets:
                    con.execute("UPDATE %s SET %s WHERE id=?" % (p[1], ",".join("%s=?" % k for k in sets)),
                                list(sets.values()) + [p[2]])
                    con.commit()
                return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    # ---- DELETE
    def do_DELETE(self):
        p = self.parts()
        con = self.con
        with lock:
            if len(p) == 3 and p[1] in ("items", "sessions", "notes", "subitems"):
                key = p[2]
                if p[1] == "items":
                    con.execute("DELETE FROM sessions WHERE item_id=?", (key,))
                    con.execute("DELETE FROM notes WHERE item_id=?", (key,))
                    con.execute("DELETE FROM subitems WHERE parent=?", (key,))
                    con.execute("DELETE FROM items WHERE id=?", (key,))
                else:
                    con.execute("DELETE FROM %s WHERE id=?" % p[1], (key,))
                con.commit()
                return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    # ---- CSV exports
    def csv_out(self, header, rows, fname):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        body = buf.getvalue().encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % fname)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def csv_items(self):
        con, cfg = self.con, self.cfg
        secs = {}
        t = now()
        for r in con.execute("SELECT item_id,start,end FROM sessions"):
            e = parse(r["end"]) if r["end"] else t
            secs[r["item_id"]] = secs.get(r["item_id"], 0) + (e - parse(r["start"])).total_seconds()
        rows = []
        for r in con.execute("SELECT * FROM items ORDER BY position, rowid"):
            hrs = (secs.get(r["id"], 0) + r["tracked_offset"]) / 3600
            rows.append([r["name"], r["grp"], r["start_date"], r["start_hour"], r["priority"],
                         r["end_date"], r["end_hour"], round(hrs, 4), r["project"], r["task"],
                         r["status"], r["id"]])
        self.csv_out(["Name", "Group", "Start Date", "Start Hour", "Priority", "End Date", "End Hour",
                      "Hours", cfg["labels"]["project"], cfg["labels"]["task"], "Status", "Item ID"], rows,
                     "work-sessions-items-%s.csv" % t.date())

    def csv_sessions(self):
        con, cfg = self.con, self.cfg
        rows = [[r["name"], r["project"], r["task"], r["start"], r["end"]] for r in con.execute(
            "SELECT i.name,i.project,i.task,s.start,s.end FROM sessions s JOIN items i ON i.id=s.item_id ORDER BY s.start")]
        self.csv_out(["Item", cfg["labels"]["project"], cfg["labels"]["task"], "Start", "End"], rows,
                     "work-sessions-log-%s.csv" % now().date())


def make_server(ddir, port=None, db_name="worksessions.db"):
    cfg = load_config(ddir)
    db_path = os.path.join(ddir, db_name)
    backup(db_path, ddir)
    con = connect(db_path)
    ensure_groups(con, cfg)
    handler = type("BoundHandler", (Handler,), {"con": con, "cfg": cfg})
    srv = ThreadingHTTPServer(("127.0.0.1", port if port is not None else cfg["port"]), handler)
    return srv, cfg, con


def main(argv=None):
    ap = argparse.ArgumentParser(description="Work Sessions — local time-tracking board")
    ap.add_argument("--port", type=int)
    ap.add_argument("--data", help="data folder (default ./data or $WORKSESSIONS_DATA)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--demo", action="store_true", help="use generated sample data (demo.db)")
    args = ap.parse_args(argv)
    ddir = data_dir(args.data)
    db_name = "worksessions.db"
    if args.demo:
        import demo_data
        db_name = "demo.db"
        if not os.path.exists(os.path.join(ddir, db_name)):
            demo_data.build(os.path.join(ddir, db_name))
    srv, cfg, _ = make_server(ddir, args.port, db_name)
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    print("%s running at %s  (Ctrl+C to stop)\nData: %s" % (cfg["title"], url, os.path.join(ddir, db_name)))
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
