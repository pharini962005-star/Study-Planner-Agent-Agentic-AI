"""Tools the Study Planner Agent can call: files, calculator, dates, and a SQLite schedule with reminders.

Every tool validates its input. Expected problems raise ToolError/PlanError, which run_tool turns into an
"Error: ..." string so the model can read the reason and recover instead of crashing the agent.
"""
import ast
import inspect
import json
import math
import operator
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import planner
from planner import PlanError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.getenv("PLANNER_DATA_DIR") or os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "planner.db")
READ_EXTS = (".txt", ".md", ".csv", ".pdf")
WRITE_EXTS = (".txt", ".md", ".csv")
OVERWRITE_EXTS = (".md", ".csv")  # generated outputs; .txt/.pdf syllabi are never overwritten
MAX_FILE_BYTES = 2_000_000
MAX_TEXT_CHARS = 20_000
LIST_LIMIT = 60


class ToolError(Exception):
    """An expected problem (bad input, missing file, ...) reported back to the model."""


# ---------- Database ----------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, topic TEXT,
    study_date TEXT, hours REAL, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT, remind_at TEXT, subject TEXT);
CREATE TABLE IF NOT EXISTS subjects(name TEXT PRIMARY KEY, exam_date TEXT, hours_per_day REAL,
    revision_days INTEGER, reminder_time TEXT, units TEXT);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(study_date);
