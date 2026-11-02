"""Offline tests (no Groq calls):  python -m unittest discover tests -v"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from types import SimpleNamespace as NS

TMP = tempfile.mkdtemp()
os.environ["PLANNER_DATA_DIR"] = TMP  # must be set before tools is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import planner  # noqa: E402
import tools  # noqa: E402

TODAY = date.today()


def d(n):
    return (TODAY + timedelta(days=n)).isoformat()


UNITS = [{"topic": "A", "hours": 6}, {"topic": "B", "hours": 8}, {"topic": "C", "hours": 5}]


class Base(unittest.TestCase):
    def setUp(self):
        tools.run_tool("clear_schedule", {"subject": "all"})
        for f in os.listdir(TMP):  # every test starts with an empty data folder
            if f != "planner.db":
                os.remove(os.path.join(TMP, f))


class TestParsing(Base):
    def test_sample_syllabus(self):
        text = ("Subject: Machine Learning (Exam: 2026-12-10)\n"
                "Unit 1: Introduction, Linear Regression (6 hours)\nUnit 2: Classification, SVM (8 hours)\n")
        r = planner.parse_syllabus(text)
        self.assertEqual(r["subject"], "Machine Learning")
        self.assertEqual(r["exam_date"], "2026-12-10")
        self.assertEqual(r["units"], [{"topic": "Introduction, Linear Regression", "hours": 6.0, "difficulty": "medium"},
                                      {"topic": "Classification, SVM", "hours": 8.0, "difficulty": "medium"}])

    def test_difficulty_from_level_tags_and_hours(self):
        r = planner.parse_syllabus("Subject: X\nLevel: Advanced\nUnit 1: Intro (2h) [easy]\nUnit 2: Core (4h)\n"
                                   "Unit 3: Deep stuff (8h)\nUnit 4: Extra (2h)")
        self.assertEqual(r["level"], "hard")
        self.assertEqual([(u["topic"], u["difficulty"]) for u in r["units"]],
                         [("Intro", "easy"), ("Core", "hard"), ("Deep stuff", "hard"), ("Extra", "medium")])
        self.assertEqual(planner.parse_level("Beginner level"), 1)
        with self.assertRaises(planner.PlanError):
            planner.parse_level("impossible")

    def test_formats_and_missing_hours(self):
        text = ("# Course: DBMS\nExam date: 10th December 2026\n- ER model - 3 hrs\n2. SQL 1h 30m\n"
                "Module 3: Transactions\nIntro paragraph without hours")
        r = planner.parse_syllabus(text)
        self.assertEqual(r["subject"], "DBMS")
        self.assertEqual(r["exam_date"], "2026-12-10")
        self.assertEqual([u["topic"] for u in r["units"]], ["ER model", "SQL", "Transactions"])
        self.assertEqual(r["units"][1]["hours"], 1.5)
        self.assertEqual(r["units"][2]["hours"], 2.25)  # average of known hours
        self.assertTrue(any("Transactions" in w for w in r["warnings"]))

    def test_metadata_lines_are_not_units(self):
        r = planner.parse_syllabus("Subject: X\nSource: some course, 15 hours total\nTotal: 40 hours\nCredits: 4\nUnit 1: A (2h)")
        self.assertEqual(r["units"], [{"topic": "A", "hours": 2.0, "difficulty": "medium"}])

    def test_dataset_rows_to_syllabus(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        import make_syllabi
        with open(make_syllabi.CACHE, encoding="utf-8") as f:
            rows = json.load(f)
        self.assertGreaterEqual(len(rows), 3)
        for row in rows:
            r = planner.parse_syllabus(make_syllabi.row_to_syllabus(row, date(2030, 1, 1)))
            self.assertEqual(r["subject"], row["title"])
            self.assertEqual(r["exam_date"], "2030-01-01")
            self.assertEqual(r["total_hours"], make_syllabi.total_hours(row["Schedule"]))
            self.assertEqual(r["warnings"], [])

    def test_nothing_found(self):
        r = planner.parse_syllabus("hello world")
        self.assertEqual(r["units"], [])
        self.assertIn("Exam date not found - ask the user for it.", r["warnings"])

    def test_dates(self):
        for s in ["2026-12-10", "10/12/2026", "10 Dec 2026", "December 10th, 2026", "Dec 10 2026"]:
            self.assertEqual(planner.parse_date(s), date(2026, 12, 10), s)
        with self.assertRaises(planner.PlanError):
            planner.parse_date("next friday")

    def test_validate_units(self):
        for bad in [[], "x", [{"topic": "", "hours": 2}], [{"topic": "A", "hours": 0}],
                    [{"topic": "A", "hours": -1}], [{"topic": "A", "hours": "abc"}], [{"topic": "A", "hours": float("nan")}]]:
            with self.assertRaises(planner.PlanError, msg=bad):
                planner.validate_units(bad)
        clean = planner.validate_units([{"topic": "A", "hours": "2"}, {"topic": "a", "hours": 1.1}, {"topic": "Revision", "hours": 1}])
        self.assertEqual([u["topic"] for u in clean], ["A", "a (2)", "Revision (unit 3)"])
        self.assertEqual(clean[1]["hours"], 1.0)


class TestBuildPlan(unittest.TestCase):
    def test_spreads_evenly_and_keeps_order(self):
        start = TODAY
        sessions, info = planner.build_plan(planner.validate_units(UNITS), start, start + timedelta(days=23), 2, 3)
        study = [s for s in sessions if s[1] != planner.REVISION]
        self.assertEqual(sum(s[2] for s in study), 19)
        self.assertEqual([s[1] for s in study], sorted([s[1] for s in study]))  # A..B..C order
        self.assertLessEqual(info["daily_quota"], 1)  # 19h over 20 days -> ~1h/day, not front-loaded at 2h
        self.assertEqual([s[0] for s in sessions if s[1] == planner.REVISION],
                         [start + timedelta(days=k) for k in (20, 21, 22)])

    def test_daily_cap_never_exceeded(self):
        sessions, _ = planner.build_plan(planner.validate_units(UNITS), TODAY, TODAY + timedelta(days=12), 2, 2)
        per_day = {}
        for day, _, h in sessions:
            per_day[day] = per_day.get(day, 0) + h
        self.assertTrue(all(h <= 2 for h in per_day.values()))

    def test_cannot_fit_suggests_hours(self):
        with self.assertRaisesRegex(planner.PlanError, r"hours_per_day=4"):
            planner.build_plan(planner.validate_units(UNITS), TODAY, TODAY + timedelta(days=8), 2, 3)

    def test_infeasible_and_bad_inputs(self):
        u = planner.validate_units(UNITS)
        cases = [(u, TODAY, TODAY + timedelta(days=1), 16, 0, "Not feasible"),
                 (u, TODAY, TODAY + timedelta(days=3), 2, 3, "revision_days"),
                 (u, TODAY, TODAY, 2, 3, "after the start"),
                 (u, TODAY, TODAY + timedelta(days=30), 0, 3, "hours_per_day"),
                 (u, TODAY, TODAY + timedelta(days=30), 2, 99, "revision_days")]
        for units, s, e, h, r, msg in cases:
            with self.assertRaisesRegex(planner.PlanError, msg):
                planner.build_plan(units, s, e, h, r)

    def test_shares_cap_with_booked_hours(self):
        booked = {TODAY + timedelta(days=i): 1.5 for i in range(30)}
        sessions, _ = planner.build_plan(planner.validate_units([{"topic": "X", "hours": 3}]), TODAY,
                                         TODAY + timedelta(days=10), 2, 1, booked)
        self.assertTrue(all(h <= 0.5 for _, _, h in sessions))


class TestMultiPlan(unittest.TestCase):
    def subjects(self):
        mk = lambda n, exam, level, units: {"name": n, "exam": TODAY + timedelta(days=exam), "level": level,
                                            "units": planner.prepare_units(planner.validate_units(units))}
        return [mk("Late", 30, 1, [{"topic": "L1", "hours": 10}]),
                mk("Soon", 10, 3, [{"topic": "Easy", "hours": 2, "difficulty": "easy"},
                                   {"topic": "Hard", "hours": 5, "difficulty": "hard"}]),
                mk("Mid", 20, 2, [{"topic": "M1", "hours": 8}])]

    def test_meets_every_deadline_under_shared_cap(self):
        sessions, info = planner.build_multi_plan(self.subjects(), TODAY, 3, 2)
        self.assertEqual(info["mode"], "balanced")
        per_day, hours = {}, {}
        for d, name, topic, h in sessions:
            per_day[d] = per_day.get(d, 0) + h
            if topic != planner.REVISION:
                hours[name] = hours.get(name, 0) + h
                exam = {s["name"]: s["exam"] for s in self.subjects()}[name]
                self.assertLess(d, exam - timedelta(days=2), (name, d))  # study ends before its revision days
        self.assertLessEqual(max(per_day.values()), 3)
        self.assertEqual(hours, {"Late": 10, "Soon": 8, "Mid": 8})  # Hard 5h got +20% -> 6h

    def test_hard_topics_first_and_extra_time(self):
        units = planner.prepare_units(planner.validate_units(
            [{"topic": "A", "hours": 2, "difficulty": "easy"}, {"topic": "B", "hours": 5, "difficulty": "hard"}]),
            done_hours={"B": 1})
        self.assertEqual(units[0], {"topic": "B", "hours": 5.0, "difficulty": 3})
        self.assertEqual([u["topic"] for u in planner.prepare_units(planner.validate_units(
            [{"topic": "A", "hours": 2, "difficulty": "easy"}, {"topic": "B", "hours": 5, "difficulty": "hard"}]),
            hard_first=False)], ["A", "B"])

    def test_tight_plan_falls_back_then_errors_with_suggestion(self):
        sessions, info = planner.build_multi_plan(self.subjects(), TODAY, 2, 2)
        self.assertIn(info["mode"], ("balanced", "front-loaded"))
        with self.assertRaisesRegex(planner.PlanError, r"Soon is short by .*hours_per_day="):
            planner.build_multi_plan(self.subjects(), TODAY, 0.5, 2)


class TestTools(Base):
    def plan(self, **kw):
        args = {"subject": "ML", "units": UNITS, "exam_date": d(30), "hours_per_day": 2}
        args.update(kw)
        return tools.run_tool("plan_schedule", args)

    def test_plan_and_one_reminder_per_day(self):
        out = self.plan()
        self.assertTrue(out.startswith("Planned ML"), out)
        rows = tools.get_sessions("ml")  # case-insensitive
        days = {r["study_date"] for r in rows}
        study_rems = [r for r in tools.get_reminders() if r["message"].startswith("Study")]
        self.assertGreaterEqual(len(study_rems), len(days) - 1)  # today's may be skipped if the time has passed
        self.assertEqual(len({r["remind_at"] for r in study_rems}), len(study_rems))
        self.assertTrue(any("EXAM" in r["message"] for r in tools.get_reminders()))

    def test_edge_case_errors_do_not_touch_db(self):
        self.plan()
        before = tools.get_sessions()
        for kw in [{"exam_date": d(-1)}, {"exam_date": d(0)}, {"exam_date": "soon"}, {"hours_per_day": 0},
                   {"hours_per_day": "lots"}, {"revision_days": 2.5}, {"units": []}, {"units": "not json"},
                   {"reminder_time": "6pm"}, {"exam_date": d(5)}]:
            out = self.plan(**kw)
            self.assertTrue(out.startswith("Error:"), (kw, out))
        self.assertEqual(tools.get_sessions(), before)

    def test_past_start_is_moved_to_today(self):
        out = self.plan(start_date=d(-5))
        self.assertIn("starts today", out)
        self.assertEqual(tools.get_sessions()[0]["study_date"], d(0))

    def test_replan_keeps_done_and_other_subjects(self):
        self.plan()
        tools.run_tool("plan_schedule", {"subject": "DBMS", "units": [{"topic": "SQL", "hours": 10}],
                                         "exam_date": d(20), "hours_per_day": 3})
        first = tools.get_sessions("ML")[0]
        tools.mark_session_done(first["id"])
        self.plan(hours_per_day=3)
        ml = tools.get_sessions("ML")
        self.assertIn(first["id"], [r["id"] for r in ml if r["done"]])
        self.assertEqual(sum(r["hours"] for r in ml if r["topic"] != "Revision"), 19)
        self.assertTrue(tools.get_sessions("DBMS"))
        per_day = {}
        for r in tools.get_sessions():
            per_day[r["study_date"]] = per_day.get(r["study_date"], 0) + r["hours"]
        self.assertLessEqual(max(per_day.values()), 3)

    def test_reschedule_missed(self):
        self.assertIn("No saved plan", tools.run_tool("reschedule_missed", {"subject": "ML"}))
        self.plan()
        self.assertIn("on track", tools.run_tool("reschedule_missed", {"subject": "ML"}))
        with tools._db() as c:  # simulate a missed session from the past
            c.execute("UPDATE sessions SET study_date=? WHERE id=(SELECT MIN(id) FROM sessions)", (d(-2),))
        out = tools.run_tool("reschedule_missed", {"subject": "ML"})
        self.assertIn("Moved 1 missed", out)
        self.assertFalse([r for r in tools.get_sessions() if r["study_date"] < d(0)])

    def test_files_are_sandboxed(self):
        tools.write_file("syl.txt", "Subject: X\nExam: 2030-01-01\nUnit 1: A (2 hours)")
        self.assertEqual(json.loads(tools.run_tool("parse_syllabus", {"filename": "data/syl.txt"}))["units"][0]["hours"], 2)
        for name in ["../agent.py", "..\\x.txt", "C:\\Windows\\win.ini", "/etc/passwd.txt", "sub/x.txt", "x.exe", ""]:
            self.assertTrue(tools.run_tool("read_file", {"filename": name}).startswith("Error"), name)
        self.assertIn("not found", tools.run_tool("read_file", {"filename": "missing.txt"}))
        self.assertIn("already exists", tools.run_tool("write_file", {"filename": "syl.txt", "content": "x"}))
        self.assertTrue(tools.run_tool("write_file", {"filename": "a.py", "content": "x"}).startswith("Error"))

    def test_upload_sanitises_name(self):
        self.assertEqual(tools.save_upload("../../evil name?.txt", b"Unit 1: A (1h)"), "evil name_.txt")
        with self.assertRaises(tools.ToolError):
            tools.save_upload("x.txt", b"")

    def test_calculator(self):
        self.assertEqual(tools.calculator("(6+8+5)/12"), "1.583333")
        for bad in ["1/0", "9**999999", "__import__('os')", "", "2 +"]:
            self.assertTrue(tools.run_tool("calculator", {"expression": bad}).startswith("Error"), bad)

    def test_run_tool_robustness(self):
        self.assertIn("unknown tool", tools.run_tool("nope", {}))
        self.assertIn("bad arguments", tools.run_tool("get_today", {"x": 1}))
        self.assertIn("must be a JSON object", tools.run_tool("get_today", []))
        self.assertTrue(tools.run_tool("list_schedule", {"subject": None}).startswith("No study sessions"))
        self.assertIn("No session #999", tools.run_tool("mark_session_done", {"session_id": 999}))
        self.assertIn("No plan found", tools.run_tool("clear_schedule", {"subject": "Ghost"}))
        self.assertIn("in the past", tools.run_tool("add_reminder", {"message": "x", "remind_at": "2000-01-01 10:00"}))
        self.assertIn("Nothing to export", tools.run_tool("export_timetable", {}))

    def test_log_progress(self):
        self.assertIn("No plan found", tools.run_tool("log_progress", {"subject": "ML", "topic": "A", "hours": 1}))
        self.plan()
        self.assertIn("Give the hours", tools.run_tool("log_progress", {"subject": "ML", "topic": "A"}))
        self.assertIn("not in this plan", tools.run_tool("log_progress", {"subject": "ML", "topic": "zzz", "hours": 1}))
        out = tools.run_tool("log_progress", {"subject": "ml", "topic": "a", "hours": 2.6})  # floors to 2.5h
        self.assertIn("Logged 2.5h of 'A'", out)
        self.assertIn("3.5h of this topic left", out)
        _, topics = tools.topic_progress("ML")
        self.assertEqual(topics[0], {"topic": "A", "planned": 6, "done": 2.5})
        self.assertEqual([t["topic"] for t in topics], ["A", "B", "C", "Revision"])
        out = tools.run_tool("log_progress", {"subject": "ML", "topic": "B", "complete": True})
        self.assertIn("Logged 8h", out)
        self.assertIn("already fully completed", tools.run_tool("log_progress", {"subject": "ML", "topic": "B", "hours": 1}))
        self.assertIn("more than planned", tools.run_tool("log_progress", {"subject": "ML", "topic": "C", "hours": 9}))
        self.assertIn("- A: 2.5/6h (42%)", tools.run_tool("get_progress", {"subject": "ML"}))
        self.plan()  # re-planning keeps logged hours and only schedules what is left
        self.assertEqual(sum(r["hours"] for r in tools.get_sessions("ML") if not r["done"] and r["topic"] != "Revision"), 3.5)

    def test_bootstrap_first_run(self):
        results = tools.bootstrap_sample_data(hours_per_day=4)
        self.assertGreaterEqual(len(tools.progress_rows()), 4, results)
        self.assertTrue(results[0].startswith("Planned 4 subjects together"), results)
        self.assertTrue(os.path.exists(os.path.join(TMP, "timetable.md")))
        per_day = {}
        for r in tools.get_sessions():
            per_day[r["study_date"]] = per_day.get(r["study_date"], 0) + r["hours"]
        self.assertLessEqual(max(per_day.values()), 4)
        self.assertEqual(tools.bootstrap_sample_data(), [])  # already has plans -> does nothing

    def test_plan_all_subjects_with_ratings(self):
        tools.write_file("s1.txt", f"Subject: Alpha\nExam: {d(15)}\nLevel: Advanced\nUnit 1: Pointers (4h)\nUnit 2: Arrays (2h)")
        tools.write_file("s2.txt", f"Subject: Beta\nExam: {d(25)}\nUnit 1: Sets (3h)\nUnit 2: Graphs (3h)")
        tools.write_file("s3.txt", "Subject: NoDate\nUnit 1: X (2h)")
        self.assertIn("Unknown difficulty", tools.run_tool("plan_all_subjects", {"difficulty": {"Arrays": "meh"}}))
        out = tools.run_tool("plan_all_subjects", {"hours_per_day": 3, "difficulty": {"Arrays": "easy", "beta::graphs": "hard"}})
        self.assertTrue(out.startswith("Planned 2 subjects together"), out)
        self.assertIn("Skipped s3.txt", out)
        self.assertIn("Priority", out)
        self.assertIn("Next 7 days", out)
        alpha = [r["topic"] for r in tools.get_sessions("Alpha") if r["topic"] != "Revision"]
        self.assertEqual(alpha[0], "Pointers")  # hard first
        self.assertEqual(sum(r["hours"] for r in tools.get_sessions("Beta") if r["topic"] == "Graphs"), 3.75)  # hard +20%
        # ratings are remembered on re-plan, and logged progress is kept
        tools.run_tool("log_progress", {"subject": "Beta", "topic": "Sets", "hours": 1})
        tools.run_tool("plan_all_subjects", {"hours_per_day": 3})
        self.assertEqual(sum(r["hours"] for r in tools.get_sessions("Beta") if r["topic"] == "Graphs"), 3.75)
        self.assertEqual(sum(r["hours"] for r in tools.get_sessions("Beta") if r["topic"] == "Sets" and not r["done"]), 2)
        self.assertEqual([r["subject"] for r in tools.priority_rows()][0], "Alpha")
        self.assertIn("hours_per_day=", tools.run_tool("plan_all_subjects", {"hours_per_day": 0.25}))
        self.assertIn("Alpha", tools.run_tool("list_subjects", {}))

    def test_export_timetable(self):
        self.plan()
        self.assertIn("Saved data/timetable.md", tools.run_tool("export_timetable", {}))
        with open(os.path.join(TMP, "timetable.md"), encoding="utf-8") as f:
            self.assertIn("| Date | Day | Topic | Hours | Status |", f.read())


# ---------- Agent loop with a fake Groq client ----------
def _call(name, args, cid="c1"):
    raw = args if isinstance(args, str) else json.dumps(args)
    return NS(id=cid, function=NS(name=name, arguments=raw))


def _reply(content=None, calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls))])


class FakeClient:
    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        item = self.script.pop(0) if self.script else _reply("final")
        if isinstance(item, Exception):
            raise item
        return item


class TestAgent(Base):
    def test_happy_path_and_history(self):
        client = FakeClient([_reply(calls=[_call("get_today", {})]), _reply("All planned!")])
        steps = []
        text, hist = agent.run_agent("plan", on_step=lambda *a: steps.append(a), client=client)
        self.assertEqual(text, "All planned!")
        self.assertEqual(steps[0][0], "get_today")
        self.assertEqual(hist, [{"role": "user", "content": "plan"}, {"role": "assistant", "content": "All planned!"}])

    def test_bad_json_unknown_tool_and_repeats(self):
        client = FakeClient([_reply(calls=[_call("get_today", "{bad json"), _call("ghost", {}, "c2")])]
                            + [_reply(calls=[_call("list_files", {})])] * 4 + [_reply("ok")])
        outs = []
        agent.run_agent("go", on_step=lambda n, a, o: outs.append(o), client=client)
        self.assertIn("not valid JSON", outs[0])
        self.assertIn("unknown tool", outs[1])
        self.assertIn("already made this exact call", outs[-1])

    def test_step_limit_returns_summary(self):
        client = FakeClient([_reply(calls=[_call("calculator", {"expression": f"{i}+1"})]) for i in range(agent.MAX_STEPS)]
                            + [_reply("Partial summary")])
        text, _ = agent.run_agent("loop", client=client)
        self.assertEqual(text, "Partial summary")
        self.assertNotIn("tools", client.requests[-1])

    def test_empty_input_and_broken_callback(self):
        self.assertIn("Tell me", agent.run_agent("   ", client=FakeClient([]))[0])
        client = FakeClient([_reply(calls=[_call("get_today", {})]), _reply("done")])
        self.assertEqual(agent.run_agent("x", on_step=lambda *a: 1 / 0, client=client)[0], "done")

    def test_history_is_compacted(self):
        old = [{"role": "system", "content": "s"}, {"role": "tool", "content": "t", "tool_call_id": "1"},
               {"role": "assistant", "content": "", "tool_calls": []}] + [{"role": "user", "content": str(i)} for i in range(30)]
        client = FakeClient([_reply("hi")])
        agent.run_agent("next", old, client=client)
        sent = client.requests[0]["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertEqual(sum(m["role"] == "system" for m in sent), 1)
        self.assertFalse([m for m in sent if m["role"] == "tool"])
        self.assertLessEqual(len(sent), 2 + 2 * agent.MAX_HISTORY_TURNS)

    def test_missing_key(self):
        saved = os.environ.pop("GROQ_API_KEY", None)
        try:
            with self.assertRaisesRegex(agent.AgentError, "GROQ_API_KEY"):
                agent.make_client()
        finally:
            if saved:
                os.environ["GROQ_API_KEY"] = saved


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
