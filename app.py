"""Exam Study & Goal Tracker

Single-file Streamlit + Supabase application with:
- Full goal CRUD, including editing and deletion
- Markdown notes per goal
- Spaced repetition (+1, +3, +7 days)
- Topic-level focus timer and study logging
- Study analytics, heatmap and streaks
- .ics calendar export for exam/revision milestones

Install:
    pip install streamlit pandas plotly icalendar supabase

Run:
    streamlit run exam_tracker_updated.py
"""

import mimetypes
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from icalendar import Calendar, Event
from supabase import create_client, Client


# =============================================================================
# Configuration
# =============================================================================

PRIORITIES = ["High", "Medium", "Low"]
STATUSES = ["Pending", "Completed"]
EXAM_TYPES = ["Mid-term", "End-term"]
RESOURCE_TYPES = ["Notes", "PPT", "PYQ"]
SRS_INTERVALS = {1: 1, 2: 3, 3: 7}
SRS_STAGE_LABELS = {1: "+1 Day Review", 2: "+3 Day Review", 3: "+7 Day Review"}
URGENT_WINDOW_HOURS = 48
STORAGE_BUCKET = "study-resources"


@st.cache_resource
def get_supabase() -> Client:
    """Create one server-side Supabase client from Streamlit secrets."""
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_SECRET_KEY"]
    except KeyError as exc:
        st.error(
            "Supabase is not configured yet. Add SUPABASE_URL and "
            "SUPABASE_SECRET_KEY to Streamlit Cloud → App settings → Secrets."
        )
        st.stop()
        raise exc
    return create_client(url, key)


def init_db():
    """Validate that the Supabase schema exists.

    The tables are created once with supabase_setup.sql. Unlike SQLite, the
    database lives independently of Streamlit, so redeploys/restarts do not
    erase study data.
    """
    sb = get_supabase()
    try:
        sb.table("goals").select("id").limit(1).execute()
        sb.table("study_logs").select("id").limit(1).execute()
        sb.table("resources").select("id").limit(1).execute()
    except Exception as exc:
        st.error(
            "Supabase is connected, but the required tables are missing. "
            "Run the supplied supabase_setup.sql once in Supabase → SQL Editor."
        )
        st.exception(exc)
        st.stop()


def run_migrations():
    """Schema migrations are handled by the idempotent supabase_setup.sql."""
    return


def _rows(response):
    return response.data or []


# =============================================================================
# Goal CRUD
# =============================================================================

def add_goal(subject, topic, deadline_dt, priority, exam_type, notes=""):
    payload = {
        "subject": subject.strip(),
        "topic": topic.strip(),
        "deadline": deadline_dt.isoformat(sep=" "),
        "priority": priority,
        "status": "Pending",
        "exam_type": exam_type,
        "notes": notes,
    }
    response = get_supabase().table("goals").insert(payload).execute()
    rows = _rows(response)
    return rows[0]["id"] if rows else None


def get_goals(subject=None, exam_type=None, status=None, order_by_deadline=True):
    query = get_supabase().table("goals").select("*")
    if subject and subject != "All":
        query = query.eq("subject", subject)
    if exam_type and exam_type != "All":
        query = query.eq("exam_type", exam_type)
    if status and status != "All":
        query = query.eq("status", status)
    query = query.order("deadline" if order_by_deadline else "id", desc=not order_by_deadline)
    return _rows(query.execute())


def get_goal_by_id(goal_id):
    rows = _rows(get_supabase().table("goals").select("*").eq("id", goal_id).limit(1).execute())
    return rows[0] if rows else None


def get_distinct_subjects():
    rows = _rows(get_supabase().table("goals").select("subject").execute())
    return sorted({row["subject"] for row in rows if row.get("subject")})


def update_goal(goal_id, subject, topic, deadline_dt, priority, status, exam_type, notes):
    old = get_goal_by_id(goal_id)
    if not old:
        return False

    now = datetime.now()
    completed_at = old.get("completed_at")
    review_stage = old.get("review_stage") or 0
    next_review_date = old.get("next_review_date")

    if status == "Pending":
        completed_at = None
        review_stage = 0
        next_review_date = None
    elif old["status"] != "Completed" and status == "Completed":
        completed_at = now.isoformat(sep=" ")
        review_stage = 1
        next_review_date = (now.date() + timedelta(days=1)).isoformat()
    elif status == "Completed" and not completed_at:
        completed_at = now.isoformat(sep=" ")
        review_stage = 1
        next_review_date = (now.date() + timedelta(days=1)).isoformat()

    payload = {
        "subject": subject.strip(),
        "topic": topic.strip(),
        "deadline": deadline_dt.isoformat(sep=" "),
        "priority": priority,
        "status": status,
        "exam_type": exam_type,
        "notes": notes,
        "completed_at": completed_at,
        "next_review_date": next_review_date,
        "review_stage": review_stage,
    }
    get_supabase().table("goals").update(payload).eq("id", goal_id).execute()
    return True


def delete_goal(goal_id):
    # Historical study logs are retained; their FK uses ON DELETE SET NULL.
    get_supabase().table("goals").delete().eq("id", goal_id).execute()


def save_goal_notes(goal_id, notes):
    get_supabase().table("goals").update({"notes": notes}).eq("id", goal_id).execute()


# =============================================================================
# SRS
# =============================================================================