"""


@contextmanager
def _db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        if "subject" not in {r[1] for r in conn.execute("PRAGMA table_info(reminders)")}:
            # Old databases: add the column and drop legacy auto-generated reminders (they are rebuilt on re-plan).
            conn.execute("ALTER TABLE reminders ADD COLUMN subject TEXT")
            conn.execute("DELETE FROM reminders WHERE message LIKE 'Study %' OR message LIKE '% EXAM today%'")
        if "level" not in {r[1] for r in conn.execute("PRAGMA table_info(subjects)")}:
            conn.execute("ALTER TABLE subjects ADD COLUMN level INTEGER")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- Input helpers ----------
def _clean_text(value, field, max_len=200):
    text = " ".join(str(value or "").split())
    if not text:
        raise ToolError(f"{field} is required.")
    if len(text) > max_len:
        raise ToolError(f"{field} is too long (max {max_len} characters).")
    return text


def _number(value, field, lo, hi):
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ToolError(f"{field} must be a number, got {value!r}.") from None
    if not math.isfinite(x) or not lo < x <= hi:
        raise ToolError(f"{field} must be greater than {lo:g} and at most {hi:g}, got {x:g}.")
    return x


def _int(value, field, lo, hi):
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ToolError(f"{field} must be a whole number, got {value!r}.") from None
    if not x.is_integer() or not lo <= x <= hi:
        raise ToolError(f"{field} must be a whole number from {lo} to {hi}, got {value!r}.")
    return int(x)


def _bool(value):
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


def _clock(value):
    try:
        return datetime.strptime(str(value).strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ToolError(f"Invalid time {value!r}. Use HH:MM (24-hour), e.g. 18:00.") from None


def _parse_datetime(value):
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return datetime.combine(planner.parse_date(text, "remind_at"), datetime.min.time()).replace(hour=9)


def _canonical_subject(conn, subject):
    """Match an existing subject case-insensitively, so 'ml' and 'ML' refer to the same plan."""
    subject = _clean_text(subject, "subject", 100)
    row = conn.execute("SELECT name FROM subjects WHERE lower(name)=lower(?) UNION "
                       "SELECT subject FROM sessions WHERE lower(subject)=lower(?) LIMIT 1",
                       (subject, subject)).fetchone()
    return row[0] if row else subject


def _status(row, today):
    if row["done"]:
        return "done"
    return "missed" if row["study_date"] < today else "today" if row["study_date"] == today else "pending"


# ---------- Data access (shared by the tools and the Streamlit UI) ----------
def get_sessions(subject=None, from_date=None):
    sql, params = "SELECT id, study_date, subject, topic, hours, done FROM sessions WHERE 1=1", []
    if subject:
        sql += " AND lower(subject)=lower(?)"
        params.append(str(subject).strip())
    if from_date:
        sql += " AND study_date >= ?"
        params.append(planner.parse_date(from_date, "from_date").isoformat())
    with _db() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY study_date, id", params)]


def get_reminders(upcoming_only=False):
    sql, params = "SELECT id, remind_at, message, subject FROM reminders", []
    if upcoming_only:
        sql += " WHERE remind_at >= ?"
        params.append(datetime.now().strftime("%Y-%m-%d %H:%M"))
    with _db() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY remind_at", params)]


def progress_rows():
    today = date.today().isoformat()
    with _db() as c:
        rows = c.execute("""SELECT s.subject, SUM(s.hours) AS total, SUM(CASE WHEN s.done THEN s.hours ELSE 0 END) AS done,
                                   SUM(CASE WHEN NOT s.done AND s.study_date < ? THEN 1 ELSE 0 END) AS missed,
                                   MIN(s.study_date) AS first_day, MAX(s.study_date) AS last_day, sub.exam_date
                            FROM sessions s LEFT JOIN subjects sub ON sub.name = s.subject
                            GROUP BY s.subject ORDER BY s.subject""", (today,)).fetchall()
    return [dict(r) for r in rows]


def data_files():
    if not os.path.isdir(DATA_DIR):
        return []
    return sorted(f for f in os.listdir(DATA_DIR)
                  if f.lower().endswith(READ_EXTS) and os.path.isfile(os.path.join(DATA_DIR, f)))


def save_upload(filename, data):
    """Save an uploaded syllabus into data/ under a sanitised name; returns the saved name."""
    name = re.sub(r"[^\w.\- ]", "_", os.path.basename(str(filename or ""))).strip(" .") or "syllabus.txt"
    if not name.lower().endswith(READ_EXTS):
        raise ToolError(f"Unsupported file type. Allowed: {', '.join(READ_EXTS)}.")
    if not data:
        raise ToolError("The uploaded file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(f"File is too large (max {MAX_FILE_BYTES // 1_000_000} MB).")
    with open(_safe_path(name, READ_EXTS), "wb") as f:
        f.write(data)
    return name


def syllabus_overview():
    """Parse every text syllabus in data/ -> [{file, subject, exam_date, units, total_hours, warnings}]."""
    out = []
    for f in data_files():
        if f.lower().endswith((".txt", ".md")) and f.lower() != "timetable.md":
            try:
                out.append({"file": f, **planner.parse_syllabus(read_file(f))})
            except ToolError:
                pass
    return out


def bootstrap_sample_data(hours_per_day=4):
    """First run (no plans yet): create the sample syllabi from the saved Hugging Face rows and plan every
    subject, earliest exam first, so the app opens with data. Returns one result line per subject."""
    if progress_rows():
        return []
    sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
    import make_syllabi
    today = date.today().isoformat()
    existing = {s["file"]: s["exam_date"] for s in syllabus_overview()}
    rows = make_syllabi.load_cached()
    if any(existing.get(make_syllabi.file_name(r)) is None or existing[make_syllabi.file_name(r)] <= today for r in rows):
        make_syllabi.write_syllabi(rows, DATA_DIR)  # missing or exams already passed: regenerate from today
    out = run_tool("plan_all_subjects", {"hours_per_day": hours_per_day})
    return [line for line in out.split("\n\n")[0].splitlines() if line]


def timetable_markdown(subject=None):
    rows = get_sessions(subject)
    if not rows:
        return None
    with _db() as c:
        exams = {r[0]: r[1] for r in c.execute("SELECT name, exam_date FROM subjects")}
    today = date.today().isoformat()
    out = ["# Study Timetable", "", f"_Generated on {today}_", ""]
    for name in dict.fromkeys(r["subject"] for r in rows):
        items = [r for r in rows if r["subject"] == name]
        out += [f"## {name}" + (f" - exam on {exams[name]}" if exams.get(name) else ""), "",
                "| Date | Day | Topic | Hours | Status |", "|------|-----|-------|-------|--------|"]
        for r in items:
            day = date.fromisoformat(r["study_date"]).strftime("%a")
            out.append(f"| {r['study_date']} | {day} | {r['topic'].replace('|', '/')} | {r['hours']:g} | {_status(r, today)} |")
        out += ["", f"**Total:** {sum(r['hours'] for r in items):g} h in {len(items)} sessions", ""]
    return "\n".join(out)


# ---------- File tools (sandboxed to data/) ----------
def _safe_path(filename, allowed_exts):
    name = str(filename or "").strip().strip("'\"")
    name = re.sub(r"^\.?[\\/]?data[\\/]", "", name)  # models often write "data/x.txt"
    if not name:
        raise ToolError("A file name is required.")
    if os.path.isabs(name) or os.path.dirname(os.path.normpath(name)):
        raise ToolError("Use a plain file name inside the data/ folder (no folders or paths).")
    if not name.lower().endswith(allowed_exts):
        raise ToolError(f"Unsupported file type for '{name}'. Allowed: {', '.join(allowed_exts)}.")
    return os.path.join(DATA_DIR, name)


def _read_text(path):
    if path.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ToolError("Reading PDFs needs the pypdf package: pip install pypdf") from None
        try:
            return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        except Exception as e:
            raise ToolError(f"Could not read the PDF ({type(e).__name__}). Try a .txt version.") from None
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def list_files():
    files = data_files()
    if not files:
        return "The data/ folder has no syllabus files. Ask the user to upload one."
    return "\n".join(f"{f} ({os.path.getsize(os.path.join(DATA_DIR, f))} bytes)" for f in files)


def read_file(filename):
    path = _safe_path(filename, READ_EXTS)
    if not os.path.isfile(path):
        raise ToolError(f"'{os.path.basename(path)}' not found in data/. Available: {', '.join(data_files()) or 'none'}.")
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise ToolError(f"File is too large (max {MAX_FILE_BYTES // 1_000_000} MB).")
    text = _read_text(path)
    if not text.strip():
        hint = " (scanned PDFs without a text layer cannot be read)" if path.lower().endswith(".pdf") else ""
        raise ToolError(f"'{os.path.basename(path)}' is empty{hint}.")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + f"\n[truncated: file has {len(text)} characters]"
    return text


def write_file(filename, content):
    path = _safe_path(filename, WRITE_EXTS)
    if os.path.exists(path) and not path.lower().endswith(OVERWRITE_EXTS):
        raise ToolError(f"'{os.path.basename(path)}' already exists and may be a syllabus; choose a new name or a .md file.")
    content = "" if content is None else str(content)
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ToolError("Content is too large to save.")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return f"Saved data/{os.path.basename(path)} ({len(content)} characters)."


def parse_syllabus(filename):
    result = planner.parse_syllabus(read_file(filename))
    if not result["units"]:
        result["warnings"].append("Could not detect units: call read_file and extract them yourself, or ask the user.")
    return json.dumps(result, indent=1)


def export_timetable(filename="timetable.md", subject=None):
    if not str(filename).lower().endswith(".md"):
        raise ToolError("export_timetable writes Markdown; use a .md file name.")
    md = timetable_markdown(subject)
    if md is None:
        raise ToolError("Nothing to export - the schedule is empty" + (f" for '{subject}'." if subject else "."))
    return write_file(filename, md)


# ---------- Calculator (safe: no eval) ----------
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
        ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}


def _eval(node):
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
            raise ToolError("Exponent too large.")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ToolError("Only numbers and + - * / // % ** ( ) are allowed.")


def calculator(expression):
    expr = str(expression or "").strip()
    if not expr or len(expr) > 200:
        raise ToolError("Expression must be 1-200 characters.")
    try:
        result = _eval(ast.parse(expr, mode="eval").body)
    except ZeroDivisionError:
        raise ToolError("Division by zero.") from None
    except SyntaxError:
        raise ToolError(f"Invalid expression: {expr!r}.") from None
    return f"{round(result, 6):.12g}" if isinstance(result, float) else str(result)


# ---------- Dates ----------
def get_today():
    d = date.today()
    return f"{d.isoformat()} ({d:%A})"


def days_between(start_date, end_date):
    d1 = planner.parse_date(start_date, "start_date")
    d2 = planner.parse_date(end_date, "end_date")
    n = (d2 - d1).days
    return f"{n} days from {d1} to {d2}" + (" (end is before start)" if n < 0 else "")


# ---------- Schedule ----------
def plan_schedule(subject, units, exam_date, hours_per_day=2, start_date=None, revision_days=3, reminder_time="18:00"):
    """Build (or rebuild) one subject's plan and reminders. Other subjects and completed sessions are kept."""
    if isinstance(units, str):
        try:
            units = json.loads(units)
        except json.JSONDecodeError:
            raise ToolError('units must be a JSON list like [{"topic": "Intro", "hours": 4}].') from None
    units = planner.validate_units(units)
    hpd = _number(hours_per_day, "hours_per_day", 0, planner.MAX_HOURS_PER_DAY)
    rev = _int(revision_days, "revision_days", 0, planner.MAX_REVISION_DAYS)
    rtime = _clock(reminder_time)
    today = date.today()
    exam = planner.parse_date(exam_date, "exam_date")
    start = planner.parse_date(start_date, "start_date") if start_date else today
    notes = []
    if exam < today:
        raise ToolError(f"The exam date {exam} is in the past (today is {today}). Ask the user for the correct date.")
    if exam == today:
        raise ToolError("The exam is today, so there are no days left to plan. Wish the student luck!")
    if start < today:
        notes.append(f"start_date {start} was in the past, so the plan starts today ({today}).")
        start = today
    if start >= exam:
        raise ToolError(f"start_date {start} must be before the exam date {exam}.")

    with _db() as c:
        name = _canonical_subject(c, subject)
        done_hours = dict(c.execute("SELECT topic, SUM(hours) FROM sessions WHERE subject=? AND done=1 GROUP BY topic",
                                    (name,)).fetchall())
        remaining = []
        for u in units:
            left = planner.floor_step(u["hours"] - done_hours.get(u["topic"], 0))
            if left > 0:
                remaining.append({"topic": u["topic"], "hours": left})
            else:
                notes.append(f"'{u['topic']}' is already completed.")
        # Hours already taken on each day by other subjects (or by this subject's completed sessions).
        booked = {date.fromisoformat(d): h for d, h in c.execute(
            "SELECT study_date, SUM(hours) FROM sessions WHERE study_date >= ? AND (subject != ? OR done = 1) "
            "GROUP BY study_date", (start.isoformat(), name))}
        sessions, info = planner.build_plan(remaining, start, exam, hpd, rev, booked)  # raises before any DB change

        done_revision = {r[0] for r in c.execute("SELECT study_date FROM sessions WHERE subject=? AND done=1 AND topic=?",
                                                 (name, planner.REVISION))}
        c.execute("DELETE FROM sessions WHERE subject=? AND done=0", (name,))
        c.execute("DELETE FROM reminders WHERE subject=?", (name,))
        per_day = {}
        for d, topic, h in sessions:
            if topic == planner.REVISION and d.isoformat() in done_revision:
                continue  # that revision day is already done
            c.execute("INSERT INTO sessions(subject, topic, study_date, hours) VALUES(?,?,?,?)", (name, topic, d.isoformat(), h))
            per_day.setdefault(d, []).append(f"{topic} ({h:g}h)")
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        reminders = [(f"Study {name}: " + ", ".join(items), f"{d} {rtime}") for d, items in per_day.items()]
        reminders.append((f"{name} EXAM today - all the best!", f"{exam} 08:00"))
        reminders = [r for r in reminders if r[1] >= now]  # e.g. today's 18:00 reminder when it is already 20:00
        c.executemany("INSERT INTO reminders(message, remind_at, subject) VALUES(?,?,?)", [(m, t, name) for m, t in reminders])
        c.execute("INSERT OR REPLACE INTO subjects(name, exam_date, hours_per_day, revision_days, reminder_time, units) "
                  "VALUES(?,?,?,?,?,?)", (name, exam.isoformat(), hpd, rev, rtime, json.dumps(units)))

    study = [s for s in sessions if s[1] != planner.REVISION]
    revision = [s for s in sessions if s[1] == planner.REVISION]
    lines = [f"Planned {name} (exam {exam}, {hpd:g}h/day max)."]
    if study:
        lines.append(f"Study: {sum(s[2] for s in study):g}h in {len(study)} sessions, {study[0][0]} to {study[-1][0]} "
                     f"(about {info['daily_quota']:g}h/day). {info['buffer_days']} spare day(s) left as buffer.")
    else:
        lines.append("All units are already completed.")
    lines.append(f"Revision: {len(revision)} day(s). Reminders: {len(reminders)} at {rtime}.")
    lines += notes + info["warnings"]
    lines.append("Next: call export_timetable.")
    return "\n".join(lines)


