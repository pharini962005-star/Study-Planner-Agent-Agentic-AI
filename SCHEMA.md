# Schema: Study Planner Agent (Agentic AI)

**Author:** Your Name – Reg. No. XXXXX

## 1. Idea

An AI agent that reads the student's syllabi for several subjects, builds one balanced study timetable across all of
them, sets reminders and tracks progress.

**Design principle:** the LLM decides *what* to do; ordinary code does the exact work (dates, hours, scheduling).
This way the plan is always valid, and the LLM cannot invent sessions.

## 2. Components

| Component | Role |
|---|---|
| **Input** | Syllabus files (subject, exam date, units with hours), from a Hugging Face course dataset or uploaded by the student |
| **Interface** | Streamlit web app (chat, split-up, schedule, progress, reminders) and a command-line chat |
| **Agent core** | Groq LLM (Llama 3.3 70B) with a system prompt that holds the goal, workflows and rules |
| **Tools** | 21 functions the agent can call to observe and act |
| **Planning engine** | Deterministic code that parses syllabi and builds the schedule |
| **Memory** | Short-term: recent chat turns. Long-term: SQLite database |

## 3. Agent Schema (PEAS)

| | |
|---|---|
| **Performance** | All subjects covered before their exams, daily hour limit respected, revision days included, hard topics given more time |
| **Environment** | Syllabus files, the study database, today's date, the student |
| **Actuators** | Planning, rescheduling, logging progress, setting reminders, exporting the timetable, replying to the student |
| **Sensors** | Reading syllabi, reading the schedule and progress, the student's messages |

**Agent type:** goal-based, tool-using agent with a utility-based priority ranking.

## 4. Working Loop

```
Student request
   → PLAN    : LLM decides which tool to use
   → ACT     : tool runs (validated by code)
   → OBSERVE : result goes back to the LLM
   → repeat until the goal is met (max 25 steps)
   → final answer to the student
```

Guardrails: retries when the API is busy, recovery from bad tool calls, a limit on repeated calls, and asking the
student when information is missing.

## 5. Tools (grouped)

| Group | Purpose | Examples |
|---|---|---|
| Perceive | Read syllabi and the current state | `list_subjects`, `parse_syllabus`, `get_today` |
| Plan | Build or repair the timetable | `plan_all_subjects`, `reschedule_missed` |
| Track | Progress and overview | `log_progress`, `get_progress`, `get_overview` |
| Act | Produce outputs | `add_reminder`, `export_timetable` |
| Utility | Safe maths and dates | `calculator`, `days_between` |

## 6. Memory (Database)

| Table | Stores |
|---|---|
| `subjects` | One row per subject: exam date, units, difficulty |
| `sessions` | The timetable: date, subject, topic, hours, done / pending |
| `reminders` | Reminder message and time |

One subject has many sessions and many reminders.

## 7. Planning Logic (summary)

- Subjects with **earlier exams** are scheduled first.
- **Hard topics** get 20 % extra time and are studied first.
- Work is spread in **1–2 hour blocks** within the student's daily hour limit.
- The last days before each exam are kept for **revision**.
- If the plan cannot fit, the agent reports the hours per day needed.
- Completed work is kept when re-planning; missed sessions can be rescheduled.

## 8. Example

**Student:** "Give me the split-up for all my subjects, 4 hours/day."
**Agent:** reads all syllabi → rates topic difficulty → plans all subjects in one call → replies with the priority
order and a day-by-day table (e.g. *Monday: Data Structures 2 h + Statistics 2 h*).