def mark_goal_completed(goal_id):
    now = datetime.now()
    get_supabase().table("goals").update({
        "status": "Completed",
        "completed_at": now.isoformat(sep=" "),
        "review_stage": 1,
        "next_review_date": (now.date() + timedelta(days=1)).isoformat(),
    }).eq("id", goal_id).execute()


def reopen_goal(goal_id):
    get_supabase().table("goals").update({
        "status": "Pending",
        "completed_at": None,
        "review_stage": 0,
        "next_review_date": None,
    }).eq("id", goal_id).execute()


def advance_review(goal_id):
    goal = get_goal_by_id(goal_id)
    if not goal or not goal.get("completed_at"):
        return

    next_stage = (goal.get("review_stage") or 0) + 1
    completed_date = datetime.fromisoformat(goal["completed_at"]).date()

    if next_stage in SRS_INTERVALS:
        payload = {
            "review_stage": next_stage,
            "next_review_date": (completed_date + timedelta(days=SRS_INTERVALS[next_stage])).isoformat(),
        }
    else:
        payload = {"review_stage": 4, "next_review_date": None}

    get_supabase().table("goals").update(payload).eq("id", goal_id).execute()


def get_revision_queue():
    today = date.today().isoformat()
    response = (
        get_supabase().table("goals").select("*")
        .eq("status", "Completed")
        .gte("review_stage", 1).lte("review_stage", 3)
        .not_.is_("next_review_date", "null")
        .lte("next_review_date", today)
        .order("next_review_date")
        .execute()
    )
    return _rows(response)


def get_upcoming_reviews():
    today = date.today().isoformat()
    response = (
        get_supabase().table("goals").select("*")
        .eq("status", "Completed")
        .gte("review_stage", 1).lte("review_stage", 3)
        .not_.is_("next_review_date", "null")
        .gt("next_review_date", today)
        .order("next_review_date")
        .execute()
    )
    return _rows(response)


# =============================================================================
# Resources (Supabase Storage)
# =============================================================================

def add_resource(goal_id, file_name, storage_path, file_type):
    get_supabase().table("resources").insert({
        "goal_id": goal_id,
        "file_name": file_name,
        "storage_path": storage_path,
        "file_type": file_type,
    }).execute()


def get_resources(goal_id=None):
    query = get_supabase().table("resources").select("*, goals(subject, topic)")
    if goal_id:
        query = query.eq("goal_id", goal_id)
    rows = _rows(query.order("id", desc=True).execute())
    for row in rows:
        goal = row.get("goals") or {}
        row["goal_subject"] = goal.get("subject")
        row["goal_topic"] = goal.get("topic")
    return rows


def delete_resource(resource_id):
    sb = get_supabase()
    rows = _rows(sb.table("resources").select("storage_path").eq("id", resource_id).limit(1).execute())
    if rows and rows[0].get("storage_path"):
        try:
            sb.storage.from_(STORAGE_BUCKET).remove([rows[0]["storage_path"]])
        except Exception:
            pass
    sb.table("resources").delete().eq("id", resource_id).execute()


def upload_resource_to_storage(goal_id, uploaded):
    safe_name = f"{goal_id}/{uuid.uuid4().hex}_{uploaded.name}"
    content_type = uploaded.type or mimetypes.guess_type(uploaded.name)[0] or "application/octet-stream"
    get_supabase().storage.from_(STORAGE_BUCKET).upload(
        path=safe_name,
        file=uploaded.getvalue(),
        file_options={"content-type": content_type, "upsert": "false"},
    )
    return safe_name


def download_resource_bytes(storage_path):
    return get_supabase().storage.from_(STORAGE_BUCKET).download(storage_path)


# =============================================================================
# Study logs / statistics
# =============================================================================

def add_study_log(subject, duration_minutes, topic=None, goal_id=None, log_dt=None):
    log_dt = log_dt or datetime.now()
    get_supabase().table("study_logs").insert({
        "subject": subject.strip(),
        "duration_minutes": int(duration_minutes),
        "log_date": log_dt.isoformat(sep=" "),
        "topic": topic.strip() if topic else None,
        "goal_id": goal_id,
    }).execute()


def get_study_logs(limit=50):
    return _rows(
        get_supabase().table("study_logs").select("*")
        .order("log_date", desc=True).limit(limit).execute()
    )


def _all_logs():
    return _rows(get_supabase().table("study_logs").select("*").execute())


def get_total_minutes_today():
    today = date.today()
    total = 0
    for row in _all_logs():
        try:
            if datetime.fromisoformat(row["log_date"]).date() == today:
                total += int(row["duration_minutes"] or 0)
        except (TypeError, ValueError):
            pass
    return total


def get_topic_total_minutes(subject, topic, goal_id=None):
    query = get_supabase().table("study_logs").select("duration_minutes")
    if goal_id:
        query = query.eq("goal_id", goal_id)
    else:
        query = query.eq("subject", subject).eq("topic", topic)
    return sum(int(r["duration_minutes"] or 0) for r in _rows(query.execute()))


def get_subject_total_minutes(subject):
    rows = _rows(
        get_supabase().table("study_logs").select("duration_minutes")
        .eq("subject", subject).execute()
    )
    return sum(int(r["duration_minutes"] or 0) for r in rows)


def get_overall_total_minutes():
    return sum(int(r["duration_minutes"] or 0) for r in _all_logs())