def _collect_subjects(c, overrides, files=None):
    """Subjects to plan together: every syllabus in data/ (or just `files`) plus saved subjects with no file.
    Topic difficulty: explicit override > rating saved by an earlier plan > estimate from the syllabus."""
    today = date.today().isoformat()
    saved = {r["name"].lower(): dict(r) for r in c.execute("SELECT * FROM subjects")}
    entries, notes = {}, []
    for s in syllabus_overview():
        if files and s["file"] not in files:
            continue
        if not s["units"] or not s["exam_date"]:
            notes.append(f"Skipped {s['file']}: no units or no exam date (ask the user).")
            continue
        if s["exam_date"] <= today:
            notes.append(f"Skipped {s['subject'] or s['file']}: its exam ({s['exam_date']}) has already happened.")
            continue
        name = _canonical_subject(c, s["subject"] or os.path.splitext(s["file"])[0])
        entries[name.lower()] = {"name": name, "exam": date.fromisoformat(s["exam_date"]),
                                 "level": planner.parse_level(s["level"], 2), "units": planner.validate_units(s["units"])}
    if not files:
        for key, row in saved.items():
            if key not in entries and row["exam_date"] > today:
                entries[key] = {"name": row["name"], "exam": date.fromisoformat(row["exam_date"]),
                                "level": row["level"] or 2, "units": planner.validate_units(json.loads(row["units"]))}
    for key, e in entries.items():
        rated = {u["topic"].lower(): u["difficulty"] for u in json.loads(saved[key]["units"]) if u.get("rated")} if key in saved else {}
        for u in e["units"]:
            t = u["topic"].lower()
            value = overrides.get(f"{key}::{t}", overrides.get(t))
            if value is not None or t in rated:
                u["difficulty"], u["rated"] = planner.parse_level(value if value is not None else rated[t]), True
    return list(entries.values()), notes


