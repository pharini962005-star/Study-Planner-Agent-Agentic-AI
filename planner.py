"""Pure planning logic: date/hour parsing, syllabus parsing and schedule building (no file or DB access)."""
import math
import re
from datetime import date, datetime, timedelta

STEP = 0.25               # smallest bookable slot (15 minutes)
MAX_HOURS_PER_DAY = 16
MAX_REVISION_DAYS = 14
MAX_UNIT_HOURS = 500
REVISION = "Revision"
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
                "%d %b %Y", "%d %B %Y", "%d %b, %Y", "%d %B, %Y",
                "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y")


LEVEL_WORDS = {"easy": 1, "basic": 1, "beginner": 1, "medium": 2, "moderate": 2, "intermediate": 2,
               "hard": 3, "difficult": 3, "advanced": 3}
LEVEL_NAMES = {1: "easy", 2: "medium", 3: "hard"}
HARD_EXTRA = 0.2                         # hard topics get 20% extra practice time
PACE_FACTOR = {1: 1.0, 2: 1.0, 3: 1.2}   # hard subjects are paced faster, so they finish with more slack
PRIORITY_FACTOR = {1: 0.85, 2: 1.0, 3: 1.2}


class PlanError(ValueError):
    """Invalid planning input, or a plan that cannot fit; the message says how to fix it."""


def parse_level(value, default=None):
    """'hard' / 'Advanced level' / 3 -> 3. None -> default."""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool) and int(value) == value and 1 <= value <= 3:
        return int(value)
    text = str(value).strip().lower()
    if text in ("1", "2", "3"):
        return int(text)
    for word, level in LEVEL_WORDS.items():
        if word in text:
            return level
    raise PlanError(f"Unknown difficulty {value!r}. Use easy, medium or hard.")


def floor_step(x):
    return math.floor(x / STEP + 1e-9) * STEP


def round_step(x):
    return max(STEP, round(x / STEP) * STEP)


