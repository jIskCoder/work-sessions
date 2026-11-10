"""Generate a sample database so you can try Work Sessions without importing anything.

    python3 app.py --demo
"""
import datetime as dt
import random

import app

PROJECTS = {
    "Platform Operations": ["Incident review", "Capacity planning", "Patching"],
    "Data Management": ["Storage onboarding", "Retention policy", "Metadata"],
    "Team Leadership": ["1:1s", "Hiring", "Budget"],
    "Service Desk": ["Tickets", "Drop-in sessions"],
    "Strategy": ["Annual plan", "Stakeholder updates"],
    "Professional Development": ["Learning", "Conferences"],
}
VERBS = ["Review", "Plan", "Draft", "Meet about", "Follow up on", "Prepare", "Sync on", "Update"]


def build(db_path, days=420, seed=7):
    rnd = random.Random(seed)
    con = app.connect(db_path)
    cfg = app.DEFAULT_CONFIG
    today = dt.date.today()
    groups = cfg["groups"]
    con.executemany("INSERT OR IGNORE INTO groups VALUES (?,?)", [(g, i) for i, g in enumerate(groups)])
    n = 0
    for back in range(days, -8, -1):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        clock = dt.datetime.combine(day, dt.time(8, 30)) + dt.timedelta(minutes=rnd.randint(0, 40))
        for _ in range(rnd.randint(2, 5)):
            proj = rnd.choice(list(PROJECTS))
            task = rnd.choice(PROJECTS[proj])
            n += 1
            iid = "D%05d" % n
            future = back < 0
            past = back > 0
            status = "Not started" if future else ("Done" if past or rnd.random() < .4 else "Working on it")
            grp = "To do today" if back in (0, 1) else ("Meetings" if "Meet" in task or "1:1" in task or "Sync" in task
                                                         else ("Done" if status == "Done" and back > 30 else "Tasks"))
            mins = 0 if future else rnd.choice([15, 25, 30, 45, 60, 75, 90, 120, 150])
            start = clock
            end = start + dt.timedelta(minutes=mins)
            clock = end + dt.timedelta(minutes=rnd.randint(5, 50))
            prio = rnd.choices([p["name"] for p in cfg["priorities"]], [1, 3, 5, 12])[0]
            con.execute("INSERT INTO items(id,name,grp,start_date,start_hour,priority,end_date,end_hour,project,task,"
                        "status,tracked_offset,created,position) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                        (iid, "%s %s" % (rnd.choice(VERBS), task.lower()), grp, day.isoformat(),
                         start.strftime("%H:%M"), prio, end.date().isoformat() if status == "Done" else None,
                         end.strftime("%H:%M") if status == "Done" else None, proj, task, status,
                         start.isoformat(), -back))
            if mins:
                con.execute("INSERT INTO sessions(item_id,start,end) VALUES (?,?,?)",
                            (iid, app.iso(start), app.iso(end)))
    con.commit()
    con.close()