def plan_all_subjects(hours_per_day=4, revision_days=3, difficulty=None, hard_first=True, reminder_time="18:00", files=None):
    """Plan every subject together under one daily limit, prioritising by exam date, difficulty and hours left."""
    hpd = _number(hours_per_day, "hours_per_day", 0, planner.MAX_HOURS_PER_DAY)
    rev = _int(revision_days, "revision_days", 0, planner.MAX_REVISION_DAYS)
    rtime = _clock(reminder_time)
    if isinstance(difficulty, str):
        try:
            difficulty = json.loads(difficulty) if difficulty.strip() else {}
        except json.JSONDecodeError:
            raise ToolError('difficulty must be an object like {"Pointers": "hard"}.') from None
    if difficulty is not None and not isinstance(difficulty, dict):
        raise ToolError('difficulty must be an object like {"Pointers": "hard"}.')
    overrides = {" ".join(str(k).split()).lower(): v for k, v in (difficulty or {}).items()}
    for v in overrides.values():
        planner.parse_level(v)  # reject bad ratings before touching the database
    files = [files] if isinstance(files, str) else files
    today = date.today()
    with _db() as c:
        entries, notes = _collect_subjects(c, overrides, files)
        if not entries:
            raise ToolError("No subjects with units and an upcoming exam were found in data/." + (" " + " ".join(notes) if notes else ""))
        plan_input = []
        for e in entries:
            done = dict(c.execute("SELECT topic, SUM(hours) FROM sessions WHERE subject=? AND done=1 GROUP BY topic",
                                  (e["name"],)).fetchall())
            plan_input.append({"name": e["name"], "exam": e["exam"], "level": e["level"],
                               "units": planner.prepare_units(e["units"], done, _bool(hard_first))})
        sessions, info = planner.build_multi_plan(plan_input, today, hpd, rev)  # raises before any DB change
        for e in entries:
            c.execute("DELETE FROM sessions WHERE subject=? AND done=0", (e["name"],))
            c.execute("DELETE FROM reminders WHERE subject=?", (e["name"],))
            c.execute("INSERT OR REPLACE INTO subjects(name, exam_date, hours_per_day, revision_days, reminder_time, units, level) "
                      "VALUES(?,?,?,?,?,?,?)", (e["name"], e["exam"].isoformat(), hpd, rev, rtime, json.dumps(e["units"]), e["level"]))
        per_day = {}
        for d, name, topic, h in sessions:
            c.execute("INSERT INTO sessions(subject, topic, study_date, hours) VALUES(?,?,?,?)", (name, topic, d.isoformat(), h))
            per_day.setdefault((name, d), []).append(f"{topic} ({h:g}h)")
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        reminders = [(f"Study {name}: " + ", ".join(items), f"{d} {rtime}", name) for (name, d), items in per_day.items()]
        reminders += [(f"{e['name']} EXAM today - all the best!", f"{e['exam']} 08:00", e["name"]) for e in entries]
        c.executemany("INSERT INTO reminders(message, remind_at, subject) VALUES(?,?,?)", [r for r in reminders if r[1] >= now])
    hard = [u["topic"] for e in entries for u in e["units"] if u["difficulty"] == 3]
    lines = [f"Planned {len(entries)} subjects together (max {hpd:g}h/day, {rev} revision day(s) each, "
             f"{'hardest topics first' if _bool(hard_first) else 'syllabus order'}, {info['mode']} schedule)."]
    if info["mode"] == "front-loaded":
        lines.append("The plan is tight, so free time was filled early; consider more hours/day for breathing room.")
    if hard:
        lines.append(f"Hard topics got {planner.HARD_EXTRA:.0%} extra practice time: {', '.join(hard[:12])}" + (" ..." if len(hard) > 12 else "") + ".")
    lines += notes + info["warnings"]
    run_tool("export_timetable", {})
    return "\n".join(lines) + "\n\n" + overview_text()


