"""
Exam Study & Goal Tracker
--------------------------
A single-file Streamlit + SQLite application for tracking exam study goals,
attaching resources (notes/PPTs/PYQs), and running a built-in Pomodoro-style
focus timer that logs study sessions.

Run with:
    pip install streamlit pandas
    streamlit run app.py
"""

import os
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, date, time as dtime, timedelta

import pandas as pd
import streamlit as st

# --------------------------------------------------------------------------
# Configuration & constants
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "study_tracker.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

PRIORITIES = ["High", "Medium", "Low"]
STATUSES = ["Pending", "Completed"]
EXAM_TYPES = ["Mid-term", "End-term"]
RESOURCE_TYPES = ["Notes", "PPT", "PYQ"]

URGENT_WINDOW_HOURS = 48

os.makedirs(UPLOAD_DIR, exist_ok=True)


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------

@contextmanager
def get_conn():
    """Yield a SQLite connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                topic TEXT NOT NULL,
                deadline TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                exam_type TEXT NOT NULL
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_type TEXT NOT NULL,
                FOREIGN KEY (goal_id) REFERENCES goals (id) ON DELETE CASCADE
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS study_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL,
                log_date TEXT NOT NULL
            );
            """
        )


# ---- Goals ----------------------------------------------------------------

def add_goal(subject, topic, deadline_dt, priority, exam_type):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO goals (subject, topic, deadline, priority, status, exam_type)
            VALUES (?, ?, ?, ?, 'Pending', ?)
            """,
            (subject.strip(), topic.strip(), deadline_dt.isoformat(sep=" "), priority, exam_type),
        )


def get_goals(subject=None, exam_type=None, status=None, order_by_deadline=True):
    query = "SELECT * FROM goals WHERE 1=1"
    params = []
    if subject and subject != "All":
        query += " AND subject = ?"
        params.append(subject)
    if exam_type and exam_type != "All":
        query += " AND exam_type = ?"
        params.append(exam_type)
    if status and status != "All":
        query += " AND status = ?"
        params.append(status)
    if order_by_deadline:
        query += " ORDER BY deadline ASC"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_distinct_subjects():
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT subject FROM goals ORDER BY subject ASC").fetchall()
    return [r["subject"] for r in rows]


def update_goal_status(goal_id, status):
    with get_conn() as conn:
        conn.execute("UPDATE goals SET status = ? WHERE id = ?", (status, goal_id))


def delete_goal(goal_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))


def get_goal_by_id(goal_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    return dict(row) if row else None


# ---- Resources --------------------------------------------------------------

def add_resource(goal_id, file_name, file_path, file_type):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO resources (goal_id, file_name, file_path, file_type)
            VALUES (?, ?, ?, ?)
            """,
            (goal_id, file_name, file_path, file_type),
        )


def get_resources(goal_id=None):
    query = """
        SELECT resources.*, goals.subject AS goal_subject, goals.topic AS goal_topic
        FROM resources
        LEFT JOIN goals ON resources.goal_id = goals.id
        WHERE 1=1
    """
    params = []
    if goal_id:
        query += " AND resources.goal_id = ?"
        params.append(goal_id)
    query += " ORDER BY resources.id DESC"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def delete_resource(resource_id):
    with get_conn() as conn:
        row = conn.execute("SELECT file_path FROM resources WHERE id = ?", (resource_id,)).fetchone()
        conn.execute("DELETE FROM resources WHERE id = ?", (resource_id,))
    if row and os.path.exists(row["file_path"]):
        try:
            os.remove(row["file_path"])
        except OSError:
            pass


# ---- Study logs ---------------------------------------------------------------

