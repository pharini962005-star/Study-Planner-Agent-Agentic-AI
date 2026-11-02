"""Generate sample syllabus files in data/ from the Hugging Face dataset azrai99/coursera-course-dataset.

Each Coursera course becomes one subject: its skills become units and its total hours are split across them.
    python scripts/make_syllabi.py                 # download from Hugging Face (needs internet)
    python scripts/make_syllabi.py --offline       # use the saved rows in scripts/coursera_rows.json
"""
import ast
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
CACHE = os.path.join(ROOT, "scripts", "coursera_rows.json")
DATASET = "azrai99/coursera-course-dataset"
API = "https://datasets-server.huggingface.co/search"
QUERIES = ["data structures", "statistics", "database sql", "operating systems networks"]
EXAM_OFFSETS = [23, 29, 33, 40]  # days from today, so the exams are staggered


def total_hours(schedule):
    """'15 hours to complete (...)' -> 15. Multi-month specializations are skipped (too long for one exam)."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*hours? to complete", schedule or "")
    return float(m.group(1)) if m else None


def skills(row):
    raw = row.get("Skills") or "[]"
    items = ast.literal_eval(raw) if isinstance(raw, str) else raw
    seen, out = set(), []
    for s in items:
        s = " ".join(str(s).split())
        if s and s.lower() not in seen and "(" not in s:
            seen.add(s.lower())
            out.append(s[0].upper() + s[1:])
    return out


def usable(row):
    return total_hours(row.get("Schedule")) and len(skills(row)) >= 4 and "exam" not in row["title"].lower()


def row_to_syllabus(row, exam_date):
    units, hours = skills(row), total_hours(row["Schedule"])
    quarters = round(hours * 4)  # split total hours across units in 15-minute steps
    share = [quarters // len(units) + (i < quarters % len(units)) for i in range(len(units))]
    lines = [f"Subject: {row['title']} (Exam: {exam_date.isoformat()})",
             f"Source: Hugging Face dataset {DATASET} - course by {row['Organization']}"]
    if row.get("Level"):
        lines.append(f"Level: {row['Level'].replace(' level', '')}")
    lines += [f"Unit {i}: {u} ({q / 4:g} hours)" for i, (u, q) in enumerate(zip(units, share), 1)]
    return "\n".join(lines) + "\n"


def fetch(query):
    url = API + "?" + urllib.parse.urlencode({"dataset": DATASET, "config": "default", "split": "train",
                                              "query": query, "offset": 0, "length": 20})
    with urllib.request.urlopen(url, timeout=30) as resp:
        return [r["row"] for r in json.load(resp)["rows"]]


def file_name(row):
    return re.sub(r"[^a-z0-9]+", "_", row["title"].lower()).strip("_") + ".txt"


def load_cached():
    with open(CACHE, encoding="utf-8") as f:
        return json.load(f)


def write_syllabi(rows, data_dir=DATA_DIR):
    """Write one syllabus per row with exams staggered from today; returns the file names."""
    os.makedirs(data_dir, exist_ok=True)
    names = []
    for row, offset in zip(rows, EXAM_OFFSETS):
        names.append(file_name(row))
        with open(os.path.join(data_dir, names[-1]), "w", encoding="utf-8") as f:
            f.write(row_to_syllabus(row, date.today() + timedelta(days=offset)))
    return names


def main():
    if "--offline" in sys.argv:
        rows = load_cached()
    else:
        rows = []
        for q in QUERIES:
            pick = next((r for r in fetch(q) if usable(r) and r["title"] not in {x["title"] for x in rows}), None)
            if pick:
                rows.append(pick)
            else:
                print(f"No usable course for '{q}'")
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump([{k: r.get(k) for k in ("title", "Organization", "Level", "Skills", "Schedule")} for r in rows], f, indent=1)
    for name in write_syllabi(rows):
        print(f"data/{name}")


if __name__ == "__main__":
    main()
