"""Draw the database schema (ER diagram of data/planner.db) to schema.png with matplotlib:
    python scripts/draw_schema.py
"""
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.png")
ROW_H, HEAD_H = 4.2, 5.5

# (column, type, key, description)
TABLES = {
    "subjects": ("#7C3AED", "one row per subject / exam", [
        ("name", "TEXT", "PK", "subject name, e.g. 'Data Structures in C'"),
        ("exam_date", "TEXT", "", "YYYY-MM-DD"),
        ("hours_per_day", "REAL", "", "daily study limit used for the plan"),
        ("revision_days", "INTEGER", "", "revision days before the exam"),
        ("reminder_time", "TEXT", "", "HH:MM for daily reminders"),
        ("units", "TEXT (JSON)", "", "syllabus units - see structure below"),
        ("level", "INTEGER", "", "subject difficulty 1 easy / 2 medium / 3 hard"),
    ]),
    "sessions": ("#059669", "one row per study block (the timetable)", [
        ("id", "INTEGER", "PK", "auto-increment"),
        ("subject", "TEXT", "FK", "→ subjects.name"),
        ("topic", "TEXT", "", "unit topic, or 'Revision'"),
        ("study_date", "TEXT", "IDX", "YYYY-MM-DD (indexed)"),
        ("hours", "REAL", "", "length, in 0.25 h steps"),
        ("done", "INTEGER", "", "0 pending / 1 completed (default 0)"),
    ]),
    "reminders": ("#D97706", "one row per reminder", [
        ("id", "INTEGER", "PK", "auto-increment"),
        ("message", "TEXT", "", "e.g. 'Study DS in C: Pointers (2h)'"),
        ("remind_at", "TEXT", "", "YYYY-MM-DD HH:MM"),
        ("subject", "TEXT", "FK", "→ subjects.name (NULL = custom reminder)"),
    ]),
}
KEY_COLORS = {"PK": "#DC2626", "FK": "#2563EB", "IDX": "#6B7280"}


def table(ax, x, y_top, w, name):
    color, caption, cols = TABLES[name]
    h = HEAD_H + ROW_H * len(cols)
    ax.add_patch(FancyBboxPatch((x, y_top - h), w, h, boxstyle="round,pad=0,rounding_size=0.8",
                                fc="white", ec=color, lw=2))
    ax.add_patch(Rectangle((x, y_top - HEAD_H), w, HEAD_H, fc=color, ec=color))
    ax.text(x + 1.2, y_top - HEAD_H / 2, name, color="white", fontsize=14, fontweight="bold", va="center")
    ax.text(x + w - 1.2, y_top - HEAD_H / 2, caption, color="white", fontsize=8.5, va="center", ha="right", style="italic")
    rows = {}
    for i, (col, typ, key, desc) in enumerate(cols):
        cy = y_top - HEAD_H - ROW_H * (i + 0.5)
        if i % 2:
            ax.add_patch(Rectangle((x, cy - ROW_H / 2), w, ROW_H, fc="#F9FAFB", ec="none"))
        if key:
            ax.text(x + 1.0, cy, key, fontsize=7.5, fontweight="bold", color="white", va="center",
                    bbox=dict(boxstyle="round,pad=0.25", fc=KEY_COLORS[key], ec="none"))
        ax.text(x + 5.2, cy + 0.75, col, fontsize=10.5, fontweight="bold" if key == "PK" else "normal", va="center")
        ax.text(x + w - 1.0, cy + 0.75, typ, fontsize=9, color="#6B7280", va="center", ha="right", family="monospace")
        ax.text(x + 5.2, cy - 1.1, desc, fontsize=7.8, color="#4B5563", va="center")
        rows[col] = cy
    return {"x": x, "w": w, "rows": rows, "bottom": y_top - h}


