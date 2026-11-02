"""Streamlit UI:  streamlit run app.py"""
from datetime import date

import pandas as pd
import streamlit as st

import tools
from agent import MODEL, AgentError, run_agent

st.set_page_config(page_title="Study Planner Agent", page_icon="📚", layout="wide")
ss = st.session_state
ss.setdefault("history", [])  # compact turns sent back to the agent
ss.setdefault("chat", [])     # what is shown: {"role", "content", "steps"}

EXAMPLES = ["Give me the split-up for all my subjects, 4 hours/day",
            "Update my progress",
            "I missed some sessions - reschedule them"]

@st.cache_resource(show_spinner="Setting up your subjects...")
def bootstrap():
    """Runs once per server start: on an empty database, create and plan the sample subjects."""
    return tools.bootstrap_sample_data(hours_per_day=4)


setup_log = bootstrap()

# ---------- Sidebar: preloaded subjects + optional upload ----------
with st.sidebar:
    st.header("📚 Your subjects")
    planned = {r["subject"].lower() for r in tools.progress_rows()}
    for s in sorted(tools.syllabus_overview(), key=lambda s: s["exam_date"] or "9999"):
        icon = "✅" if (s["subject"] or "").lower() in planned else "🆕"
        exam = s["exam_date"] or "no exam date"
        st.markdown(f"{icon} **{s['subject'] or s['file']}**  \n<small>Exam {exam} · {s['total_hours']:g} h · "
                    f"{len(s['units'])} units</small>", unsafe_allow_html=True)
    st.caption("✅ planned · 🆕 not planned yet (ask the agent to plan it)")
    with st.expander("➕ Add another syllabus"):
        up = st.file_uploader("Upload .txt, .md, .csv or .pdf", type=["txt", "md", "csv", "pdf"])
        if up is not None and ss.get("uploaded") != (up.name, up.size):
            try:
                name = tools.save_upload(up.name, up.getvalue())
                ss.uploaded = (up.name, up.size)
                st.success(f"Saved as data/{name}. Ask: *Plan my study from {name}*")
            except tools.ToolError as e:
                st.error(str(e))
    st.divider()
    st.subheader("🔄 Re-plan all subjects")
    hpd = st.number_input("Max study hours per day", min_value=0.5, max_value=16.0, value=4.0, step=0.5)
    hard_first = st.toggle("Hardest topics first", value=True)
    if st.button("Re-plan now", width="stretch", type="primary"):
        out = tools.run_tool("plan_all_subjects", {"hours_per_day": hpd, "hard_first": hard_first})
        (st.error if out.startswith("Error") else st.success)(out.split("\n\n")[0])
    st.divider()
    if st.button("🧹 Clear chat", width="stretch"):
        ss.history, ss.chat = [], []
        st.rerun()
    st.caption(f"Model: `{MODEL}`")

st.title("📚 Study Planner Agent")
st.caption("Reads your syllabus, builds a balanced schedule, saves it and sets reminders.")
chat_tab, split_tab, sched_tab, prog_tab, rem_tab = st.tabs(
    ["💬 Agent", "🗓️ Split-up", "📅 Schedule", "📈 Progress", "⏰ Reminders"])
prompt = st.chat_input("e.g. Give me the split-up for all my subjects, 4 hours/day") or ss.pop("pending", None)

# ---------- Chat ----------
with chat_tab:
    if not ss.chat and not prompt:
        n = len(tools.progress_rows())
        st.info(f"Your {n} subjects are already loaded and planned (see the 📅 Schedule and 📈 Progress tabs). "
                "Ask me anything, or try one of these:" if n else "Ask me to plan your subjects, or try one of these:")
        if setup_log:
            with st.expander("What was set up automatically"):
                st.markdown("\n".join(f"- {line}" for line in setup_log))
        cols = st.columns(len(EXAMPLES))
        for col, ex in zip(cols, EXAMPLES):
            if col.button(ex, width="stretch"):
                ss.pending = ex
                st.rerun()
    for m in ss.chat:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m.get("steps"):
                with st.expander(f"🔧 {len(m['steps'])} tool call(s)"):
                    st.markdown("\n".join(m["steps"]))
    if prompt:
        ss.chat.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            steps = []
            status = st.status("Agent working...", expanded=True)

            def log(name, args, out):
                first = str(out).splitlines()[0][:150] if out else ""
                icon = "❌" if str(out).startswith("Error") else "✅"
                steps.append(f"{icon} **{name}** → {first}")
                status.write(steps[-1])

            try:
                text, ss.history = run_agent(prompt, ss.history, on_step=log)
                status.update(label=f"Done - {len(steps)} tool call(s)", state="complete", expanded=False)
            except AgentError as e:
                text = f"⚠️ {e}"
                status.update(label="Failed", state="error")
            except Exception as e:  # keep the UI alive whatever happens
                text = f"⚠️ Unexpected error: {type(e).__name__}: {e}"
                status.update(label="Failed", state="error")
            st.markdown(text)
        ss.chat.append({"role": "assistant", "content": text, "steps": steps})