def add_study_log(subject, duration_minutes, log_dt=None):
    log_dt = log_dt or datetime.now()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO study_logs (subject, duration_minutes, log_date)
            VALUES (?, ?, ?)
            """,
            (subject.strip(), int(duration_minutes), log_dt.isoformat(sep=" ")),
        )


def get_study_logs(limit=50):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM study_logs ORDER BY log_date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_total_minutes_today():
    today_str = date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_minutes), 0) AS total FROM study_logs WHERE log_date LIKE ?",
            (f"{today_str}%",),
        ).fetchone()
    return row["total"]


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def parse_deadline(deadline_str):
    return datetime.fromisoformat(deadline_str)


def urgency_bucket(deadline_dt, now=None):
    now = now or datetime.now()
    delta = deadline_dt - now
    if delta.total_seconds() < 0:
        return "overdue", delta
    elif delta.total_seconds() < URGENT_WINDOW_HOURS * 3600:
        return "urgent", delta
    else:
        return "upcoming", delta


def format_countdown(delta):
    """Format a timedelta (may be negative) into a human string like '2d 3h 15m'."""
    total_seconds = int(delta.total_seconds())
    sign = "-" if total_seconds < 0 else ""
    total_seconds = abs(total_seconds)
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return sign + " ".join(parts)


URGENCY_STYLE = {
    "overdue": {"color": "#ffffff", "bg": "#d9363e", "label": "OVERDUE"},
    "urgent": {"color": "#ffffff", "bg": "#e08a1e", "label": "URGENT"},
    "upcoming": {"color": "#ffffff", "bg": "#2f9e44", "label": "UPCOMING"},
}


def urgency_badge_html(bucket):
    style = URGENCY_STYLE[bucket]
    return (
        f'<span style="background-color:{style["bg"]}; color:{style["color"]}; '
        f'padding:2px 10px; border-radius:12px; font-size:0.75rem; font-weight:600;">'
        f'{style["label"]}</span>'
    )


def priority_badge_html(priority):
    colors = {"High": "#d9363e", "Medium": "#e08a1e", "Low": "#2f6fb0"}
    color = colors.get(priority, "#666666")
    return (
        f'<span style="border:1px solid {color}; color:{color}; '
        f'padding:1px 8px; border-radius:10px; font-size:0.72rem; font-weight:600;">'
        f'{priority}</span>'
    )


def human_file_size(num_bytes):
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def open_file_on_server(path):
    """Attempt to open a file with the host OS's default application.
    Only meaningful when Streamlit is running locally on the user's own machine."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# --------------------------------------------------------------------------
# UI: Dashboard
# --------------------------------------------------------------------------

def render_dashboard():
    st.header("📊 Dashboard")

    subjects = ["All"] + get_distinct_subjects()
    col_f1, col_f2, col_f3 = st.columns([1, 1, 1])
    with col_f1:
        subject_filter = st.selectbox("Filter by Subject", subjects, key="dash_subject")
    with col_f2:
        exam_filter = st.selectbox("Filter by Exam Type", ["All"] + EXAM_TYPES, key="dash_exam")
    with col_f3:
        show_completed = st.checkbox("Show completed goals too", value=False)

    status_filter = "All" if show_completed else "Pending"
    goals = get_goals(subject=subject_filter, exam_type=exam_filter, status=status_filter)

    st.subheader("Goals")
    if not goals:
        st.info("No goals match the current filters. Add one from the 'Add Goal' tab.")
    else:
        now = datetime.now()
        for goal in goals:
            deadline_dt = parse_deadline(goal["deadline"])
            bucket, delta = urgency_bucket(deadline_dt, now)
            is_completed = goal["status"] == "Completed"

            with st.container(border=True):
                top_col1, top_col2 = st.columns([4, 1])
                with top_col1:
                    title_html = f'<span style="font-size:1.05rem; font-weight:700;">{goal["subject"]} — {goal["topic"]}</span>'
                    st.markdown(title_html, unsafe_allow_html=True)
                    badges = ""
                    if not is_completed:
                        badges += urgency_badge_html(bucket) + "&nbsp;&nbsp;"
                    badges += priority_badge_html(goal["priority"]) + "&nbsp;&nbsp;"
                    badges += (
                        f'<span style="background-color:#444; color:#fff; padding:1px 8px; '
                        f'border-radius:10px; font-size:0.72rem;">{goal["exam_type"]}</span>'
                    )
                    if is_completed:
                        badges += (
                            '&nbsp;&nbsp;<span style="background-color:#2f6fb0; color:#fff; '
                            'padding:1px 8px; border-radius:10px; font-size:0.72rem;">COMPLETED</span>'
                        )
                    st.markdown(badges, unsafe_allow_html=True)
                    st.caption(f"Deadline: {deadline_dt.strftime('%a, %d %b %Y  %I:%M %p')}")
                    if not is_completed:
                        countdown_text = format_countdown(delta)
                        if bucket == "overdue":
                            st.markdown(f"**Time overdue by:** `{countdown_text.lstrip('-')}`")
                        else:
                            st.markdown(f"**Time remaining:** `{countdown_text}`")
                with top_col2:
                    if not is_completed:
                        if st.button("✅ Mark Completed", key=f"complete_{goal['id']}"):
                            update_goal_status(goal["id"], "Completed")
                            st.rerun()
                    else:
                        if st.button("↩️ Reopen", key=f"reopen_{goal['id']}"):
                            update_goal_status(goal["id"], "Pending")
                            st.rerun()
                    if st.button("🗑️ Delete", key=f"delete_{goal['id']}"):
                        delete_goal(goal["id"])
                        st.rerun()

    st.divider()
    st.subheader("📈 Syllabus Completion by Subject")
    all_goals = get_goals(order_by_deadline=False)
    if not all_goals:
        st.caption("No goals yet — progress will appear here once you add some.")
    else:
        df = pd.DataFrame(all_goals)
        summary = (
            df.groupby("subject")["status"]
            .apply(lambda s: (s == "Completed").sum() / len(s))
            .reset_index(name="pct")
        )
        for _, row in summary.iterrows():
            pct = row["pct"]
            st.write(f"**{row['subject']}** — {pct * 100:.0f}% complete")
            st.progress(min(max(pct, 0.0), 1.0))