def get_daily_totals(days_back=365):
    start = date.today() - timedelta(days=days_back)
    totals = {}
    for row in _all_logs():
        try:
            day = datetime.fromisoformat(row["log_date"]).date()
        except (TypeError, ValueError):
            continue
        if day >= start:
            key = day.isoformat()
            totals[key] = totals.get(key, 0) + int(row["duration_minutes"] or 0)
    return dict(sorted(totals.items()))


def get_subject_totals():
    totals = {}
    for row in _all_logs():
        subject = row.get("subject") or "Unknown"
        totals[subject] = totals.get(subject, 0) + int(row["duration_minutes"] or 0)
    return [
        {"subject": subject, "total": total}
        for subject, total in sorted(totals.items(), key=lambda x: x[1], reverse=True)
    ]


def format_minutes(minutes):
    minutes = int(minutes or 0)
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def compute_streaks(daily_totals):
    active = {datetime.fromisoformat(day).date() for day, mins in daily_totals.items() if mins and mins > 0}
    if not active:
        return 0, 0

    today = date.today()
    cursor = today if today in active else today - timedelta(days=1)
    current = 0
    while cursor in active:
        current += 1
        cursor -= timedelta(days=1)

    longest = run = 0
    cursor = min(active)
    end = max(active)
    while cursor <= end:
        if cursor in active:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
        cursor += timedelta(days=1)
    return current, longest


# =============================================================================
# Formatting / calendar export
# =============================================================================

def parse_deadline(value):
    return datetime.fromisoformat(value)


def urgency_bucket(deadline_dt, now=None):
    now = now or datetime.now()
    delta = deadline_dt - now
    if delta.total_seconds() < 0:
        return "overdue", delta
    if delta.total_seconds() < 48 * 3600:
        return "urgent", delta
    return "upcoming", delta


def format_countdown(delta):
    seconds = int(delta.total_seconds())
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    days, rem = divmod(seconds, 86400)
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
    "overdue": ("#d9363e", "OVERDUE"),
    "urgent": ("#e08a1e", "URGENT"),
    "upcoming": ("#2f9e44", "UPCOMING"),
}


def urgency_badge_html(bucket):
    bg, label = URGENCY_STYLE[bucket]
    return f'<span style="background:{bg};color:#fff;padding:2px 10px;border-radius:12px;font-size:.75rem;font-weight:600">{label}</span>'


def priority_badge_html(priority):
    color = {"High": "#d9363e", "Medium": "#e08a1e", "Low": "#2f6fb0"}.get(priority, "#666")
    return f'<span style="border:1px solid {color};color:{color};padding:1px 8px;border-radius:10px;font-size:.72rem;font-weight:600">{priority}</span>'


def review_badge_html(stage_label, overdue=False):
    bg = "#d9363e" if overdue else "#7048e8"
    return f'<span style="background:{bg};color:#fff;padding:1px 8px;border-radius:10px;font-size:.72rem;font-weight:600">🔁 {stage_label}</span>'


def generate_ics_bytes():
    """Export exam deadlines plus completion/revision milestone dates."""
    cal = Calendar()
    cal.add("prodid", "-//Exam Study and Goal Tracker//")
    cal.add("version", "2.0")

    for goal in get_goals(order_by_deadline=False):
        deadline = parse_deadline(goal["deadline"])
        event = Event()
        event.add("summary", f"EXAM: {goal['subject']} — {goal['topic']} ({goal['exam_type']})")
        event.add("dtstart", deadline)
        event.add("dtend", deadline + timedelta(hours=1))
        event.add("dtstamp", datetime.now())
        event.add("description", f"Priority: {goal['priority']} | Status: {goal['status']}")
        event.add("uid", f"goal-{goal['id']}@exam-study-tracker")
        cal.add_component(event)

        if goal.get("completed_at"):
            completed = datetime.fromisoformat(goal["completed_at"])
            milestone = Event()
            milestone.add("summary", f"MILESTONE: Completed — {goal['subject']} / {goal['topic']}")
            milestone.add("dtstart", completed)
            milestone.add("dtend", completed + timedelta(minutes=15))
            milestone.add("dtstamp", datetime.now())
            milestone.add("uid", f"completed-{goal['id']}@exam-study-tracker")
            cal.add_component(milestone)

        stage = goal.get("review_stage") or 0
        if 1 <= stage <= 3 and goal.get("next_review_date"):
            review = Event()
            review.add("summary", f"REVIEW: {goal['subject']} — {goal['topic']} ({SRS_STAGE_LABELS[stage]})")
            review.add("dtstart", date.fromisoformat(goal["next_review_date"]))
            review.add("dtstamp", datetime.now())
            review.add("description", "Spaced-repetition revision milestone")
            review.add("uid", f"review-{goal['id']}-{stage}@exam-study-tracker")
            cal.add_component(review)

    return cal.to_ical()


# =============================================================================
# Goal notes / editing UI
# =============================================================================

def render_goal_notes(goal):
    with st.expander("📝 Notes / formulas / doubts", expanded=False):
        notes_key = f"notes_{goal['id']}"
        if notes_key not in st.session_state:
            st.session_state[notes_key] = goal.get("notes") or ""
        edited = st.text_area(
            "Markdown editor",
            key=notes_key,
            height=180,
            placeholder="## Key formulas\n- Formula 1\n\n## Doubts\n- Clarify...",
            label_visibility="collapsed",
        )
        if st.button("💾 Save Notes", key=f"save_notes_{goal['id']}"):
            save_goal_notes(goal["id"], edited)
            st.success("Notes saved.")
        if edited.strip():
            st.markdown("**Preview**")
            st.markdown(edited)
        else:
            st.caption("No notes saved for this goal yet.")