def priority_rows():
    """Subjects ranked by priority: the daily pace still needed, weighted by difficulty."""
    today = date.today()
    rows = []
    with _db() as c:
        for s in c.execute("SELECT name, exam_date, revision_days, level FROM subjects").fetchall():
            exam = date.fromisoformat(s["exam_date"])
            if exam < today:
                continue
            left = c.execute("SELECT COALESCE(SUM(hours), 0) FROM sessions WHERE subject=? AND done=0 AND topic != ?",
                             (s["name"], planner.REVISION)).fetchone()[0]
            level = s["level"] or 2
            pace = left / max(1, (exam - today).days - (s["revision_days"] or 0))
            days_left = (exam - today).days
            rows.append({"subject": s["name"], "level": planner.LEVEL_NAMES[level], "exam_date": s["exam_date"],
                         "days_left": days_left, "hours_left": left, "hours_per_day": round(pace, 2),
                         # daily pace needed x difficulty, boosted as the exam gets close
                         "score": pace * planner.PRIORITY_FACTOR[level] * (1 + 28 / max(days_left, 1))})
    return sorted(rows, key=lambda r: (-r["score"], r["days_left"]))


def daily_split(days=7):
    """Pending sessions for the next `days` days: [{date, subject, topic, hours}]."""
    today = date.today()
    end = (today + timedelta(days=days)).isoformat()
    return [r for r in get_sessions(from_date=today.isoformat()) if r["study_date"] < end and not r["done"]]


def overview_text(days=7):
    rows = priority_rows()
    if not rows:
        return "No plans yet."
    out = ["Priority (focus on the top first):"]
    out += [f"{i}. {r['subject']} - {r['level']}, exam {r['exam_date']} ({r['days_left']}d), {r['hours_left']:g}h left, "
            f"needs ~{r['hours_per_day']:g}h/day" for i, r in enumerate(rows, 1)]
    out.append(f"\nNext {days} days:")
    by_day = {}
    for r in daily_split(days):
        by_day.setdefault(r["study_date"], []).append(r)
    for d, items in by_day.items():
        total = sum(r["hours"] for r in items)
        parts = "; ".join(f"{r['subject']}: {r['topic']} {r['hours']:g}h" for r in items)
        out.append(f"- {date.fromisoformat(d):%a %d %b} ({total:g}h): {parts}")
    return "\n".join(out)


def list_subjects():
    subjects = syllabus_overview()
    if not subjects:
        return "No syllabus files in data/. Ask the user to upload one."
    out = []
    for s in sorted(subjects, key=lambda s: s["exam_date"] or "9999"):
        topics = "; ".join(f"{u['topic']} ({u['hours']:g}h, {u['difficulty']})" for u in s["units"]) or "no units found"
        out.append(f"{s['subject'] or '?'} [{s['file']}] exam {s['exam_date'] or 'MISSING'}, level {s['level']}: {topics}")
    return "\n".join(out)


def get_overview(days=7):
    return overview_text(_int(days, "days", 1, 14))


