#!/usr/bin/env python3
"""Import a monday.com board export (Excel) into Work Sessions.

In monday: board ⋯ menu → More actions → Export board to Excel. Then:

    pip3 install openpyxl                  # one-off, only needed for importing
    python3 import_monday.py ~/Downloads/My_Board_1234567890.xlsx
    python3 import_monday.py export.xlsx --dry-run          # show the detected column mapping
    python3 import_monday.py export.xlsx --map project="Client" --map task="Workstream"

What is imported
  * every item, in its monday group, with the columns mapped below
  * the "Time tracking" sheet: each timer session, matched back to its item
  * the "updates" sheet: item updates become notes
  * subitems (name, status, date)
  * any other columns are kept per item as read-only "extra" fields

Columns are detected by header name. Override any of them with --map FIELD="Header":
  start_date, start_hour, end_date, end_hour, priority, status, project, task, tracked, id

A data/config.json is written (if absent) with the groups, statuses, priorities and
column labels found in the export, so the app looks like your board.
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app  # noqa: E402

FIELDS = ["start_date", "start_hour", "end_date", "end_hour", "priority", "status",
          "project", "task", "tracked", "id"]

# (field, predicate on lower-cased header); first match wins, in header order
RULES = [
    ("tracked", lambda h: "time tracking" in h or h == "time tracked"),
    ("id", lambda h: h.startswith("item id")),
    ("start_date", lambda h: h in ("start date", "date", "start")),
    ("end_date", lambda h: h in ("end date", "due date", "end")),
    ("start_hour", lambda h: h in ("start hour", "start time")),
    ("end_hour", lambda h: h in ("end hour", "end time")),
    ("priority", lambda h: h == "priority"),
    ("status", lambda h: h == "status"),
    ("project", lambda h: "project" in h),
    ("task", lambda h: "task" in h and "sub" not in h),
]

TONES_STATUS = {"done": "success", "complete": "success", "working": "warning", "progress": "warning",
                "stuck": "critical", "blocked": "critical"}
TONES_PRIORITY = {"critical": "critical", "urgent": "critical", "high": "high", "moderate": "moderate",
                  "medium": "moderate", "low": "low"}


def to_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    s = str(v).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def to_hour(v):
    if v is None or v == "":
        return None
    if isinstance(v, dt.time):
        return v.strftime("%H:%M")
    if isinstance(v, dt.datetime):
        return v.strftime("%H:%M")
    s = str(v).strip()
    for fmt in ("%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return None


def to_secs(v):
    if v is None or v == "":
        return 0
    if isinstance(v, dt.timedelta):
        return int(v.total_seconds())
    if isinstance(v, dt.time):
        return v.hour * 3600 + v.minute * 60 + v.second
    parts = [int(p) for p in str(v).split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return h * 3600 + m * 60 + s


def to_clock(v):
    """'05:00:00 PM' or time -> 'HH:MM:SS'."""
    if isinstance(v, dt.time):
        return v.strftime("%H:%M:%S")
    s = str(v).strip()
    for fmt in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
        try:
            return dt.datetime.strptime(s, fmt).strftime("%H:%M:%S")
        except ValueError:
            pass
    raise ValueError("unrecognised time %r" % v)


def nonempty(row):
    return [v for v in row if v is not None and v != ""]


def detect_mapping(header, overrides):
    mapping = {}
    for f, h in overrides.items():
        if h not in header:
            sys.exit("--map %s=%r: no such column. Columns are: %s" % (f, h, ", ".join(map(str, header))))
        mapping[f] = h
    taken = set(mapping.values())
    for h in header[1:]:
        if h is None or h in taken:
            continue
        hl = str(h).strip().lower()
        for f, pred in RULES:
            if f not in mapping and pred(hl):
                mapping[f] = h
                taken.add(h)
                break
    return mapping


def read_export(path, overrides):
    try:
        import openpyxl
    except ImportError:
        sys.exit("Importing needs openpyxl:  pip3 install openpyxl")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = {ws.title.lower(): ws for ws in wb.worksheets}
    main = wb.worksheets[0]
    board_title = None
    items, subs = [], []
    header = mapping = None
    group = None
    mode = "item"
    last = None
    for row in main.iter_rows(values_only=True):
        r = list(row)
        vals = nonempty(r)
        if not vals:
            continue
        if board_title is None and len(vals) == 1 and header is None and group is None:
            board_title = vals[0]
            group = None
            board_title_seen = True
            continue
        if r[0] == "Subitems" and len(r) > 1 and r[1] == "Name":
            mode = "sub"
            sub_header = r
            continue
        if r[0] == "Name" and len(vals) > 2:
            header = r
            mapping = mapping or detect_mapping(header, overrides)
            mode = "item"
            continue
        if r[0] is None:
            if mode == "sub" and last is not None and len(r) > 1 and r[1]:
                d = dict(zip(sub_header, r))
                subs.append({"parent": last["id"], "name": d.get("Name"), "status": d.get("Status"),
                             "date": to_date(d.get("Date")),
                             "id": str(d.get("Item ID (auto generated)") or "S%d" % len(subs))})
            continue  # group summary rows
        if len(vals) == 1 and isinstance(r[0], str):
            group = r[0].strip()
            mode = "item"
            continue
        if header is None:
            continue
        mode = "item"
        d = dict(zip(header, r))
        get = lambda f: d.get(mapping[f]) if f in mapping else None  # noqa: E731
        iid = str(get("id") or "I%d" % len(items))
        extra = {}
        used = set(mapping.values()) | {"Name", "Subitems"}
        for h, v in d.items():
            if h and h not in used and v not in (None, ""):
                extra[str(h)] = v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else str(v)
        it = {"id": iid, "name": str(r[0]), "group": group,
              "start_date": to_date(get("start_date")), "start_hour": to_hour(get("start_hour")),
              "end_date": to_date(get("end_date")), "end_hour": to_hour(get("end_hour")),
              "priority": get("priority"), "status": get("status"),
              "project": (re.sub(r"\s+", " ", str(get("project"))).strip() if get("project") else None),
              "task": (str(get("task")).strip() if get("task") else None),
              "tracked": to_secs(get("tracked")), "extra": extra}
        items.append(it)
        last = it

    # ---- time tracking sheet: blocks of "<item name>" / header / sessions / Total
    blocks = []
    ts = sheets.get("time tracking")
    if ts is not None:
        cur = None
        for row in ts.iter_rows(values_only=True):
            r = list(row)
            vals = nonempty(r)
            if not vals or r[0] in ("Time Tracking", "Started By"):
                continue
            if r[0] == "Total":
                if cur is not None:
                    cur["total"] = to_secs(r[6] if len(r) > 6 else None)
                continue
            if len(vals) == 1:
                cur = {"name": str(r[0]), "sessions": [], "total": None}
                blocks.append(cur)
                continue
            if cur is None or len(r) < 6 or not r[2] or not r[4]:
                continue
            sd, ed = to_date(r[2]), to_date(r[3] or r[2])
            cur["sessions"].append({"start": "%sT%s" % (sd, to_clock(r[4])),
                                    "end": "%sT%s" % (ed, to_clock(r[5])) if r[5] else None})

    # match blocks to items: same name + same total first, then same name in order
    by_name = collections.defaultdict(list)
    for it in items:
        by_name[it["name"]].append(it)
    used, assign = set(), {}
    for pass_exact in (True, False):
        for bi, b in enumerate(blocks):
            if bi in assign:
                continue
            for it in by_name.get(b["name"], []):
                if it["id"] in used:
                    continue
                if pass_exact and (b["total"] is None or abs(it["tracked"] - b["total"]) > 2):
                    continue
                used.add(it["id"])
                assign[bi] = it
                break
    # names monday truncated in the sheet
    for bi, b in enumerate(blocks):
        if bi in assign:
            continue
        for it in items:
            if it["id"] not in used and (it["name"].startswith(b["name"]) or b["name"].startswith(it["name"])):
                used.add(it["id"])
                assign[bi] = it
                break
    sessions, unmatched = [], []
    for bi, b in enumerate(blocks):
        if bi not in assign:
            unmatched.append(b["name"])
            continue
        for s in b["sessions"]:
            sessions.append(dict(item_id=assign[bi]["id"], **s))

    # ---- updates sheet -> notes
    notes = []
    up = sheets.get("updates")
    if up is not None:
        uh = None
        for row in up.iter_rows(values_only=True):
            r = list(row)
            if r and r[0] == "Item ID":
                uh = r
                continue
            if uh is None or not r or not r[0]:
                continue
            d = dict(zip(uh, r))
            created = None
            raw = d.get("Created At")
            if isinstance(raw, dt.datetime):
                created = raw.isoformat()
            elif raw:
                try:
                    created = dt.datetime.strptime(re.sub(r"\s+", " ", str(raw)),
                                                   "%d/%B/%Y %I:%M:%S %p").isoformat()
                except ValueError:
                    created = None
            notes.append({"item_id": str(d["Item ID"]), "created": created, "body": d.get("Update Content")})

    return {"title": board_title, "items": items, "subitems": subs, "sessions": sessions,
            "notes": notes, "mapping": mapping or {}, "unmatched": unmatched}


def tone(name, table, default):
    n = (name or "").lower()
    for k, v in table.items():
        if k in n:
            return v
    return default


def write_config(ddir, data):
    path = os.path.join(ddir, "config.json")
    if os.path.exists(path):
        print("Kept existing %s" % path)
        return
    items = data["items"]
    groups = []
    for it in items:
        if it["group"] and it["group"] not in groups:
            groups.append(it["group"])
    statuses = [s for s, _ in collections.Counter(i["status"] for i in items if i["status"]).most_common()]
    order = {"success": 2, "warning": 1}
    statuses.sort(key=lambda s: order.get(tone(s, TONES_STATUS, "neutral"), 0))
    prios = [p for p, _ in collections.Counter(i["priority"] for i in items if i["priority"]).most_common()]
    prank = {"critical": 0, "high": 1, "moderate": 2, "low": 3}
    prios.sort(key=lambda p: prank.get(tone(p, TONES_PRIORITY, "low"), 4))
    done = next((s for s in statuses if tone(s, TONES_STATUS, "") == "success"), "Done")
    started = next((s for s in statuses if tone(s, TONES_STATUS, "") == "warning"), "Working on it")
    notstarted = next((s for s in statuses if tone(s, TONES_STATUS, "neutral") == "neutral"), statuses[0] if statuses else "Not started")
    m = data["mapping"]
    cfg = {
        "title": data["title"] or "Work Sessions",
        "labels": {"project": m.get("project") or "Project", "task": m.get("task") or "Task"},
        "groups": groups or app.DEFAULT_CONFIG["groups"],
        "default_group": groups[0] if groups else app.DEFAULT_CONFIG["default_group"],
        "statuses": [{"name": s, "tone": tone(s, TONES_STATUS, "neutral")} for s in statuses] or app.DEFAULT_CONFIG["statuses"],
        "default_status": notstarted, "start_status": started, "done_status": done,
        "priorities": [{"name": p, "tone": tone(p, TONES_PRIORITY, "low")} for p in prios] or app.DEFAULT_CONFIG["priorities"],
        "default_priority": prios[-1] if prios else "Low",
    }
    last_year = str(dt.date.today().year - 1)
    first = min((i["start_date"] for i in items if (i["start_date"] or "").startswith(last_year)), default=None)
    if first:
        cfg["history_year_from_month"] = int(first[5:7])
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    print("Wrote %s" % path)


def write_db(db_path, data):
    con = app.connect(db_path)
    secs = collections.Counter()
    t = app.now()
    for s in data["sessions"]:
        end = app.parse(s["end"]) if s["end"] else t
        secs[s["item_id"]] += int((end - app.parse(s["start"])).total_seconds())
    for pos, it in enumerate(data["items"]):
        offset = it["tracked"] - secs.get(it["id"], 0) if it["tracked"] else 0  # keep monday's per-item totals
        con.execute("INSERT OR REPLACE INTO items(id,name,grp,start_date,start_hour,priority,end_date,end_hour,"
                    "project,task,status,tracked_offset,created,position,extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (it["id"], it["name"], it["group"], it["start_date"], it["start_hour"], it["priority"],
                     it["end_date"], it["end_hour"], it["project"], it["task"], it["status"], offset,
                     it["start_date"], pos, json.dumps(it["extra"]) if it["extra"] else None))
    con.executemany("INSERT INTO sessions(item_id,start,end) VALUES (?,?,?)",
                    [(s["item_id"], s["start"], s["end"]) for s in data["sessions"]])
    con.executemany("INSERT INTO notes(item_id,created,body) VALUES (?,?,?)",
                    [(n["item_id"], n["created"], n["body"]) for n in data["notes"]])
    con.executemany("INSERT OR REPLACE INTO subitems VALUES (?,?,?,?,?)",
                    [(s["id"], s["parent"], s["name"], s["status"], s["date"]) for s in data["subitems"]])
    groups = []
    for it in data["items"]:
        if it["group"] and it["group"] not in groups:
            groups.append(it["group"])
    con.executemany("INSERT OR IGNORE INTO groups VALUES (?,?)", [(g, i) for i, g in enumerate(groups)])
    con.commit()
    con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xlsx")
    ap.add_argument("--data", help="data folder (default ./data or $WORKSESSIONS_DATA)")
    ap.add_argument("--map", action="append", default=[], metavar='FIELD="Header"')
    ap.add_argument("--dry-run", action="store_true", help="only show what would be imported")
    ap.add_argument("--replace", action="store_true",
                    help="if a database exists, move it to backups/ and import fresh")
    args = ap.parse_args(argv)
    overrides = {}
    for m in args.map:
        k, _, v = m.partition("=")
        if k not in FIELDS:
            sys.exit("--map: unknown field %r (choose from %s)" % (k, ", ".join(FIELDS)))
        overrides[k] = v.strip('"')
    data = read_export(args.xlsx, overrides)
    print("Board: %s" % data["title"])
    print("Column mapping:")
    for f in FIELDS:
        print("  %-11s <- %s" % (f, data["mapping"].get(f, "(not found)")))
    total = sum(it["tracked"] for it in data["items"]) / 3600
    print("Found %d items in %d groups, %d timer sessions, %d notes, %d subitems (%.1f h tracked)." % (
        len(data["items"]), len({i["group"] for i in data["items"]}), len(data["sessions"]),
        len(data["notes"]), len(data["subitems"]), total))
    if data["unmatched"]:
        print("Note: %d time-tracking blocks could not be matched to an item: %s" % (
            len(data["unmatched"]), ", ".join(data["unmatched"][:5])))
    if args.dry_run:
        return
    ddir = app.data_dir(args.data)
    db_path = os.path.join(ddir, "worksessions.db")
    if os.path.exists(db_path):
        if not args.replace:
            sys.exit("%s already exists. Use --replace to move it to backups/ and import fresh." % db_path)
        os.makedirs(os.path.join(ddir, "backups"), exist_ok=True)
        moved = os.path.join(ddir, "backups", "worksessions-before-import-%s.db" % app.now().strftime("%Y%m%d-%H%M%S"))
        shutil.move(db_path, moved)
        print("Moved the old database to %s" % moved)
    write_db(db_path, data)
    write_config(ddir, data)
    print("Imported into %s. Start the app with:  python3 app.py" % db_path)


if __name__ == "__main__":
    main()