def parse_date(value, field="date"):
    """Accept ISO and common human date formats ('10/12/2026', '10 Dec 2026', 'December 10th, 2026')."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", " ".join(str(value or "").split()))
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise PlanError(f"Invalid {field} {value!r}. Use YYYY-MM-DD.")


# ---------- Syllabus parsing ----------
_NUM = r"(\d+(?:\.\d+)?)"
_HOURS_RE = re.compile(_NUM + r"\s*(?:hours?|hrs?|h)\b", re.I)
_MINS_RE = re.compile(_NUM + r"\s*(?:minutes?|mins?|m)\b", re.I)
_DURATION_RE = re.compile(r"[(\[]?\s*(?:~|approx\.?|about)?\s*\d+(?:\.\d+)?\s*(?:hours?|hrs?|h|minutes?|mins?|m)\b\.?\s*[)\]]?", re.I)
_PREFIX_RE = re.compile(r"^\s*(?:(?:unit|module|chapter|topic|week|lesson|part|section)\s*[\w.]*\s*[:.)\-–]+"
                        r"|[-*•]\s+|\d+\s*[.):\-]\s+)\s*", re.I)
_DATE_RE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}"
                      r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+\d{4}|[A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}")
_SUBJECT_RE = re.compile(r"^\s*(?:subject|course|paper)\s*(?:name)?\s*[:\-–]\s*(.+)", re.I)
_LEVEL_LINE_RE = re.compile(r"^\s*(?:level|difficulty)\s*[:\-–]\s*(.+)", re.I)
_TAG_RE = re.compile(r"[\[(]\s*(" + "|".join(LEVEL_WORDS) + r")\s*[\])]", re.I)
_META_RE = re.compile(r"^\s*(?:source|instructor|faculty|lecturer|credits?|total|duration|textbooks?|references?|"
                      r"notes?|prerequisites?|grading|assessment)\b[^:]{0,20}:", re.I)


def parse_hours(text):
    """'6 hours', '6 hrs', '1.5h', '90 min', '1h 30m' -> hours (multiple of STEP), or None if absent."""
    h = sum(float(x) for x in _HOURS_RE.findall(text))
    m = sum(float(x) for x in _MINS_RE.findall(text))
    return round_step(h + m / 60) if h or m else None


def _find_date(text):
    for candidate in _DATE_RE.findall(text):
        try:
            return parse_date(candidate)
        except PlanError:
            pass
    return None


def parse_syllabus(text):
    """Extract subject, exam date and units from free-form syllabus text. Missing hours are estimated and warned."""
    subject, exam, level, units, warnings = None, None, None, [], []
    for raw in str(text or "").splitlines():
        line = raw.strip().lstrip("#").strip()
        if not line:
            continue
        m = _LEVEL_LINE_RE.match(line)
        if m:
            try:
                level = parse_level(m.group(1))
            except PlanError:
                warnings.append(f"Unknown level '{m.group(1)}' - using medium.")
            continue
        m = _SUBJECT_RE.match(line)
        if m:
            subject = re.split(r"[(\[]|\bexam", m.group(1), flags=re.I)[0].strip(" -–:,")
            exam = exam or (_find_date(line) if re.search(r"exam", line, re.I) else None)
            continue
        if _META_RE.match(line):
            continue  # "Total: 40 hours", "Source: ...", "Credits: 4" are not units
        if re.search(r"\bexam", line, re.I) and _find_date(line) and not parse_hours(line):
            exam = exam or _find_date(line)
            continue
        has_prefix = bool(_PREFIX_RE.match(line))
        hours = parse_hours(line)
        if not has_prefix and hours is None:
            continue  # headings and free text
        tag = _TAG_RE.search(line)
        topic = _DURATION_RE.sub(" ", _TAG_RE.sub(" ", _PREFIX_RE.sub("", line, count=1)))
        topic = re.sub(r"\(\s*\)|\[\s*\]", " ", topic)
        topic = " ".join(topic.split()).strip(" -–:,;.")
        if topic:
            units.append({"topic": topic, "hours": hours, "tag": parse_level(tag.group(1)) if tag else None})
    known = [u["hours"] for u in units if u["hours"]]
    default = round_step(sum(known) / len(known)) if known else 4.0
    for u in units:
        if u["hours"] is None:
            u["hours"] = default
            warnings.append(f"No hours given for '{u['topic']}' - assumed {default:g}h.")
    mean = sum(u["hours"] for u in units) / len(units) if units else 0
    for u in units:  # tagged difficulty wins; otherwise subject level, nudged by how long the topic is
        d = u.pop("tag")
        if d is None:
            d = (level or 2) + (u["hours"] >= 1.3 * mean) - (u["hours"] <= 0.7 * mean)
        u["difficulty"] = LEVEL_NAMES[min(3, max(1, d))]
    if not subject:
        warnings.append("Subject name not found.")
    if not exam:
        warnings.append("Exam date not found - ask the user for it.")
    if not units:
        warnings.append("No units found.")
    return {"subject": subject, "exam_date": exam.isoformat() if exam else None,
            "level": LEVEL_NAMES[level or 2], "units": units, "total_hours": sum(u["hours"] for u in units), "warnings": warnings}


# ---------- Schedule building ----------
def validate_units(units):
    """Check units are [{topic, hours>0}], round hours to STEP and make topic names unique."""
    if not isinstance(units, list) or not units:
        raise PlanError('units must be a non-empty list like [{"topic": "Intro", "hours": 4}].')
    clean, seen = [], set()
    for i, u in enumerate(units, 1):
        if not isinstance(u, dict):
            raise PlanError(f"Unit {i} must be an object with 'topic' and 'hours'.")
        topic = " ".join(str(u.get("topic") or "").split())
        if not topic:
            raise PlanError(f"Unit {i} has no topic.")
        try:
            hours = float(u.get("hours"))
        except (TypeError, ValueError):
            raise PlanError(f"Unit {i} ('{topic}') has invalid hours {u.get('hours')!r}.") from None
        if not math.isfinite(hours) or hours <= 0 or hours > MAX_UNIT_HOURS:
            raise PlanError(f"Unit {i} ('{topic}') hours must be between 0 and {MAX_UNIT_HOURS}, got {hours:g}.")
        if topic.lower() == REVISION.lower():
            topic = f"{topic} (unit {i})"  # 'Revision' is reserved for the revision days
        base, n = topic, 2
        while topic.lower() in seen:
            topic, n = f"{base} ({n})", n + 1
        seen.add(topic.lower())
        clean.append({"topic": topic, "hours": round_step(hours), "difficulty": parse_level(u.get("difficulty"), 2)})
    return clean


def _hours_needed(total, days, booked):
    """Smallest hours/day (multiple of STEP, up to 24) whose free capacity holds `total`, else None."""
    x = STEP
    while x <= 24:
        if sum(floor_step(max(0.0, x - booked.get(d, 0))) for d in days) >= total - 1e-9:
            return x
        x += STEP
    return None


def build_plan(units, start, exam, hours_per_day, revision_days=3, booked=None):
    """Spread units (in order) evenly over the days before the revision period.

    `booked` maps date -> hours already used that day (other subjects), so the daily cap is shared.
    Returns (sessions, info): sessions are (date, topic, hours) tuples, info has warnings and buffer days.
    """
    booked = booked or {}
    hpd, rev = float(hours_per_day), int(revision_days)
    if not 0 < hpd <= MAX_HOURS_PER_DAY:
        raise PlanError(f"hours_per_day must be between 0 and {MAX_HOURS_PER_DAY}.")
    if not 0 <= rev <= MAX_REVISION_DAYS:
        raise PlanError(f"revision_days must be between 0 and {MAX_REVISION_DAYS}.")
    if exam <= start:
        raise PlanError(f"The exam ({exam}) must be after the start date ({start}).")
    days_left = (exam - start).days
    study_days = [start + timedelta(days=i) for i in range(days_left - rev)]
    total = sum(u["hours"] for u in units)
    if units and not study_days:
        raise PlanError(f"Only {days_left} day(s) before the exam, which is not more than revision_days={rev}. "
                        f"Use revision_days={max(0, days_left - 1)} or less.")
    cap = {d: floor_step(max(0.0, hpd - booked.get(d, 0))) for d in study_days}
    if total > sum(cap.values()) + 1e-9:
        need = _hours_needed(total, study_days, booked)
        if need is None or need > MAX_HOURS_PER_DAY:
            raise PlanError(f"Not feasible: {total:g}h of study cannot fit into {len(study_days)} day(s) even at "
                            f"{MAX_HOURS_PER_DAY}h/day. Reduce revision_days, cut content, or check the exam date.")
        raise PlanError(f"Cannot fit {total:g}h into {len(study_days)} study day(s) at {hpd:g}h/day "
                        f"({sum(cap.values()):g}h free). Use hours_per_day={need:g} or reduce revision_days.")

    # Smallest even daily quota that still fits everything -> work is spread out, not front-loaded.
    quota = STEP
    while sum(min(quota, c) for c in cap.values()) < total - 1e-9:
        quota += STEP
    sessions, queue, i = [], [[u["topic"], u["hours"]] for u in units], 0
    for d in study_days:
        left = min(quota, cap[d])
        while left > 1e-9 and i < len(queue):
            take = min(left, queue[i][1])
            sessions.append((d, queue[i][0], take))
            left -= take
            queue[i][1] -= take
            if queue[i][1] <= 1e-9:
                i += 1
    last_used = max((d for d, _, _ in sessions), default=None)
    warnings = []
    for k in range(rev, 0, -1):
        d = exam - timedelta(days=k)
        if d < start:
            continue
        h = floor_step(max(0.0, hpd - booked.get(d, 0)))
        if h > 0:
            sessions.append((d, REVISION, h))
        else:
            warnings.append(f"No free time for revision on {d} (other subjects use the whole day).")
    return sessions, {"warnings": warnings, "daily_quota": quota,
                      # free days between the last study session and the revision period
                      "buffer_days": sum(1 for d in study_days if d > last_used) if last_used else 0}


# ---------- Several subjects planned together ----------
def prepare_units(units, done_hours=None, hard_first=True):
    """Add extra practice time to hard topics, subtract hours already done, and order hard topics first."""
    done_hours = done_hours or {}
    out = []
    for u in units:
        weighted = math.ceil(u["hours"] * (1 + HARD_EXTRA) / STEP - 1e-9) * STEP if u["difficulty"] == 3 else u["hours"]
        left = floor_step(weighted - done_hours.get(u["topic"], 0))
        if left > 0:
            out.append({**u, "hours": left})
    return sorted(out, key=lambda u: -u["difficulty"]) if hard_first else out


def _ceil_half(x):
    return math.ceil(x * 2 - 1e-9) / 2


def _simulate(subjects, start, hpd, rev, fill):
    """Allocate day by day. Revision is reserved first; then each day subjects are served earliest-deadline
    first (harder first on ties). Balanced mode gives each subject just its required pace; fill mode
    (the fallback) packs every free hour, which always finds a plan when one exists."""
    last_exam = max(s["exam"] for s in subjects)
    days = [start + timedelta(days=i) for i in range((last_exam - start).days)]
    cap = {d: hpd for d in days}
    sessions, warnings = [], []
    rev_hours = max(STEP, floor_step(min(2.0, hpd / 2)))
    for s in sorted(subjects, key=lambda s: s["exam"]):
        for k in range(rev, 0, -1):
            d = s["exam"] - timedelta(days=k)
            if d < start:
                continue
            h = min(rev_hours, cap[d])
            if h > 0:
                sessions.append((d, s["name"], REVISION, h))
                cap[d] -= h
            else:
                warnings.append(f"No time left for {s['name']} revision on {d}.")
    queue = {s["name"]: [[u["topic"], u["hours"]] for u in s["units"]] for s in subjects}
    left = {s["name"]: sum(u["hours"] for u in s["units"]) for s in subjects}
    cutoff = {s["name"]: s["exam"] - timedelta(days=rev + 1) for s in subjects}
    order = sorted(subjects, key=lambda s: (cutoff[s["name"]], -s["level"]))
    block = min(1.0, max(0.5, floor_step(hpd / 2)))  # study in real blocks, not 15-minute slivers
    owed = {s["name"]: block for s in subjects}  # every subject is ready for a block on day one
    for d in days:
        free = cap[d]
        for s in order:
            name = s["name"]
            if left[name] <= 1e-9 or d > cutoff[name]:
                continue
            if fill:
                want = left[name]
            else:  # accumulate the daily pace and study once a full block is owed
                days_left = sum(1 for x in days if d <= x <= cutoff[name] and cap[x] > 0)
                pace = left[name] / max(days_left, 1) * PACE_FACTOR[s["level"]]
                owed[name] += pace
                if owed[name] < block - 1e-9 and days_left > 1:
                    continue
                want = min(_ceil_half(owed[name]), max(2 * block, _ceil_half(pace)))  # no marathon days
            if free <= 1e-9:
                continue
            give = floor_step(min(want, left[name], free))
            free -= give
            left[name] -= give
            owed[name] -= give
            q = queue[name]
            while give > 1e-9 and q:
                take = min(give, q[0][1])
                sessions.append((d, name, q[0][0], take))
                give -= take
                q[0][1] -= take
                if q[0][1] <= 1e-9:
                    q.pop(0)
        cap[d] = free
    missing = {n: h for n, h in left.items() if h > 1e-9}
    return sorted(sessions, key=lambda x: (x[0], x[1])), warnings, missing


def build_multi_plan(subjects, start, hours_per_day, revision_days=3):
    """Plan several subjects at once under one shared daily limit.

    subjects: [{"name", "exam": date, "level": 1-3, "units": [{"topic", "hours", "difficulty"}]}] where units are
    already prepared (see prepare_units). Returns (sessions, info); sessions are (date, subject, topic, hours).
    """
    hpd, rev = float(hours_per_day), int(revision_days)
    if not 0 < hpd <= MAX_HOURS_PER_DAY:
        raise PlanError(f"hours_per_day must be between 0 and {MAX_HOURS_PER_DAY}.")
    if not 0 <= rev <= MAX_REVISION_DAYS:
        raise PlanError(f"revision_days must be between 0 and {MAX_REVISION_DAYS}.")
    if not subjects:
        raise PlanError("No subjects to plan.")
    for s in subjects:
        if s["exam"] <= start:
            raise PlanError(f"{s['name']}: the exam ({s['exam']}) must be after {start}.")
        if s["units"] and (s["exam"] - start).days <= rev:
            raise PlanError(f"{s['name']}: only {(s['exam'] - start).days} day(s) to the exam - use revision_days="
                            f"{max(0, (s['exam'] - start).days - 1)} or less.")
    for fill in (False, True):
        sessions, warnings, missing = _simulate(subjects, start, hpd, rev, fill)
        if not missing:
            return sessions, {"warnings": warnings, "mode": "front-loaded" if fill else "balanced"}
    short = ", ".join(f"{n} is short by {h:g}h" for n, h in missing.items())
    need = next((x / 2 for x in range(int(hpd * 2) + 1, MAX_HOURS_PER_DAY * 2 + 1)
                 if not _simulate(subjects, start, x / 2, rev, True)[2]), None)
    if need is None:
        raise PlanError(f"Not feasible even at {MAX_HOURS_PER_DAY}h/day ({short}). Reduce revision_days or content.")
    raise PlanError(f"Cannot fit everything at {hpd:g}h/day ({short}). Use hours_per_day={need:g} or reduce revision_days.")