def relation(ax, p, q, label):
    """subjects (1) ──< (N) child. p is the parent side, q the child side."""
    mx = (p[0] + q[0]) / 2  # elbow: across, up/down, across
    ax.plot([p[0], mx, mx, q[0]], [p[1], p[1], q[1], q[1]], color="#374151", lw=1.8)
    dx = 1 if q[0] > p[0] else -1
    ax.text(p[0] + dx * 1.2, p[1] - 1.9, "1", fontsize=11, fontweight="bold", color="#374151", ha="center")
    ax.text(q[0] - dx * 1.4, q[1] - 2.0, "N", fontsize=11, fontweight="bold", color="#374151", ha="center")
    for k in (-0.9, 0, 0.9):  # crow's foot on the "many" side
        ax.plot([q[0] - dx * 1.6, q[0]], [q[1], q[1] + k], color="#374151", lw=1.4)
    ax.text(mx, min(p[1], q[1]) - 4.5, label, fontsize=8.5, style="italic", ha="center", va="top",
            color="#374151", linespacing=1.3)


fig, ax = plt.subplots(figsize=(18, 10.5), dpi=150)
ax.set_xlim(0, 120)
ax.set_ylim(0, 70)
ax.axis("off")
fig.patch.set_facecolor("white")
ax.text(60, 69.5, "Study Planner Agent - Database Schema (SQLite: data/planner.db)", ha="center", va="top",
        fontsize=17, fontweight="bold")
ax.text(60, 66.3, "Long-term memory of the agent: what to study (subjects), when (sessions) and the alerts (reminders)",
        ha="center", va="top", fontsize=10.5, color="#4B5563")

sess = table(ax, 2, 61, 34, "sessions")
subj = table(ax, 43, 61, 34, "subjects")
rem = table(ax, 84, 61, 34, "reminders")
relation(ax, (subj["x"], subj["rows"]["name"]), (sess["x"] + sess["w"], sess["rows"]["subject"]), "1 subject\nhas many\nsessions")
relation(ax, (subj["x"] + subj["w"], subj["rows"]["name"]), (rem["x"], rem["rows"]["subject"]), "1 subject\nhas many\nreminders")

# JSON structure stored in subjects.units
jx, jy, jw, jh = 43, 3, 34, 19.5
ax.add_patch(FancyBboxPatch((jx, jy), jw, jh, boxstyle="round,pad=0,rounding_size=0.8", fc="#F5F3FF", ec="#7C3AED", lw=1.5, ls="--"))
ax.text(jx + 1.2, jy + jh - 1.4, "subjects.units  (JSON array)", fontsize=11, fontweight="bold", color="#5B21B6", va="top")
ax.text(jx + 1.2, jy + jh - 4.6,
        '[\n  { "topic": "Pointers",\n    "hours": 3.75,\n    "difficulty": 3, // 1 easy..3 hard\n'
        '    "rated": true    // by user/agent\n  },\n  ...\n]',
        fontsize=9, family="monospace", va="top", color="#1F2937", linespacing=1.4)
ax.annotate("", xy=(jx + jw / 2, jy + jh), xytext=(jx + jw / 2, subj["bottom"]),
            arrowprops=dict(arrowstyle="-|>", lw=1.4, color="#7C3AED", ls="--"))

# Notes
notes = ("Notes\n"
         "• PK = primary key\n"
         "• FK = foreign key (linked by subject name)\n"
         "• IDX = indexed column (idx_sessions_date)\n"
         "• Re-planning deletes only pending sessions\n"
         "  (done = 0); completed ones are kept and their\n"
         "  hours are subtracted from what is left.\n"
         "• log_progress can split a session: the studied\n"
         "  part becomes a new row with done = 1.\n"
         "• Dates are ISO text, so they sort as strings.")
ax.text(2.5, 21.5, notes, fontsize=9, va="top", color="#374151", linespacing=1.45,
        bbox=dict(boxstyle="round,pad=0.8", fc="#F9FAFB", ec="#D1D5DB"))
ax.text(84, 21.5, "Example rows\n\nsessions:  #62 | Machine Learning | Introduction,\n"
        "           Linear Regression | 2026-11-02 | 1.0 | 1\n\nreminders: 2026-11-03 18:00 | 'Study Database\n"
        "           Management...: Software Development (2h)'\n\nsubjects:  Data Structures in C | 2026-11-25 |\n"
        "           4.0 | 3 | 18:00 | [...] | 2",
        fontsize=8.2, va="top", family="monospace", color="#374151", linespacing=1.35,
        bbox=dict(boxstyle="round,pad=0.8", fc="#F9FAFB", ec="#D1D5DB"))

plt.savefig(OUT, bbox_inches="tight", facecolor="white")
print(f"Saved {OUT}")