def reschedule_missed(subject, hours_per_day=None):
    today = date.today().isoformat()
    with _db() as c:
        name = _canonical_subject(c, subject)
        plan = c.execute("SELECT * FROM subjects WHERE name=?", (name,)).fetchone()
        count, hours = c.execute("SELECT COUNT(*), COALESCE(SUM(hours), 0) FROM sessions "
                                 "WHERE subject=? AND done=0 AND study_date < ?", (name, today)).fetchone()
    if not plan:
        raise ToolError(f"No saved plan for '{name}'. Create one with plan_schedule first.")
    if not count:
        return f"No missed sessions for {name} - you're on track!"
    if plan["level"] is not None:  # planned together with other subjects: re-plan them all jointly
        result = plan_all_subjects(hours_per_day or plan["hours_per_day"], plan["revision_days"],
                                   reminder_time=plan["reminder_time"])
        return f"Moved {count} missed session(s) ({hours:g}h) into the remaining days.\n{result}"
    result = plan_schedule(name, json.loads(plan["units"]), plan["exam_date"], hours_per_day or plan["hours_per_day"],
                           None, plan["revision_days"], plan["reminder_time"])
    return f"Moved {count} missed session(s) ({hours:g}h) into the remaining days.\n{result}"


def list_schedule(subject=None, from_date=None):
    rows = get_sessions(subject, from_date)
    if not rows:
        return "No study sessions scheduled" + (f" for '{subject}'." if subject else ".")
    today = date.today().isoformat()
    lines = [f"#{r['id']} {r['study_date']} | {r['subject']} - {r['topic']} | {r['hours']:g}h | {_status(r, today)}"
             for r in rows[:LIST_LIMIT]]
    if len(rows) > LIST_LIMIT:
        lines.append(f"... and {len(rows) - LIST_LIMIT} more (pass from_date to see later sessions).")
    return "\n".join(lines)


def mark_session_done(session_id, done=True):
    sid = _int(session_id, "session_id", 1, 10**9)
    done = _bool(done)
    with _db() as c:
        row = c.execute("SELECT study_date, topic FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise ToolError(f"No session #{sid}. Use list_schedule to see valid ids.")
        c.execute("UPDATE sessions SET done=? WHERE id=?", (int(done), sid))
    return f"Session #{sid} ({row['study_date']}, {row['topic']}) marked {'done' if done else 'not done'}."


def add_study_session(subject, topic, study_date, hours):
    d = planner.parse_date(study_date, "study_date")
    if d < date.today():
        raise ToolError("Cannot schedule a session in the past.")
    hours = _number(hours, "hours", 0, planner.MAX_HOURS_PER_DAY)
    topic = _clean_text(topic, "topic")
    with _db() as c:
        name = _canonical_subject(c, subject)
        if c.execute("SELECT 1 FROM sessions WHERE subject=? AND topic=? AND study_date=?", (name, topic, d.isoformat())).fetchone():
            return f"Skipped: '{topic}' is already scheduled on {d}."
        booked = c.execute("SELECT COALESCE(SUM(hours), 0) FROM sessions WHERE study_date=?", (d.isoformat(),)).fetchone()[0]
        if booked + hours > planner.MAX_HOURS_PER_DAY:
            raise ToolError(f"{d} already has {booked:g}h booked; adding {hours:g}h would exceed {planner.MAX_HOURS_PER_DAY}h.")
        c.execute("INSERT INTO sessions(subject, topic, study_date, hours) VALUES(?,?,?,?)", (name, topic, d.isoformat(), hours))
    return f"Added: {d} | {name} - {topic} ({hours:g}h)"


def clear_schedule(subject):
    with _db() as c:
        if str(subject).strip().lower() == "all":
            n = c.execute("DELETE FROM sessions").rowcount
            c.execute("DELETE FROM reminders")
            c.execute("DELETE FROM subjects")
            return f"Deleted all {n} sessions, reminders and saved plans."
        name = _canonical_subject(c, subject)
        n = c.execute("DELETE FROM sessions WHERE subject=?", (name,)).rowcount
        c.execute("DELETE FROM reminders WHERE subject=?", (name,))
        had_plan = c.execute("DELETE FROM subjects WHERE name=?", (name,)).rowcount
    if not n and not had_plan:
        raise ToolError(f"No plan found for '{name}'.")
    return f"Deleted the {name} plan ({n} sessions) and its reminders."


def add_reminder(message, remind_at):
    message = _clean_text(message, "message", 300)
    when = _parse_datetime(remind_at)
    if when < datetime.now():
        raise ToolError(f"{when:%Y-%m-%d %H:%M} is in the past.")
    with _db() as c:
        c.execute("INSERT INTO reminders(message, remind_at) VALUES(?,?)", (message, f"{when:%Y-%m-%d %H:%M}"))
    return f"Reminder set for {when:%Y-%m-%d %H:%M}: {message}"


def list_reminders(upcoming_only=True):
    rows = get_reminders(_bool(upcoming_only))
    if not rows:
        return "No upcoming reminders." if _bool(upcoming_only) else "No reminders."
    lines = [f"#{r['id']} {r['remind_at']} - {r['message']}" for r in rows[:LIST_LIMIT]]
    if len(rows) > LIST_LIMIT:
        lines.append(f"... and {len(rows) - LIST_LIMIT} more.")
    return "\n".join(lines)


def topic_progress(subject):
    """Per-topic planned vs done hours for one subject, in syllabus order. Returns (canonical_name, rows)."""
    with _db() as c:
        name = _canonical_subject(c, subject)
        rows = c.execute("""SELECT topic, SUM(hours) AS planned, SUM(CASE WHEN done THEN hours ELSE 0 END) AS done
                            FROM sessions WHERE subject=? GROUP BY topic ORDER BY MIN(id)""", (name,)).fetchall()
    return name, [dict(r) for r in rows]


def _match_topic(topics, topic):
    """Exact (case-insensitive) match first, then a unique partial match, e.g. 'pointer' -> 'Pointers'."""
    wanted = " ".join(str(topic or "").split()).lower()
    if not wanted:
        raise ToolError(f"topic is required. Topics: {', '.join(topics)}.")
    exact = [t for t in topics if t.lower() == wanted]
    partial = [t for t in topics if wanted in t.lower() or t.lower() in wanted]
    if exact or len(partial) == 1:
        return (exact or partial)[0]
    hint = "matches several topics" if partial else "is not in this plan"
    raise ToolError(f"'{topic}' {hint}. Topics: {', '.join(partial or topics)}.")


def log_progress(subject, topic, hours=None, complete=False):
    """Record study done on a topic: marks its pending sessions done in date order (splitting one if needed)."""
    complete = _bool(complete)
    if hours is None and not complete:
        raise ToolError("Give the hours studied, or complete=true if the whole topic is finished.")
    hrs = None if complete else max(planner.STEP, planner.floor_step(_number(hours, "hours", 0, 24)))
    with _db() as c:
        name = _canonical_subject(c, subject)
        topics = [r[0] for r in c.execute("SELECT topic FROM sessions WHERE subject=? GROUP BY topic ORDER BY MIN(id)", (name,))]
        if not topics:
            raise ToolError(f"No plan found for '{name}'. Create one with plan_schedule first.")
        t = _match_topic(topics, topic)
        pending = c.execute("SELECT id, study_date, hours FROM sessions WHERE subject=? AND topic=? AND done=0 "
                            "ORDER BY study_date, id", (name, t)).fetchall()
        if not pending:
            return f"'{t}' in {name} is already fully completed."
        planned_left = sum(r["hours"] for r in pending)
        left, logged = (planned_left if complete else hrs), 0.0
        for r in pending:
            if left <= 1e-9:
                break
            if r["hours"] <= left + 1e-9:
                c.execute("UPDATE sessions SET done=1 WHERE id=?", (r["id"],))
                take = r["hours"]
            else:  # partial session: keep the rest pending, record the studied part as a done session
                take = left
                c.execute("UPDATE sessions SET hours=? WHERE id=?", (r["hours"] - take, r["id"]))
                c.execute("INSERT INTO sessions(subject, topic, study_date, hours, done) VALUES(?,?,?,?,1)",
                          (name, t, r["study_date"], take))
            logged += take
            left -= take
    msg = f"Logged {logged:g}h of '{t}' ({name}). {planned_left - logged:g}h of this topic left."
    if hrs and hrs > logged + 1e-9:
        msg += f" You studied {hrs - logged:g}h more than planned for it."
    return msg


def get_progress(subject=None):
    if subject:
        name, rows = topic_progress(subject)
        if not rows:
            raise ToolError(f"No plan found for '{name}'.")
        lines = [f"{name} - progress by topic:"]
        lines += [f"- {r['topic']}: {r['done']:g}/{r['planned']:g}h ({100 * r['done'] / r['planned']:.0f}%)" for r in rows]
        return "\n".join(lines)
    rows = progress_rows()
    if not rows:
        return "No plans yet."
    today = date.today()
    out = []
    for r in rows:
        line = f"{r['subject']}: {r['done']:g}/{r['total']:g}h done ({100 * r['done'] / r['total']:.0f}%), {r['missed']} missed session(s)"
        if r["exam_date"]:
            line += f", exam in {(date.fromisoformat(r['exam_date']) - today).days} day(s) ({r['exam_date']})"
        out.append(line)
    return "\n".join(out)


# ---------- Registry & schemas (OpenAI-style function calling, used by Groq) ----------
def _s(name, desc, props=None, req=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props or {}, "required": list(req)}}}


