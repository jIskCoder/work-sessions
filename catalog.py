"""Projects and tasks — the lists that items' Project and Task fields pick from.

Mirrors monday.com's linked "My Projects" / "My Tasks" boards: every task belongs to
(at most) one project, and choosing a task on an item fills in that task's project.

Items keep plain-text ``project`` / ``task`` values, so the report, exports and older
databases keep working; renaming a project or task here updates every item that uses it.
"""
import collections

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, status TEXT,
  start_date TEXT, end_date TEXT, position INTEGER);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, project TEXT, status TEXT,
  position INTEGER);
"""

PROJECT_FIELDS = ("name", "status", "start_date", "end_date")
TASK_FIELDS = ("name", "project", "status")


class CatalogError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def clean(name):
    """Trim and collapse whitespace; empty -> None."""
    if name is None:
        return None
    name = " ".join(str(name).split())
    return name or None


def init(con):
    con.executescript(SCHEMA)


def _next_pos(con, table):
    return con.execute("SELECT COALESCE(MAX(position),-1)+1 FROM %s" % table).fetchone()[0]


def ensure_project(con, name):
    name = clean(name)
    if name and not con.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        con.execute("INSERT INTO projects(name,position) VALUES (?,?)", (name, _next_pos(con, "projects")))
    return name


def ensure_task(con, name, project=None):
    """Create the task if new (linked to ``project``). Returns (name, its project)."""
    name = clean(name)
    if not name:
        return None, None
    row = con.execute("SELECT project FROM tasks WHERE name=?", (name,)).fetchone()
    if row:
        return name, row[0]
    project = ensure_project(con, project)
    con.execute("INSERT INTO tasks(name,project,position) VALUES (?,?,?)",
                (name, project, _next_pos(con, "tasks")))
    return name, project


def sync_from_items(con):
    """Add any project/task used by items but missing from the lists.

    A task's project is the one its items use most often. Existing entries are never changed.
    """
    usage = collections.defaultdict(collections.Counter)
    projects = []
    for task, project in con.execute("SELECT task, project FROM items ORDER BY position, rowid"):
        project = clean(project)
        if project and project not in projects:
            projects.append(project)
        task = clean(task)
        if task:
            usage[task][project] += 1
    for p in projects:
        ensure_project(con, p)
    for task, counts in usage.items():
        named = [(n, p) for p, n in counts.items() if p]
        best = max(named)[1] if named else None
        ensure_task(con, task, best)
    con.commit()


def link_item_fields(con, sets, current=None):
    """Apply the task -> project rule to an item insert/update (``sets`` is modified).

    * Setting a known task fills in its project (unless a project is given explicitly).
    * A new task name is added to the list, linked to the item's project.
    * A new project name is added to the list.
    """
    current = current or {}
    if "project" in sets:
        sets["project"] = ensure_project(con, sets["project"])
    if "task" in sets:
        project_given = "project" in sets
        project = sets["project"] if project_given else current.get("project")
        name, task_project = ensure_task(con, sets["task"], project)
        sets["task"] = name
        if name and task_project and not project_given:
            sets["project"] = task_project
    return sets


def listing(con):
    projects = [dict(r) for r in con.execute(
        "SELECT id,name,status,start_date,end_date FROM projects ORDER BY position, id")]
    tasks = [dict(r) for r in con.execute(
        "SELECT id,name,project,status FROM tasks ORDER BY position, id")]
    return projects, tasks


# ------------------------------------------------------------------ edits
def _row(con, table, rid):
    r = con.execute("SELECT * FROM %s WHERE id=?" % table, (rid,)).fetchone()
    if not r:
        raise CatalogError(404, "not found")
    return dict(r)


def _unique(con, table, name, rid=None):
    r = con.execute("SELECT id FROM %s WHERE name=?" % table, (name,)).fetchone()
    if r and r[0] != rid:
        raise CatalogError(409, "%s “%s” already exists" % (table[:-1], name))


def create_project(con, data):
    name = clean(data.get("name"))
    if not name:
        raise CatalogError(400, "name required")
    _unique(con, "projects", name)
    con.execute("INSERT INTO projects(name,status,start_date,end_date,position) VALUES (?,?,?,?,?)",
                (name, data.get("status"), data.get("start_date"), data.get("end_date"), _next_pos(con, "projects")))
    con.commit()
    return dict(con.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone())


def update_project(con, rid, data):
    old = _row(con, "projects", rid)
    sets = {k: data[k] for k in PROJECT_FIELDS if k in data}
    if "name" in sets:
        sets["name"] = clean(sets["name"])
        if not sets["name"]:
            raise CatalogError(400, "name required")
        _unique(con, "projects", sets["name"], rid)
    if sets:
        con.execute("UPDATE projects SET %s WHERE id=?" % ",".join("%s=?" % k for k in sets),
                    list(sets.values()) + [rid])
        if sets.get("name") and sets["name"] != old["name"]:
            con.execute("UPDATE items SET project=? WHERE project=?", (sets["name"], old["name"]))
            con.execute("UPDATE tasks SET project=? WHERE project=?", (sets["name"], old["name"]))
        con.commit()
    return _row(con, "projects", rid)


def delete_project(con, rid):
    row = _row(con, "projects", rid)
    used = con.execute("SELECT (SELECT COUNT(*) FROM items WHERE project=?)+(SELECT COUNT(*) FROM tasks WHERE project=?)",
                       (row["name"], row["name"])).fetchone()[0]
    if used:
        raise CatalogError(409, "“%s” is still used by tasks or items" % row["name"])
    con.execute("DELETE FROM projects WHERE id=?", (rid,))
    con.commit()


def create_task(con, data):
    name = clean(data.get("name"))
    if not name:
        raise CatalogError(400, "name required")
    _unique(con, "tasks", name)
    project = ensure_project(con, data.get("project"))
    con.execute("INSERT INTO tasks(name,project,status,position) VALUES (?,?,?,?)",
                (name, project, data.get("status"), _next_pos(con, "tasks")))
    con.commit()
    return dict(con.execute("SELECT * FROM tasks WHERE name=?", (name,)).fetchone())


def update_task(con, rid, data):
    """Renaming updates every item with that task; moving a task to another project
    moves its items too (like monday's mirrored project column)."""
    old = _row(con, "tasks", rid)
    sets = {k: data[k] for k in TASK_FIELDS if k in data}
    if "name" in sets:
        sets["name"] = clean(sets["name"])
        if not sets["name"]:
            raise CatalogError(400, "name required")
        _unique(con, "tasks", sets["name"], rid)
    if "project" in sets:
        sets["project"] = ensure_project(con, sets["project"])
    if sets:
        con.execute("UPDATE tasks SET %s WHERE id=?" % ",".join("%s=?" % k for k in sets),
                    list(sets.values()) + [rid])
        new_name = sets.get("name") or old["name"]
        if new_name != old["name"]:
            con.execute("UPDATE items SET task=? WHERE task=?", (new_name, old["name"]))
        if "project" in sets and sets["project"] != old["project"]:
            con.execute("UPDATE items SET project=? WHERE task=?", (sets["project"], new_name))
        con.commit()
    return _row(con, "tasks", rid)


def delete_task(con, rid):
    row = _row(con, "tasks", rid)
    if con.execute("SELECT 1 FROM items WHERE task=?", (row["name"],)).fetchone():
        raise CatalogError(409, "“%s” is still used by items" % row["name"])
    con.execute("DELETE FROM tasks WHERE id=?", (rid,))
    con.commit()


def upsert(con, projects=(), tasks=()):
    """Bulk add/update from an import (by name). Existing items are not changed."""
    for p in projects:
        name = clean(p.get("name"))
        if not name:
            continue
        ensure_project(con, name)
        con.execute("UPDATE projects SET status=COALESCE(?,status), start_date=COALESCE(?,start_date), "
                    "end_date=COALESCE(?,end_date) WHERE name=?",
                    (p.get("status"), p.get("start_date"), p.get("end_date"), name))
    for t in tasks:
        name = clean(t.get("name"))
        if not name:
            continue
        project = ensure_project(con, t.get("project"))
        ensure_task(con, name, project)
        con.execute("UPDATE tasks SET project=COALESCE(?,project), status=COALESCE(?,status) WHERE name=?",
                    (project, t.get("status"), name))
    con.commit()