def render_edit_goal(goal_id):
    st.header("✏️ Edit Goal")
    goal = get_goal_by_id(goal_id)
    if not goal:
        st.error("That goal no longer exists.")
        return

    deadline_dt = parse_deadline(goal["deadline"])
    st.caption(f"Editing #{goal['id']} — {goal['subject']} / {goal['topic']}")

    with st.form(f"edit_goal_form_{goal_id}"):
        c1, c2 = st.columns(2)
        with c1:
            subject = st.text_input("Subject", value=goal["subject"])
            topic = st.text_input("Topic", value=goal["topic"])
            exam_type = st.selectbox("Exam Type", EXAM_TYPES, index=EXAM_TYPES.index(goal["exam_type"]) if goal["exam_type"] in EXAM_TYPES else 0)
            deadline_date = st.date_input("Deadline Date", value=deadline_dt.date())
        with c2:
            priority = st.selectbox("Priority", PRIORITIES, index=PRIORITIES.index(goal["priority"]) if goal["priority"] in PRIORITIES else 1)
            status = st.selectbox("Status", STATUSES, index=STATUSES.index(goal["status"]) if goal["status"] in STATUSES else 0)
            deadline_time = st.time_input("Deadline Time", value=deadline_dt.time())
            notes = st.text_area("Notes (Markdown)", value=goal.get("notes") or "", height=180)

        save = st.form_submit_button("💾 Save Changes", type="primary", use_container_width=True)

    if save:
        if not subject.strip() or not topic.strip():
            st.error("Subject and Topic are required.")
        else:
            update_goal(
                goal_id, subject, topic,
                datetime.combine(deadline_date, deadline_time),
                priority, status, exam_type, notes,
            )
            st.success("Goal updated successfully.")
            st.session_state.edit_goal_id = goal_id
            st.rerun()

    st.divider()
    st.subheader("Danger Zone")
    confirm = st.checkbox("I understand that deleting this goal cannot be undone.", key=f"confirm_delete_{goal_id}")
    if st.button("🗑️ Delete Goal", disabled=not confirm, key=f"delete_edit_{goal_id}"):
        delete_goal(goal_id)
        st.session_state.edit_goal_id = None
        st.success("Goal deleted.")
        st.rerun()


def render_goal_editor_page():
    goals = get_goals(order_by_deadline=False)
    if not goals:
        st.header("✏️ Edit Goal")
        st.info("No goals exist yet. Add a goal first.")
        return

    labels = {g["id"]: f"#{g['id']} — {g['subject']} / {g['topic']}" for g in goals}
    current = st.session_state.get("edit_goal_id")
    ids = list(labels)
    default_index = ids.index(current) if current in ids else 0
    selected = st.selectbox("Select a goal to edit", ids, index=default_index, format_func=lambda x: labels[x])
    st.session_state.edit_goal_id = selected
    render_edit_goal(selected)


# =============================================================================
# Dashboard
# =============================================================================