def _str(d):
    return {"type": "string", "description": d}


def _num(d):
    return {"type": "number", "description": d}


TOOL_SCHEMAS = [
    _s("get_today", "Get today's date and weekday."),
    _s("list_files", "List syllabus files available in the data/ folder."),
    _s("parse_syllabus", "PREFERRED way to read a syllabus: returns JSON with subject, exam_date, units (topic + hours) and warnings.",
       {"filename": _str("File name in data/, e.g. sample_syllabus.txt")}, ["filename"]),
    _s("read_file", "Read raw text of a file in data/ (.txt .md .csv .pdf). Use only if parse_syllabus finds no units.",
       {"filename": _str("File name")}, ["filename"]),
    _s("write_file", "Write a text file (.md .csv .txt) in data/. Existing .txt files cannot be overwritten.",
       {"filename": _str("File name"), "content": _str("Text content")}, ["filename", "content"]),
    _s("calculator", "Evaluate arithmetic with + - * / // % ** and parentheses.",
       {"expression": _str("e.g. (6+8+5)/12")}, ["expression"]),
    _s("days_between", "Number of days between two dates.",
       {"start_date": _str("YYYY-MM-DD"), "end_date": _str("YYYY-MM-DD")}, ["start_date", "end_date"]),
    _s("plan_schedule", "PREFERRED: builds one subject's complete schedule + reminders in one call. Spreads units evenly "
       "before the revision days, respects the daily hour cap shared with other subjects, keeps completed sessions "
       "and replaces this subject's old plan. Returns an error with a suggested hours_per_day if it cannot fit.",
       {"subject": _str("Subject name"),
        "units": {"type": "array", "description": "All syllabus units in order",
                  "items": {"type": "object", "properties": {"topic": {"type": "string"}, "hours": {"type": "number"}},
                            "required": ["topic", "hours"]}},
        "exam_date": _str("YYYY-MM-DD"),
        "hours_per_day": _num("Max study hours per day (default 2)"),
        "start_date": _str("First study day YYYY-MM-DD (default today)"),
        "revision_days": {"type": "integer", "description": "Revision days right before the exam (default 3)"},
        "reminder_time": _str("HH:MM, default 18:00")},
       ["subject", "units", "exam_date"]),
    _s("plan_all_subjects", "PREFERRED for planning: plans EVERY subject in data/ together in one call under one shared "
       "daily limit. Prioritises earlier exams, gives hard topics 20% extra time and schedules them first, keeps "
       "completed work. Returns the priority ranking and the next 7 days' split.",
       {"hours_per_day": _num("Max total study hours per day across all subjects (default 4)"),
        "revision_days": {"type": "integer", "description": "Revision days before each exam (default 3)"},
        "difficulty": {"type": "object", "description": 'Your ratings per topic, e.g. {"Pointers": "hard", "Arrays": "easy"}',
                       "additionalProperties": {"type": "string", "enum": ["easy", "medium", "hard"]}},
        "hard_first": {"type": "boolean", "description": "Hard topics first within each subject (default true)"},
        "reminder_time": _str("HH:MM, default 18:00")}),
    _s("list_subjects", "All syllabi in data/: subject, exam date, level, and each topic with hours and estimated difficulty."),
    _s("get_overview", "Priority ranking of subjects and the day-by-day study split for the next days.",
       {"days": {"type": "integer", "description": "1-14, default 7"}}),
    _s("reschedule_missed", "Re-plan a subject so past, unfinished sessions move into the remaining days.",
       {"subject": _str("Subject name"), "hours_per_day": _num("Optional new max hours per day")}, ["subject"]),
    _s("list_schedule", "List saved study sessions with ids and status (done/missed/today/pending).",
       {"subject": _str("Optional subject filter"), "from_date": _str("Optional YYYY-MM-DD")}),
    _s("get_progress", "Progress per subject (hours done, missed, days to exam), or per topic when a subject is given.",
       {"subject": _str("Optional subject for a per-topic breakdown")}),
    _s("log_progress", "Record what the student studied for one topic of a subject: hours studied, or complete=true "
       "for the whole topic. Topic names can be partial (e.g. 'pointer').",
       {"subject": _str("Subject name"), "topic": _str("Topic name"), "hours": _num("Hours studied"),
        "complete": {"type": "boolean", "description": "True if the whole topic is finished"}},
       ["subject", "topic"]),
    _s("mark_session_done", "Mark a session as completed (or not completed with done=false).",
       {"session_id": {"type": "integer"}, "done": {"type": "boolean", "description": "Default true"}}, ["session_id"]),
    _s("add_study_session", "Add ONE extra session manually. Never use this to build a whole plan.",
       {"subject": _str("Subject"), "topic": _str("Topic"), "study_date": _str("YYYY-MM-DD"), "hours": _num("Hours")},
       ["subject", "topic", "study_date", "hours"]),
    _s("clear_schedule", "Delete a subject's plan, or 'all'. Only when the user explicitly asks.",
       {"subject": _str("Subject name or 'all'")}, ["subject"]),
    _s("add_reminder", "Create a custom reminder.",
       {"message": _str("Reminder text"), "remind_at": _str("YYYY-MM-DD HH:MM")}, ["message", "remind_at"]),
    _s("list_reminders", "List reminders.", {"upcoming_only": {"type": "boolean", "description": "Default true"}}),
    _s("export_timetable", "Write the saved schedule as a Markdown table to data/ (default timetable.md).",
       {"filename": _str("Default timetable.md"), "subject": _str("Optional subject filter")}),
]

