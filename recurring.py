"""Weekly repeating groups — like a monday.com "every week, duplicate this group" automation.

A group marked "repeat weekly" gets its previous week's items copied into each new week:
same name, weekday, start hour, priority, project and task; fresh status, no tracked time,
no end date. Items already in the new week (same name, same group) are not duplicated, and each
week is filled only once, so a copy you delete doesn't come back.

Weeks follow the ``week_starts`` setting (Monday by default).
"""
import datetime as dt

SCHEMA = """
CREATE TABLE IF NOT EXISTS group_repeats (
  grp TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS group_repeat_runs (
  grp TEXT NOT NULL, week TEXT NOT NULL, created INTEGER NOT NULL DEFAULT 0, ran_at TEXT,
  PRIMARY KEY (grp, week));
"""


def init(con):
    con.executescript(SCHEMA)


def week_start(day, starts="monday"):
    offset = (day.weekday() + 1) % 7 if starts == "sunday" else day.weekday()
    return day - dt.timedelta(days=offset)


def enabled_groups(con):
    return [r[0] for r in con.execute("SELECT grp FROM group_repeats WHERE enabled=1 ORDER BY grp")]


def set_enabled(con, grp, on):
    con.execute("INSERT INTO group_repeats(grp,enabled) VALUES (?,?) "
                "ON CONFLICT(grp) DO UPDATE SET enabled=excluded.enabled", (grp, 1 if on else 0))
    con.commit()


def _source_week(con, grp, this_week):
    """The most recent week before ``this_week`` that has items in the group."""
    row = con.execute("SELECT MAX(start_date) FROM items WHERE grp=? AND start_date < ?",
                      (grp, this_week.isoformat())).fetchone()
    return row[0]


def run(con, cfg, new_id, today=None, groups=None):
    """Fill the current week for every repeating group that hasn't been filled yet.

    Returns {group: number of items created}. ``new_id`` makes item ids (from app.py).
    """
    today = today or dt.date.today()
    starts = cfg.get("week_starts", "monday")
    this_week = week_start(today, starts)
    week_key = this_week.isoformat()
    out = {}
    for grp in (groups if groups is not None else enabled_groups(con)):
        if con.execute("SELECT 1 FROM group_repeat_runs WHERE grp=? AND week=?", (grp, week_key)).fetchone():
            continue
        latest = _source_week(con, grp, this_week)
        created = 0
        if latest:
            src_start = week_start(dt.date.fromisoformat(latest), starts)
            shift = this_week - src_start
            src_end = src_start + dt.timedelta(days=6)
            existing = {r[0].strip().lower() for r in con.execute(
                "SELECT name FROM items WHERE grp=? AND start_date BETWEEN ? AND ?",
                (grp, week_key, (this_week + dt.timedelta(days=6)).isoformat()))}
            top = con.execute("SELECT COALESCE(MIN(position),0) FROM items").fetchone()[0]
            rows = con.execute(
                "SELECT * FROM items WHERE grp=? AND start_date BETWEEN ? AND ? ORDER BY start_date, start_hour, position",
                (grp, src_start.isoformat(), src_end.isoformat())).fetchall()
            for r in reversed(rows):  # so the earliest ends up first on the board
                key = r["name"].strip().lower()
                if key in existing:
                    continue
                existing.add(key)
                top -= 1
                new_start = (dt.date.fromisoformat(r["start_date"]) + shift).isoformat()
                con.execute(
                    "INSERT INTO items(id,name,grp,start_date,start_hour,priority,end_date,end_hour,project,task,"
                    "status,tracked_offset,created,position) VALUES (?,?,?,?,?,?,NULL,NULL,?,?,?,0,?,?)",
                    (new_id(con), r["name"], grp, new_start, r["start_hour"], r["priority"], r["project"],
                     r["task"], cfg.get("default_status"),
                     dt.datetime.now().replace(microsecond=0).isoformat(), top))
                created += 1
        con.execute("INSERT INTO group_repeat_runs(grp,week,created,ran_at) VALUES (?,?,?,?)",
                    (grp, week_key, created, dt.datetime.now().replace(microsecond=0).isoformat()))
        out[grp] = created
    con.commit()
    return out
