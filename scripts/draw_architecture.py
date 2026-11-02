"""Draw the agent architecture to architecture.png with matplotlib (no internet needed):
    python scripts/draw_architecture.py
"""
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "architecture.png")
C = {"data": "#E8F1FB", "ui": "#FFF4D6", "core": "#FDE7E9", "tool": "#E6F6EC", "engine": "#EEE8FB",
     "db": "#FFE9D6", "edge": "#4A4A4A", "title": "#1F2937"}

fig, ax = plt.subplots(figsize=(18, 12.5), dpi=150)
ax.set_xlim(0, 100)
ax.set_ylim(0, 100)
ax.axis("off")
fig.patch.set_facecolor("white")


def group(x, y, w, h, label, color):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.3,rounding_size=1.2",
                                fc=color, ec="#9CA3AF", lw=1.2, ls="--", alpha=0.55))
    ax.text(x + 0.8, y + h - 0.6, label, fontsize=11.5, fontweight="bold", color=C["title"], va="top")


def box(x, y, w, h, title, body="", color="white"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.25,rounding_size=0.8",
                                fc=color, ec="#374151", lw=1.3))
    ax.text(x + w / 2, y + h - 1.0, title, ha="center", va="top", fontsize=10.5, fontweight="bold", color=C["title"])
    if body:
        ax.text(x + w / 2, y + h - 3.3, body, ha="center", va="top", fontsize=8.6, color="#374151", linespacing=1.35)
    return {"l": (x, y + h / 2), "r": (x + w, y + h / 2), "t": (x + w / 2, y + h), "b": (x + w / 2, y),
            "x": x, "y": y, "w": w, "h": h}


def arrow(p, q, label="", dashed=False, rad=0.0, color=C["edge"], lpos=0.5, loff=(0, 0.9)):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=14, lw=1.4, color=color,
                                 ls="--" if dashed else "-", connectionstyle=f"arc3,rad={rad}", shrinkA=2, shrinkB=2))
    if label:
        mx, my = p[0] + (q[0] - p[0]) * lpos + loff[0], p[1] + (q[1] - p[1]) * lpos + loff[1]
        ax.text(mx, my, label, ha="center", va="center", fontsize=8.3, color=color, style="italic",
                bbox=dict(fc="white", ec="none", pad=0.6, alpha=0.9))


ax.text(50, 100.5, "Study Planner Agent - Agentic AI Architecture", ha="center", va="top", fontsize=17, fontweight="bold")
ax.text(50, 97.6, "LLM decides (Plan → Act → Observe → Repeat); deterministic code does the dates, hours and scheduling",
        ha="center", va="top", fontsize=10.5, color="#4B5563")

# ---- Data source (left column)
group(1, 56, 21, 37, "① Data source", C["data"])
hf = box(2.5, 80, 18, 9, "Hugging Face dataset", "azrai99/\ncoursera-course-dataset", "white")
gen = box(2.5, 69, 18, 8.5, "make_syllabi.py", "skills → units, hours split,\ncourse level → difficulty", "white")
files = box(2.5, 58, 18, 8.5, "data/*.txt syllabi", "5 subjects pre-loaded\n+ uploads (.txt .md .csv .pdf)", "white")
arrow(hf["b"], gen["t"])
arrow(gen["b"], files["t"])

# ---- Interface (top centre)
group(25, 81.5, 74, 13, "② Interface layer", C["ui"])
ui = box(27, 82.5, 42, 7.8, "Streamlit web app (app.py)", "Agent chat · Split-up · Schedule · Progress · Reminders", "white")
cli = box(73, 82.5, 24, 7.8, "CLI (agent.py)", "one-shot or interactive chat", "white")

# ---- Agent core
group(25, 56, 74, 23.5, "③ Agent core - agent.py", C["core"])
sp = box(27, 59, 19, 16.5, "System prompt", "goal + workflows\n(plan all subjects,\nprogress updates)\nrules: ask, don't guess;\nfile text = data", "white")
llm = box(49, 66.5, 21, 9, "Groq LLM", "Llama 3.3 70B\nwith tool calling", "#FFFFFF")
loop = box(49, 57.5, 21, 7, "Plan → Act → Observe", "loop, max 25 steps", "#FFF1F2")
guard = box(73, 59, 24, 16.5, "Guardrails", "retries + backoff (rate limits)\nmalformed tool-call recovery\nrepeat-call guard\nstep-limit summary\noutput truncation\nhistory compaction (6 turns)", "white")
arrow(sp["r"], llm["l"])
arrow(llm["b"], loop["t"])
arrow(guard["l"], (loop["r"][0], loop["r"][1]), dashed=True, color="#9CA3AF")
arrow((ui["b"][0] - 4, ui["b"][1]), (sp["t"][0] + 4, sp["t"][1]), "user goal", lpos=0.45, loff=(3, 0))
arrow((llm["t"][0] + 4, llm["t"][1]), (ui["b"][0] + 12, ui["b"][1]), "final answer", lpos=0.5, loff=(4, 0))