REGISTRY = {f.__name__: f for f in [get_today, list_files, parse_syllabus, read_file, write_file, calculator,
                                    days_between, plan_schedule, plan_all_subjects, list_subjects, get_overview, reschedule_missed, list_schedule, get_progress,
                                    log_progress, mark_session_done, add_study_session, clear_schedule, add_reminder,
                                    list_reminders, export_timetable]}


def run_tool(name, args):
    """Execute a tool by name. Never raises: failures come back as 'Error: ...' text for the model."""
    fn = REGISTRY.get(name)
    if fn is None:
        return f"Error: unknown tool '{name}'. Available tools: {', '.join(REGISTRY)}."
    if not isinstance(args, dict):
        return "Error: tool arguments must be a JSON object."
    args = {k: v for k, v in args.items() if v is not None}  # models often send null for optional fields
    try:
        inspect.signature(fn).bind(**args)
    except TypeError as e:
        return f"Error: bad arguments for {name}: {e}. Expected: {list(inspect.signature(fn).parameters)}."
    try:
        return str(fn(**args))
    except (ToolError, PlanError) as e:
        return f"Error: {e}"
    except sqlite3.Error as e:
        return f"Error: database problem ({e}). Try again."
    except Exception as e:
        return f"Error: {name} failed unexpectedly ({type(e).__name__}: {e})."