# ---------- Split-up across subjects ----------
with split_tab:
    prio = tools.priority_rows()
    if not prio:
        st.info("No plans yet - ask the agent to plan your subjects, or use 'Re-plan now' in the sidebar.")
    else:
        st.subheader("🎯 Priority")
        st.caption("Ranked by the daily pace each subject still needs (hours left ÷ days before revision), "
                   "weighted by difficulty. Earlier exams are scheduled first.")
        st.dataframe(pd.DataFrame([{"#": i, "Subject": r["subject"], "Difficulty": r["level"], "Exam": r["exam_date"],
                                    "Days left": r["days_left"], "Hours left": r["hours_left"],
                                    "Needs h/day": r["hours_per_day"]} for i, r in enumerate(prio, 1)]),
                     hide_index=True, width="stretch")
        st.subheader("🗓️ Day-by-day split")
        days = st.slider("Days to show", 3, 14, 7)
        split = tools.daily_split(days)
        if split:
            df = pd.DataFrame(split)
            st.bar_chart(df.pivot_table(index="study_date", columns="subject", values="hours", aggfunc="sum", fill_value=0),
                         x_label="Date", y_label="Hours")
            st.dataframe(df.rename(columns={"study_date": "Date", "subject": "Subject", "topic": "Topic", "hours": "Hours"})
                         [["Date", "Subject", "Topic", "Hours"]], hide_index=True, width="stretch")
        else:
            st.info("Nothing scheduled in this period.")

# ---------- Schedule (tick sessions as done) ----------
with sched_tab:
    subjects = [r["subject"] for r in tools.progress_rows()]
    if not subjects:
        st.info("No schedule yet - ask the agent to plan your study.")
    else:
        c1, c2 = st.columns([2, 1])
        subject = c1.selectbox("Subject", ["All"] + subjects)
        hide_done = c2.toggle("Hide completed", value=False)
        chosen = None if subject == "All" else subject
        rows = tools.get_sessions(chosen)
        today = date.today().isoformat()
        df = pd.DataFrame([{"id": r["id"], "Date": r["study_date"], "Subject": r["subject"], "Topic": r["topic"],
                            "Hours": r["hours"], "Status": tools._status(r, today), "Done": bool(r["done"])} for r in rows])
        if hide_done:
            df = df[~df["Done"]]
        edited = st.data_editor(df, key=f"editor_{subject}_{hide_done}", hide_index=True, width="stretch",
                                disabled=["id", "Date", "Subject", "Topic", "Hours", "Status"],
                                column_config={"id": None, "Done": st.column_config.CheckboxColumn("Done ✔")})
        changed = edited[edited["Done"] != df["Done"]]
        for _, r in changed.iterrows():
            tools.mark_session_done(int(r["id"]), bool(r["Done"]))
        if len(changed):
            st.rerun()
        b1, b2 = st.columns(2)
        md = tools.timetable_markdown(chosen)
        if md:
            b1.download_button("⬇️ Download timetable.md", md, file_name="timetable.md", width="stretch")
        if chosen and b2.button("🔁 Reschedule missed sessions", width="stretch"):
            try:
                st.success(tools.reschedule_missed(chosen))
            except (tools.ToolError, tools.PlanError) as e:
                st.error(str(e))

# ---------- Progress ----------
with prog_tab:
    rows = tools.progress_rows()
    if not rows:
        st.info("No plans yet - ask the agent to plan your subjects.")
    else:
        for r in rows:  # overview of every subject
            pct = r["done"] / r["total"] if r["total"] else 0
            exam = f" · exam in {(date.fromisoformat(r['exam_date']) - date.today()).days} days" if r["exam_date"] else ""
            missed = f" · ⚠️ {r['missed']} missed" if r["missed"] else ""
            st.progress(min(pct, 1.0), text=f"**{r['subject']}** - {r['done']:g}/{r['total']:g} h ({pct:.0%}){exam}{missed}")

        st.divider()
        st.subheader("✍️ Update progress for a subject")
        subject = st.selectbox("Which subject did you study?", [r["subject"] for r in rows], key="prog_subject")
        name, topics = tools.topic_progress(subject)
        st.dataframe(pd.DataFrame([{"Topic": t["topic"], "Done (h)": t["done"], "Planned (h)": t["planned"],
                                    "Progress": t["done"] / t["planned"] if t["planned"] else 0} for t in topics]),
                     hide_index=True, width="stretch",
                     column_config={"Progress": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1)})
        open_topics = [t["topic"] for t in topics if t["done"] < t["planned"]]
        if not open_topics:
            st.success(f"🎉 Every topic in {name} is complete!")
        else:
            with st.form("log_progress", clear_on_submit=True):
                topic = st.selectbox("Which topic?", open_topics)
                c1, c2 = st.columns(2)
                hours = c1.number_input("Hours studied", min_value=0.0, max_value=24.0, value=1.0, step=0.25)
                complete = c2.checkbox("I finished this whole topic")
                if st.form_submit_button("Save progress", width="stretch"):
                    if not complete and hours <= 0:
                        st.warning("Enter the hours you studied, or tick 'finished'.")
                    else:
                        ss.progress_msg = tools.run_tool("log_progress", {"subject": name, "topic": topic,
                                                                          "hours": hours, "complete": complete})
                        st.rerun()
        if ss.get("progress_msg"):
            msg = ss.pop("progress_msg")
            (st.error if msg.startswith("Error") else st.success)(msg)

# ---------- Reminders ----------
with rem_tab:
    upcoming = st.toggle("Upcoming only", value=True)
    rems = tools.get_reminders(upcoming)
    if rems:
        st.dataframe(pd.DataFrame(rems)[["remind_at", "message"]].rename(columns={"remind_at": "When", "message": "Reminder"}),
                     hide_index=True, width="stretch")
    else:
        st.info("No reminders.")