def render_dashboard():
    st.header("📊 Dashboard")

    c1, c2, c3, c4 = st.columns(4)
    all_goals = get_goals(order_by_deadline=False)
    pending = [g for g in all_goals if g["status"] == "Pending"]
    c1.metric("Total Goals", len(all_goals))
    c2.metric("Pending", len(pending))
    c3.metric("Reviews Due", len(get_revision_queue()))
    c4.metric("Study Today", format_minutes(get_total_minutes_today()))

    st.download_button(
        "📅 Export Deadlines & Milestones (.ics)",
        data=generate_ics_bytes(),
        file_name="study_tracker_export.ics",
        mime="text/calendar",
        use_container_width=True,
    )

    subjects = ["All"] + get_distinct_subjects()
    f1, f2, f3 = st.columns(3)
    with f1:
        subject_filter = st.selectbox("Filter by Subject", subjects, key="dash_subject")
    with f2:
        exam_filter = st.selectbox("Filter by Exam Type", ["All"] + EXAM_TYPES, key="dash_exam")
    with f3:
        status_filter = st.selectbox("Status", ["All", "Pending", "Completed"], index=1, key="dash_status")

    goals = get_goals(subject_filter, exam_filter, status_filter)
    st.subheader("Goals")

    if not goals:
        st.info("No goals match the current filters. Add a goal from the sidebar.")
    else:
        now = datetime.now()
        today = date.today()
        for goal in goals:
            deadline = parse_deadline(goal["deadline"])
            bucket, delta = urgency_bucket(deadline, now)
            completed = goal["status"] == "Completed"

            with st.container(border=True):
                top, actions = st.columns([4, 1])
                with top:
                    st.markdown(f"### {goal['subject']} — {goal['topic']}")
                    badges = priority_badge_html(goal["priority"])
                    badges += f'&nbsp;&nbsp;<span style="background:#444;color:#fff;padding:1px 8px;border-radius:10px;font-size:.72rem">{goal["exam_type"]}</span>'
                    if not completed:
                        badges += "&nbsp;&nbsp;" + urgency_badge_html(bucket)
                    else:
                        badges += '&nbsp;&nbsp;<span style="background:#2f6fb0;color:#fff;padding:1px 8px;border-radius:10px;font-size:.72rem">COMPLETED</span>'
                    st.markdown(badges, unsafe_allow_html=True)
                    st.caption(f"Deadline: {deadline.strftime('%a, %d %b %Y %I:%M %p')}")

                    if not completed:
                        if bucket == "overdue":
                            st.markdown(f"**Overdue by:** `{format_countdown(delta).lstrip('-')}`")
                        else:
                            st.markdown(f"**Time remaining:** `{format_countdown(delta)}`")
                    elif goal.get("next_review_date") and 1 <= (goal.get("review_stage") or 0) <= 3:
                        review_date = date.fromisoformat(goal["next_review_date"])
                        overdue = review_date <= today
                        st.markdown(review_badge_html(SRS_STAGE_LABELS[goal["review_stage"]], overdue), unsafe_allow_html=True)
                        st.caption(f"Next revision due: {review_date.strftime('%a, %d %b %Y')}")

                    render_goal_notes(goal)

                with actions:
                    if st.button("✏️ Edit", key=f"edit_{goal['id']}", use_container_width=True):
                        st.session_state.edit_goal_id = goal["id"]
                        st.session_state.pending_page = "Edit Goal"
                        st.rerun()
                    if not completed:
                        if st.button("✅ Complete", key=f"complete_{goal['id']}", use_container_width=True):
                            mark_goal_completed(goal["id"])
                            st.rerun()
                    else:
                        if st.button("↩️ Reopen", key=f"reopen_{goal['id']}", use_container_width=True):
                            reopen_goal(goal["id"])
                            st.rerun()
                    if st.button("🗑️ Delete", key=f"delete_{goal['id']}", use_container_width=True):
                        delete_goal(goal["id"])
                        st.rerun()

    st.divider()
    st.subheader("📈 Syllabus Completion by Subject")
    if not all_goals:
        st.caption("No goals yet.")
    else:
        df = pd.DataFrame(all_goals)
        summary = df.groupby("subject")["status"].apply(lambda s: (s == "Completed").mean()).reset_index(name="pct")
        for _, row in summary.iterrows():
            st.write(f"**{row['subject']}** — {row['pct'] * 100:.0f}% complete")
            st.progress(float(row["pct"]))


# =============================================================================
# Add Goal
# =============================================================================

def render_add_goal():
    st.header("➕ Add a New Goal")
    with st.form("add_goal_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            subject = st.text_input("Subject", placeholder="e.g. Data Structures")
            topic = st.text_input("Topic", placeholder="e.g. Binary Search Trees")
            exam_type = st.selectbox("Exam Type", EXAM_TYPES)
            deadline_date = st.date_input("Deadline Date", value=date.today() + timedelta(days=7))
        with c2:
            priority = st.selectbox("Priority", PRIORITIES)
            deadline_time = st.time_input("Deadline Time", value=dtime(23, 59))
            notes = st.text_area("Notes / formulas / doubts (Markdown)", height=180)

        submitted = st.form_submit_button("Add Goal", type="primary", use_container_width=True)

    if submitted:
        if not subject.strip() or not topic.strip():
            st.error("Subject and Topic are required.")
        else:
            add_goal(subject, topic, datetime.combine(deadline_date, deadline_time), priority, exam_type, notes)
            st.success(f"Goal '{topic}' added for {subject}.")


# =============================================================================
# Resources
# =============================================================================

def render_resources():
    st.header("📎 Resource Attachments")
    st.caption("Files are stored persistently in Supabase Storage.")

    goals = get_goals(order_by_deadline=False)
    if not goals:
        st.warning("Add at least one goal first, then attach resources here.")
        return

    labels = {g["id"]: f"#{g['id']} — {g['subject']} / {g['topic']}" for g in goals}

    with st.form("upload_resource_form", clear_on_submit=True):
        goal_id = st.selectbox("Link to Goal", list(labels), format_func=lambda x: labels[x])
        file_type = st.selectbox("Resource Type", RESOURCE_TYPES)
        uploaded = st.file_uploader("Choose a file", type=["pdf", "ppt", "pptx", "png", "jpg", "jpeg"])
        submitted = st.form_submit_button("Upload", use_container_width=True)

    if submitted:
        if uploaded is None:
            st.error("Please choose a file.")
        else:
            try:
                storage_path = upload_resource_to_storage(goal_id, uploaded)
                add_resource(goal_id, uploaded.name, storage_path, file_type)
                st.success(f"Uploaded '{uploaded.name}' permanently.")
                st.rerun()
            except Exception as exc:
                st.error(f"Upload failed: {exc}")

    st.divider()
    filter_goal = st.selectbox(
        "Filter by Goal",
        ["All"] + list(labels),
        format_func=lambda x: "All Goals" if x == "All" else labels[x],
    )
    resources = get_resources(None if filter_goal == "All" else filter_goal)

    if not resources:
        st.info("No resources uploaded yet.")
        return

    for res in resources:
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 1, 1])
            with c1:
                linked = f"{res['goal_subject']} / {res['goal_topic']}" if res["goal_subject"] else "Unlinked"
                st.markdown(f"**{res['file_name']}**  \n`{res['file_type']}` — {linked}")
            with c2:
                try:
                    data = download_resource_bytes(res["storage_path"])
                    st.download_button(
                        "⬇️ Download",
                        data=data,
                        file_name=res["file_name"],
                        key=f"dl_{res['id']}",
                        use_container_width=True,
                    )
                except Exception as exc:
                    st.caption(f"Download unavailable: {exc}")
            with c3:
                if st.button("🗑️ Remove", key=f"del_res_{res['id']}", use_container_width=True):
                    delete_resource(res["id"])
                    st.rerun()