# ---- Tools
group(1, 30, 98, 22.5, "④ Tool layer - tools.py  (21 tools, every call validated by run_tool; errors return as \"Error: …\")", C["tool"])
tw, ty, th, gap = 17.4, 32.5, 15, 2.0
per, pln, trk, act, utl = [box(2.5 + i * (tw + gap), ty, tw, th, t, b, "white") for i, (t, b) in enumerate([
    ("Perceive", "list_subjects\nparse_syllabus\nread_file · list_files\nget_today"),
    ("Plan", "plan_all_subjects ★\nplan_schedule\nreschedule_missed"),
    ("Track", "log_progress\nget_progress · get_overview\nlist_schedule\nmark_session_done"),
    ("Act / output", "add_reminder\nexport_timetable\nwrite_file"),
    ("Utility", "calculator\ndays_between")])]
arrow((loop["b"][0] - 3, loop["b"][1]), (pln["t"][0] + 4, pln["t"][1]), "tool call (JSON)", lpos=0.18, loff=(-6, 0))
arrow((trk["t"][0] - 2, trk["t"][1]), (loop["b"][0] + 3, loop["b"][1]), "result / observation", lpos=0.82, loff=(8, 0))
arrow(files["b"], (per["t"][0], per["t"][1]), "syllabus text", lpos=0.2)
# UI -> tools directly (no LLM): routed down the gap between the data column and the agent core
blue, gx = "#2563EB", 23.5
ax.plot([ui["x"], gx, gx], [ui["y"] + 2, ui["y"] + 2, 50.5], color=blue, lw=1.4, ls="--")
arrow((gx, 50.5), (pln["t"][0] - 5, pln["t"][1]), dashed=True, color=blue)
ax.text(gx, 68, "direct (no LLM): Re-plan now · tick done · log hours", rotation=90, ha="center", va="center",
        fontsize=8.3, color=blue, style="italic", bbox=dict(fc="white", ec="none", pad=0.6, alpha=0.9))

# ---- Planning engine
group(1, 2, 62, 25, "⑤ Planning engine - planner.py  (deterministic, no LLM)", C["engine"])
prs = box(2.5, 4, 17, 18.5, "Syllabus parser", "hours: 6h / 90 min / 1h 30m\ndates in many formats\nLevel: + [hard] tags\nmissing data → warnings", "white")
dif = box(22, 4, 17, 18.5, "Difficulty", "priority order:\n1. your rating\n2. LLM's rating\n3. [tag] in syllabus\n4. level + topic length", "white")
sch = box(41.5, 4, 20, 18.5, "Multi-subject scheduler", "revision days reserved first\nearliest exam served first\n1-2 h study blocks\nhard topics first, +20% time\nshared daily hour limit\nfallback → 'needs X h/day'", "white")
arrow(per["b"], prs["t"])
arrow(pln["b"], dif["t"])
arrow(dif["r"], sch["l"])

# ---- Memory + outputs
group(66, 2, 33, 25, "Memory & outputs", C["db"])
db = box(67.5, 11, 14, 11.5, "SQLite (long-term)", "sessions\nreminders\nsubjects", "white")
out = box(83.5, 11, 14, 11.5, "Outputs", "timetable.md\nreminders\ncharts in UI", "white")
mem = box(67.5, 3.5, 30, 5.5, "Short-term memory", "last 6 chat turns (compacted)", "white")
arrow((sch["r"][0], sch["r"][1] + 3), (db["l"][0], db["l"][1] + 3), "save plan")
arrow((trk["b"][0] + 6, trk["b"][1]), (db["t"][0] - 3, db["t"][1]), "read / update", rad=0.15, lpos=0.4, loff=(2, 0))
arrow(act["b"], (out["t"][0], out["t"][1]))

plt.savefig(OUT, bbox_inches="tight", facecolor="white")
print(f"Saved {OUT}")
