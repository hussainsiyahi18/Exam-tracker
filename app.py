from pathlib import Path
import re

src_path = Path("/mnt/data/Pasted text(20260819-110946).txt")
src = src_path.read_text(encoding="utf-8")

# Imports/config: remove local SQLite/filesystem dependencies, add Supabase.
src = src.replace("Single-file Streamlit + SQLite application with:", "Single-file Streamlit + Supabase application with:")
src = src.replace(
    "pip install streamlit pandas plotly icalendar",
    "pip install streamlit pandas plotly icalendar supabase"
)
src = src.replace(
    "import os\nimport sqlite3\nimport subprocess\nimport sys\nimport time\nimport uuid\nfrom contextlib import contextmanager\n",
    "import mimetypes\nimport time\nimport uuid\n"
)
src = src.replace(
    "from icalendar import Calendar, Event\n",
    "from icalendar import Calendar, Event\nfrom supabase import create_client, Client\n"
)

config_start = src.index("# =============================================================================\n# Configuration")
db_start = src.index("# =============================================================================\n# Database")
goal_crud_start = src.index("# =============================================================================\n# Goal CRUD")

new_config_db = '''# =============================================================================
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


'''

src = src[:config_start] + new_config_db + src[goal_crud_start:]

# Replace Goal CRUD + SRS + Resources + study log DB helpers in one large block.
goal_start = src.index("# =============================================================================\n# Goal CRUD")
formatting_start = src.index("# =============================================================================\n# Formatting / calendar export")

new_data_layer = r'''# =============================================================================
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


'''

src = src[:goal_start] + new_data_layer + src[formatting_start:]

# Replace Resources UI only.
resources_ui_start = src.index("# =============================================================================\n# Resources\n# =============================================================================", src.index("# Add Goal"))
focus_start = src.index("# =============================================================================\n# Focus Timer", resources_ui_start)

new_resources_ui = r'''# =============================================================================
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


'''

src = src[:resources_ui_start] + new_resources_ui + src[focus_start:]

# Update stale UI wording.
src = src.replace(
    'st.caption("Select a subject and specific topic before starting. Every completed session is stored in SQLite.")',
    'st.caption("Select a subject and specific topic before starting. Every completed session is stored persistently in Supabase.")'
)

# Main init remains valid (init_db now validates Supabase).
out = Path("/mnt/data/app.py")
out.write_text(src, encoding="utf-8")

# SQL setup: idempotent schema + private storage bucket.
sql = r'''-- Run this ONCE in Supabase Dashboard → SQL Editor.
-- It is safe to run again.

create table if not exists public.goals (
    id bigint generated by default as identity primary key,
    subject text not null,
    topic text not null,
    deadline text not null,
    priority text not null,
    status text not null default 'Pending',
    exam_type text not null,
    notes text default '',
    completed_at text,
    next_review_date text,
    review_stage integer not null default 0
);

create table if not exists public.study_logs (
    id bigint generated by default as identity primary key,
    subject text not null,
    duration_minutes integer not null,
    log_date text not null,
    topic text,
    goal_id bigint references public.goals(id) on delete set null
);

create table if not exists public.resources (
    id bigint generated by default as identity primary key,
    goal_id bigint references public.goals(id) on delete cascade,
    file_name text not null,
    storage_path text not null,
    file_type text not null
);

-- Backward-compatible additions if you already created an earlier Supabase schema.
alter table public.goals add column if not exists notes text default '';
alter table public.goals add column if not exists completed_at text;
alter table public.goals add column if not exists next_review_date text;
alter table public.goals add column if not exists review_stage integer not null default 0;
alter table public.study_logs add column if not exists topic text;
alter table public.study_logs add column if not exists goal_id bigint references public.goals(id) on delete set null;
alter table public.resources add column if not exists storage_path text;

-- This app uses a server-side Supabase secret key stored in Streamlit Secrets.
-- RLS stays enabled; the secret/service role bypasses it.
alter table public.goals enable row level security;
alter table public.study_logs enable row level security;
alter table public.resources enable row level security;

-- Private bucket for PDFs/PPTs/images.
insert into storage.buckets (id, name, public)
values ('study-resources', 'study-resources', false)
on conflict (id) do update set public = false;
'''
Path("/mnt/data/supabase_setup.sql").write_text(sql, encoding="utf-8")

requirements = """streamlit
pandas
plotly
icalendar
supabase
"""
Path("/mnt/data/requirements.txt").write_text(requirements, encoding="utf-8")

# Syntax check
compile(src, str(out), "exec")

print("Created:")
print("/mnt/data/app.py")
print("/mnt/data/supabase_setup.sql")
print("/mnt/data/requirements.txt")