# --------------------------------------------------------------------------
# UI: Add Goal
# --------------------------------------------------------------------------

def render_add_goal():
    st.header("➕ Add a New Goal")
    with st.form("add_goal_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            subject = st.text_input("Subject", placeholder="e.g. Data Structures")
            exam_type = st.selectbox("Exam Type", EXAM_TYPES)
            deadline_date = st.date_input("Deadline Date", value=date.today() + timedelta(days=7))
        with col2:
            topic = st.text_input("Topic", placeholder="e.g. Binary Search Trees")
            priority = st.selectbox("Priority", PRIORITIES)
            deadline_time = st.time_input("Deadline Time", value=dtime(23, 59))

        submitted = st.form_submit_button("Add Goal", use_container_width=True)
        if submitted:
            if not subject.strip() or not topic.strip():
                st.error("Subject and Topic are required.")
            else:
                deadline_dt = datetime.combine(deadline_date, deadline_time)
                add_goal(subject, topic, deadline_dt, priority, exam_type)
                st.success(f"Goal '{topic}' added for {subject}.")


# --------------------------------------------------------------------------
# UI: Resources
# --------------------------------------------------------------------------

def render_resources():
    st.header("📎 Resource Attachments")

    goals = get_goals(order_by_deadline=False)
    if not goals:
        st.warning("Add at least one goal first, then attach resources to it here.")
        return

    goal_labels = {g["id"]: f'#{g["id"]} — {g["subject"]} / {g["topic"]}' for g in goals}

    st.subheader("Upload a Resource")
    with st.form("upload_resource_form", clear_on_submit=True):
        goal_id = st.selectbox(
            "Link to Goal", options=list(goal_labels.keys()), format_func=lambda gid: goal_labels[gid]
        )
        file_type = st.selectbox("Resource Type", RESOURCE_TYPES)
        uploaded_file = st.file_uploader(
            "Choose a file (PDF, PPT/PPTX, or image)",
            type=["pdf", "ppt", "pptx", "png", "jpg", "jpeg"],
        )
        upload_submitted = st.form_submit_button("Upload", use_container_width=True)
        if upload_submitted:
            if uploaded_file is None:
                st.error("Please choose a file to upload.")
            else:
                unique_prefix = uuid.uuid4().hex[:8]
                safe_name = f"{goal_id}_{unique_prefix}_{uploaded_file.name}"
                save_path = os.path.join(UPLOAD_DIR, safe_name)
                with open(save_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())
                add_resource(goal_id, uploaded_file.name, save_path, file_type)
                st.success(f"Uploaded '{uploaded_file.name}' and linked it to goal #{goal_id}.")

    st.divider()
    st.subheader("Attached Files")

    filter_goal = st.selectbox(
        "Filter by Goal",
        options=["All"] + list(goal_labels.keys()),
        format_func=lambda gid: "All Goals" if gid == "All" else goal_labels[gid],
    )
    resources = get_resources(goal_id=None if filter_goal == "All" else filter_goal)

    if not resources:
        st.info("No resources uploaded yet.")
        return

    for res in resources:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
            with c1:
                subj_label = (
                    f'{res["goal_subject"]} / {res["goal_topic"]}' if res["goal_subject"] else "Unlinked"
                )
                st.markdown(f"**{res['file_name']}**  \n`{res['file_type']}` — {subj_label}")
            with c2:
                if os.path.exists(res["file_path"]):
                    size = human_file_size(os.path.getsize(res["file_path"]))
                    st.caption(size)
                    with open(res["file_path"], "rb") as f:
                        st.download_button(
                            "⬇️ Download",
                            data=f.read(),
                            file_name=res["file_name"],
                            key=f"dl_{res['id']}",
                        )
                else:
                    st.caption("File missing on disk")
            with c3:
                if st.button("🖥️ Open on server", key=f"open_{res['id']}"):
                    ok, err = open_file_on_server(res["file_path"])
                    if ok:
                        st.success("Opened on the server's default application.")
                    else:
                        st.error(f"Could not open file: {err}")
            with c4:
                if st.button("🗑️ Remove", key=f"del_res_{res['id']}"):
                    delete_resource(res["id"])
                    st.rerun()


# --------------------------------------------------------------------------
# UI: Focus Timer
# --------------------------------------------------------------------------

def init_timer_state():
    defaults = {
        "timer_running": False,
        "timer_end": None,
        "timer_subject": "",
        "timer_duration_min": 25,
        "timer_start": None,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def render_focus_timer():
    st.header("⏱️ Focus Timer (Pomodoro)")
    init_timer_state()

    today_minutes = get_total_minutes_today()
    st.metric("Focused minutes today", today_minutes)

    subjects = get_distinct_subjects()

    if not st.session_state.timer_running:
        col1, col2 = st.columns(2)
        with col1:
            if subjects:
                subject_choice = st.selectbox("Subject", subjects + ["Other (type below)"])
                if subject_choice == "Other (type below)":
                    subject_input = st.text_input("Enter subject name", key="timer_subject_manual")
                else:
                    subject_input = subject_choice
            else:
                subject_input = st.text_input("Subject", key="timer_subject_manual")
        with col2:
            duration_choice = st.selectbox("Session length (minutes)", [15, 25, 45, 60, "Custom"])
            if duration_choice == "Custom":
                duration_min = st.number_input("Custom minutes", min_value=1, max_value=240, value=25)
            else:
                duration_min = duration_choice

        if st.button("▶️ Start Focus Session", use_container_width=True, type="primary"):
            if not subject_input or not subject_input.strip():
                st.error("Please enter a subject before starting.")
            else:
                st.session_state.timer_subject = subject_input.strip()
                st.session_state.timer_duration_min = int(duration_min)
                st.session_state.timer_start = datetime.now()
                st.session_state.timer_end = datetime.now() + timedelta(minutes=int(duration_min))
                st.session_state.timer_running = True
                st.rerun()
    else:
        remaining = st.session_state.timer_end - datetime.now()
        remaining_seconds = int(remaining.total_seconds())

        st.markdown(f"**Studying:** {st.session_state.timer_subject}")

        if remaining_seconds <= 0:
            add_study_log(st.session_state.timer_subject, st.session_state.timer_duration_min)
            st.success(
                f"Session complete! Logged {st.session_state.timer_duration_min} minutes for "
                f"{st.session_state.timer_subject}."
            )
            st.session_state.timer_running = False
            st.session_state.timer_end = None
            st.session_state.timer_start = None
            st.balloons()
        else:
            mins, secs = divmod(remaining_seconds, 60)
            st.markdown(
                f'<div style="text-align:center; font-size:3.5rem; font-weight:700;">'
                f"{mins:02d}:{secs:02d}</div>",
                unsafe_allow_html=True,
            )
            total_seconds = st.session_state.timer_duration_min * 60
            elapsed_fraction = 1 - (remaining_seconds / total_seconds)
            st.progress(min(max(elapsed_fraction, 0.0), 1.0))

            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("⏹️ Stop & Log Elapsed Time", use_container_width=True):
                    elapsed_min = max(
                        1,
                        int((datetime.now() - st.session_state.timer_start).total_seconds() // 60),
                    )
                    add_study_log(st.session_state.timer_subject, elapsed_min)
                    st.success(f"Logged {elapsed_min} minutes for {st.session_state.timer_subject}.")
                    st.session_state.timer_running = False
                    st.session_state.timer_end = None
                    st.session_state.timer_start = None
                    st.rerun()
            with col_b:
                if st.button("❌ Cancel (no log)", use_container_width=True):
                    st.session_state.timer_running = False
                    st.session_state.timer_end = None
                    st.session_state.timer_start = None
                    st.rerun()

            time.sleep(1)
            st.rerun()

    st.divider()
    st.subheader("Recent Sessions")
    logs = get_study_logs(limit=15)
    if logs:
        df = pd.DataFrame(logs)[["subject", "duration_minutes", "log_date"]]
        df.columns = ["Subject", "Minutes", "Logged At"]
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.caption("No focus sessions logged yet. Start one above!")


# --------------------------------------------------------------------------
# Main app
# --------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Exam Study & Goal Tracker", page_icon="🎯", layout="wide")
    init_db()

    st.sidebar.title("🎯 Study Tracker")
    page = st.sidebar.radio(
        "Navigate",
        ["Dashboard", "Add Goal", "Resources", "Focus Timer"],
        label_visibility="collapsed",
    )

    st.sidebar.divider()
    total_goals = len(get_goals(order_by_deadline=False))
    pending_goals = len(get_goals(status="Pending", order_by_deadline=False))
    st.sidebar.caption(f"Total goals: {total_goals}")
    st.sidebar.caption(f"Pending: {pending_goals}")
    st.sidebar.caption(f"Focused today: {get_total_minutes_today()} min")

    if page == "Dashboard":
        render_dashboard()
    elif page == "Add Goal":
        render_add_goal()
    elif page == "Resources":
        render_resources()
    elif page == "Focus Timer":
        render_focus_timer()


if __name__ == "__main__":
    main()