# =============================================================================
# Focus Timer
# =============================================================================

def init_timer_state():
    defaults = {
        "timer_running": False,
        "timer_end": None,
        "timer_start": None,
        "timer_subject": "",
        "timer_topic": None,
        "timer_goal_id": None,
        "timer_duration_min": 25,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def render_time_stats(subject, topic, goal_id):
    topic_total = get_topic_total_minutes(subject, topic, goal_id) if topic else 0
    subject_total = get_subject_total_minutes(subject) if subject else 0
    overall = get_overall_total_minutes()
    c1, c2, c3 = st.columns(3)
    c1.metric("Topic Total", format_minutes(topic_total))
    c2.metric("Subject Total", format_minutes(subject_total))
    c3.metric("Grand Total", format_minutes(overall))


def clear_timer_state():
    st.session_state.timer_running = False
    st.session_state.timer_end = None
    st.session_state.timer_start = None
    st.session_state.timer_subject = ""
    st.session_state.timer_topic = None
    st.session_state.timer_goal_id = None


def render_focus_timer():
    st.header("⏱️ Focus Timer")
    st.caption("Select a subject and specific topic before starting. Every completed session is stored persistently in Supabase.")
    init_timer_state()

    if not st.session_state.timer_running:
        subjects = get_distinct_subjects()
        c1, c2 = st.columns(2)

        with c1:
            if subjects:
                subject_choice = st.selectbox("Subject", subjects + ["Other (type below)"], key="timer_subject_choice")
                if subject_choice == "Other (type below)":
                    subject = st.text_input("Enter subject", key="timer_subject_manual")
                else:
                    subject = subject_choice
            else:
                subject = st.text_input("Subject", key="timer_subject_manual")

        with c2:
            subject_goals = get_goals(subject=subject, order_by_deadline=False) if subject else []
            goal_id = None
            topic = None
            if subject_goals:
                options = {g["id"]: f"{g['topic']} ({g['status']})" for g in subject_goals}
                options[None] = "General / no specific goal"
                selected = st.selectbox("Topic / Goal", list(options), format_func=lambda x: options[x], key="timer_goal_choice")
                if selected is None:
                    topic = st.text_input("Free-form topic", key="timer_topic_manual")
                else:
                    goal_id = selected
                    topic = next(g["topic"] for g in subject_goals if g["id"] == selected)
            else:
                topic = st.text_input("Topic", key="timer_topic_manual")

        if subject:
            st.caption("Current totals")
            render_time_stats(subject, topic, goal_id)

        duration_choice = st.selectbox("Session length", [15, 25, 45, 60, "Custom"])
        duration = st.number_input("Custom minutes", 1, 240, 25) if duration_choice == "Custom" else duration_choice

        if st.button("▶️ Start Focus Session", type="primary", use_container_width=True):
            if not subject or not subject.strip():
                st.error("Please select or enter a subject.")
            elif not topic or not topic.strip():
                st.error("Please select or enter a topic. Topic-level tracking requires a topic.")
            else:
                st.session_state.timer_subject = subject.strip()
                st.session_state.timer_topic = topic.strip()
                st.session_state.timer_goal_id = goal_id
                st.session_state.timer_duration_min = int(duration)
                st.session_state.timer_start = datetime.now()
                st.session_state.timer_end = datetime.now() + timedelta(minutes=int(duration))
                st.session_state.timer_running = True
                st.rerun()

    else:
        remaining = st.session_state.timer_end - datetime.now()
        seconds = int(remaining.total_seconds())
        topic_label = st.session_state.timer_topic
        st.markdown(f"### Studying: {st.session_state.timer_subject} — {topic_label}")

        if seconds <= 0:
            add_study_log(
                st.session_state.timer_subject,
                st.session_state.timer_duration_min,
                topic=topic_label,
                goal_id=st.session_state.timer_goal_id,
            )
            st.success(f"Session complete! Logged {st.session_state.timer_duration_min} minutes.")
            st.balloons()
            clear_timer_state()
        else:
            mins, secs = divmod(seconds, 60)
            st.markdown(f'<div style="text-align:center;font-size:3.5rem;font-weight:700">{mins:02d}:{secs:02d}</div>', unsafe_allow_html=True)
            total_seconds = st.session_state.timer_duration_min * 60
            st.progress(min(max(1 - seconds / total_seconds, 0), 1))

            c1, c2 = st.columns(2)
            with c1:
                if st.button("⏹️ Stop & Log Elapsed Time", use_container_width=True):
                    elapsed = max(1, int((datetime.now() - st.session_state.timer_start).total_seconds() // 60))
                    add_study_log(st.session_state.timer_subject, elapsed, topic=topic_label, goal_id=st.session_state.timer_goal_id)
                    st.success(f"Logged {elapsed} minutes.")
                    clear_timer_state()
                    st.rerun()
            with c2:
                if st.button("❌ Cancel (No Log)", use_container_width=True):
                    clear_timer_state()
                    st.rerun()

            time.sleep(1)
            st.rerun()

    st.divider()
    if st.button(
        "📝 Forgot to start the timer? Add a session manually",
        use_container_width=True,
    ):
        st.session_state.pending_page = "Manual Study Log"
        st.rerun()

    st.divider()
    c1, c2 = st.columns(2)
    c1.metric("Focused Today", format_minutes(get_total_minutes_today()))
    c2.metric("Grand Total", format_minutes(get_overall_total_minutes()))

    st.subheader("Recent Sessions")
    logs = get_study_logs(20)
    if logs:
        df = pd.DataFrame(logs)[["subject", "topic", "duration_minutes", "log_date"]]
        df.columns = ["Subject", "Topic", "Minutes", "Logged At"]
        df["Topic"] = df["Topic"].fillna("General")
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.caption("No sessions logged yet.")


# =============================================================================
# Manual Study Log
# =============================================================================

def render_manual_study_log():
    st.header("📝 Add Forgotten Study Session")
    st.caption(
        "Forgot to start the focus timer? Add the session manually with its "
        "actual date, start time, duration, subject and topic."
    )

    goals = get_goals(order_by_deadline=False)
    subjects = get_distinct_subjects()

    c1, c2 = st.columns(2)
    with c1:
        if subjects:
            subject = st.selectbox(
                "Subject",
                subjects + ["Other (type below)"],
                key="manual_log_subject_choice",
            )
            if subject == "Other (type below)":
                subject = st.text_input(
                    "Enter subject",
                    key="manual_log_subject",
                )
        else:
            subject = st.text_input("Subject", key="manual_log_subject")

    with c2:
        subject_goals = get_goals(subject=subject, order_by_deadline=False) if subject else []
        goal_id = None
        topic = None

        if subject_goals:
            options = {g["id"]: f"{g['topic']} ({g['status']})" for g in subject_goals}
            options[None] = "Other / free-form topic"
            selected_goal = st.selectbox(
                "Topic / Goal",
                list(options),
                format_func=lambda x: options[x],
                key="manual_log_goal",
            )

            if selected_goal is None:
                topic = st.text_input("Topic", key="manual_log_topic")
            else:
                goal_id = selected_goal
                topic = next(
                    g["topic"] for g in subject_goals if g["id"] == selected_goal
                )
        else:
            topic = st.text_input("Topic", key="manual_log_topic")

    c3, c4 = st.columns(2)
    with c3:
        log_date = st.date_input(
            "Study Date",
            value=date.today(),
            max_value=date.today(),
            key="manual_log_date",
        )
    with c4:
        start_time = st.time_input(
            "Start Time",
            value=dtime(18, 0),
            key="manual_log_start_time",
        )

    duration = st.number_input(
        "Duration (minutes)",
        min_value=1,
        max_value=1440,
        value=25,
        step=5,
        key="manual_log_duration",
        help="Enter the actual time you studied, even if you forgot to run the timer.",
    )

    if st.button(
        "➕ Add Study Session",
        type="primary",
        use_container_width=True,
        key="add_manual_study_log",
    ):
        if not subject or not subject.strip():
            st.error("Please select or enter a subject.")
            return
        if not topic or not topic.strip():
            st.error("Please select or enter a topic.")
            return

        log_datetime = datetime.combine(log_date, start_time)

        try:
            add_study_log(
                subject.strip(),
                int(duration),
                topic=topic.strip(),
                goal_id=goal_id,
                log_dt=log_datetime,
            )
            st.success(
                f"Added {format_minutes(duration)} of study time for "
                f"{subject.strip()} — {topic.strip()} on "
                f"{log_date:%d %b %Y} at {start_time:%I:%M %p}."
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Could not add study session: {exc}")

    st.divider()
    st.subheader("What gets recorded?")
    st.caption(
        "The session is added to the same study_logs table as Focus Timer "
        "sessions, so it contributes to topic totals, subject totals, "
        "heatmaps, streaks and analytics."
    )


# =============================================================================
# Revision Queue
# =============================================================================

def render_revision_queue():
    st.header("🔁 Revision Queue")
    st.caption("Completed goals are scheduled automatically for +1, +3 and +7 day reviews.")

    due = get_revision_queue()
    today = date.today()
    st.subheader("Due Today / Overdue")
    if not due:
        st.success("Nothing due right now. 🎉")
    else:
        for goal in due:
            review_date = date.fromisoformat(goal["next_review_date"])
            overdue = review_date < today
            stage = goal["review_stage"]
            with st.container(border=True):
                c1, c2 = st.columns([4, 1])
                with c1:
                    st.markdown(f"**{goal['subject']} — {goal['topic']}**")
                    st.markdown(review_badge_html(SRS_STAGE_LABELS[stage], overdue), unsafe_allow_html=True)
                    st.caption((f"Was due {review_date:%a, %d %b %Y} (overdue)" if overdue else f"Due today: {review_date:%a, %d %b %Y}"))
                    if goal.get("notes"):
                        with st.expander("View notes"):
                            st.markdown(goal["notes"])
                with c2:
                    if st.button("✅ Mark Reviewed", key=f"review_{goal['id']}", use_container_width=True):
                        advance_review(goal["id"])
                        st.rerun()

    st.divider()
    st.subheader("Upcoming Reviews")
    upcoming = get_upcoming_reviews()
    if upcoming:
        df = pd.DataFrame(upcoming)
        df["Stage"] = df["review_stage"].map(SRS_STAGE_LABELS)
        df = df[["subject", "topic", "Stage", "next_review_date"]]
        df.columns = ["Subject", "Topic", "Stage", "Due Date"]
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.caption("No upcoming reviews scheduled.")


# =============================================================================
# Analytics
# =============================================================================

def render_heatmap(daily_totals, weeks=26):
    end = date.today()
    start = end - timedelta(weeks=weeks - 1)
    start -= timedelta(days=start.weekday())
    end_display = start + timedelta(weeks=weeks) - timedelta(days=1)

    z = [[0] * weeks for _ in range(7)]
    x = [(start + timedelta(weeks=i)).strftime("%d %b") for i in range(weeks)]

    cursor = start
    while cursor <= end_display:
        wi = (cursor - start).days // 7
        if 0 <= wi < weeks:
            z[cursor.weekday()][wi] = daily_totals.get(cursor.isoformat(), 0)
        cursor += timedelta(days=1)

    fig = go.Figure(go.Heatmap(
        z=z,
        x=x,
        y=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        colorscale="Greens",
        hovertemplate="Week of %{x}<br>%{y}: %{z} min<extra></extra>",
    ))
    fig.update_layout(height=300, margin=dict(t=10, b=10, l=10, r=10), xaxis=dict(tickangle=-45), yaxis=dict(autorange="reversed"))
    st.plotly_chart(fig, use_container_width=True)


def render_time_distribution():
    subjects = get_subject_totals()
    c1, c2 = st.columns(2)

    with c1:
        st.markdown("**Study Time by Subject**")
        if subjects:
            df = pd.DataFrame(subjects)
            fig = px.pie(df, values="total", names="subject", hole=0.45)
            fig.update_layout(height=340, margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("No study sessions yet.")

    with c2:
        st.markdown("**Daily / Weekly Trend**")
        view = st.radio("View", ["Daily (30d)", "Weekly (12w)"], horizontal=True, label_visibility="collapsed")
        daily = get_daily_totals(365)
        if not daily:
            st.caption("No study sessions yet.")
            return

        series = pd.Series(daily, dtype=float)
        series.index = pd.to_datetime(series.index)
        series = series.sort_index()

        if view == "Daily (30d)":
            start = pd.Timestamp(date.today() - timedelta(days=29))
            index = pd.date_range(start, pd.Timestamp(date.today()))
            plot = series.reindex(index, fill_value=0).reset_index()
            plot.columns = ["date", "minutes"]
            fig = px.bar(plot, x="date", y="minutes")
        else:
            weekly = series.resample("W-MON", label="left", closed="left").sum().tail(12)
            plot = weekly.reset_index()
            plot.columns = ["week_start", "minutes"]
            fig = px.bar(plot, x="week_start", y="minutes")

        fig.update_layout(height=340, margin=dict(t=10, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)


def render_analytics():
    st.header("📈 Study Analytics")
    daily = get_daily_totals(365)
    current, longest = compute_streaks(daily)

    c1, c2, c3 = st.columns(3)
    c1.metric("🔥 Current Streak", f"{current} day{'s' if current != 1 else ''}")
    c2.metric("🏆 Longest Streak", f"{longest} day{'s' if longest != 1 else ''}")
    c3.metric("⏳ Grand Total", format_minutes(get_overall_total_minutes()))

    st.subheader("Study Activity Heatmap")
    if daily:
        render_heatmap(daily)
    else:
        st.caption("The heatmap will fill as you log study sessions.")

    st.divider()
    st.subheader("Interactive Charts")
    render_time_distribution()


# =============================================================================
# Main
# =============================================================================

def main():
    st.set_page_config(page_title="Exam Study & Goal Tracker", page_icon="🎯", layout="wide")
    init_db()
    run_migrations()

    st.session_state.setdefault("navigate_page", "Dashboard")
    st.session_state.setdefault("pending_page", None)
    st.session_state.setdefault("edit_goal_id", None)

    st.sidebar.title("🎯 Study Tracker")
    pages = [
        "Dashboard",
        "Add Goal",
        "Edit Goal",
        "Resources",
        "Focus Timer",
        "Manual Study Log",
        "Revision Queue",
        "Analytics",
    ]
    pending_page = st.session_state.pop("pending_page", None)
    if pending_page in pages:
        st.session_state["navigate_page"] = pending_page

    page = st.sidebar.radio("Navigate", pages, key="navigate_page", label_visibility="collapsed")

    st.sidebar.divider()
    total_goals = len(get_goals(order_by_deadline=False))
    pending = len(get_goals(status="Pending", order_by_deadline=False))
    st.sidebar.caption(f"Total goals: {total_goals}")
    st.sidebar.caption(f"Pending: {pending}")
    st.sidebar.caption(f"Reviews due: {len(get_revision_queue())}")
    st.sidebar.caption(f"Focused today: {get_total_minutes_today()} min")
    st.sidebar.caption(f"Overall study time: {format_minutes(get_overall_total_minutes())}")

    if page == "Dashboard":
        render_dashboard()
    elif page == "Add Goal":
        render_add_goal()
    elif page == "Edit Goal":
        render_goal_editor_page()
    elif page == "Resources":
        render_resources()
    elif page == "Focus Timer":
        render_focus_timer()
    elif page == "Manual Study Log":
        render_manual_study_log()
    elif page == "Revision Queue":
        render_revision_queue()
    elif page == "Analytics":
        render_analytics()


if __name__ == "__main__":
    main()
