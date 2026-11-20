"""Streamlit UI for Text-To-SQL.

Run (PowerShell), with the API already running:
    streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # so `app` and `text2sql` import from any cwd

import streamlit as st  # noqa: E402

from app import components as ui  # noqa: E402
from app.api_client import ApiClient, ApiError  # noqa: E402

st.set_page_config(page_title="Ask your data", page_icon="💬", layout="wide")
st.markdown(ui.CSS, unsafe_allow_html=True)
ss = st.session_state
ss.setdefault("session_id", uuid.uuid4().hex)
ss.setdefault("result", None)
ss.setdefault("fb_done", {})
ss.setdefault("fb_open", None)
ss.setdefault("flash", None)
ss.setdefault("question", "")
client = ApiClient(session_id=ss.session_id)


@st.cache_data(ttl=60, show_spinner=False)
def load_sources(url: str, key: str) -> list[dict]:
    return ApiClient(url, key).sources()


@st.cache_data(ttl=300, show_spinner="Reading the schema…")
def load_schema(url: str, key: str, source: str) -> dict:
    return ApiClient(url, key).schema(source)


# ---- actions ------------------------------------------------------------------------------------
def ask(question: str, clarifications: dict | None = None) -> None:
    with st.status("Working on your question…", expanded=True) as status:
        st.write("Writing the SQL, checking it is safe, running it read-only and validating the answer.")
        try:
            r = client.query(question, ss.source, clarifications)
        except ApiError as err:
            status.update(label="Could not finish", state="error", expanded=True)
            ui.show_error(err)
            return
        status.update(
            label=f"Done in {r['timings_ms'].get('request', 0) / 1000:.1f}s", state="complete", expanded=False
        )
    ss.result, ss.fb_open = r, None


def rerun_sql(sql: str) -> None:
    with st.spinner("Checking the SQL against the safety rules and running it…"):
        try:
            ss.result, ss.fb_open = client.run_sql(sql, ss.source), None
        except ApiError as err:
            ui.show_error(err)
            return
    st.rerun()


def use_example(text: str) -> None:  # on_click callback: runs before the next script run
    ss.question, ss.run_now = text, True


def open_history(audit_id: str) -> None:
    try:
        ss.result, ss.fb_open = client.history_item(audit_id), None
    except ApiError as err:
        ss.flash = err.detail


def send_feedback(r: dict, verdict: str, comment: str | None = None, corrected: str | None = None) -> None:
    try:
        client.feedback(r["audit_id"], verdict, comment, corrected)
        ss.fb_done[r["audit_id"]] = verdict
        ss.fb_open = None
    except ApiError as err:
        ui.show_error(err)


# ---- sidebar (top): source + health -------------------------------------------------------------
try:
    sources = load_sources(client.base_url, client.api_key)
except ApiError as err:
    st.title("Ask your data")
    ui.show_error(err)
    st.stop()
if not sources:
    st.error("The API has no data sources configured (see the `sources/` folder).")
    st.stop()
by_name = {s["name"]: s for s in sources}
default_name = next((s["name"] for s in sources if s["default"]), sources[0]["name"])

with st.sidebar:
    st.header("Data source")
    ss.setdefault("source", default_name)
    st.selectbox(
        "Source",
        list(by_name),
        key="source",
        format_func=lambda n: by_name[n]["display_name"],
        on_change=lambda: ss.update(result=None, fb_open=None),
        label_visibility="collapsed",
    )
    health = client.health()
    if health is None:
        st.warning("API not reachable")
    elif health.get("status") != "ok":
        st.warning("API is up but the database is unreachable")
    else:
        st.caption("● Connected · read-only access")
    if st.toggle("Browse available tables"):
        try:
            schema = load_schema(client.base_url, client.api_key, ss.source)
            for t in schema["tables"]:
                with st.expander(t["name"]):
                    if t.get("comment"):
                        st.caption(t["comment"])
                    st.markdown("\n".join(f"- `{c['name']}` {c['type']}" for c in t["columns"]))
        except ApiError as err:
            ui.show_error(err)
    if st.button("Start a new session", use_container_width=True):
        for k in ("session_id", "result", "fb_done", "fb_open", "question"):
            ss.pop(k, None)
        st.rerun()

source = by_name[ss.source]

# ---- main ---------------------------------------------------------------------------------------
st.title("Ask your data")
st.caption(f"Ask in plain English about **{source['display_name']}**. Every answer shows how sure we are.")

if source["examples"]:
    st.caption("Try one of these:")
    cols = st.columns(min(3, len(source["examples"])))
    for i, ex in enumerate(source["examples"][:6]):
        cols[i % len(cols)].button(
            ex, key=f"ex_{i}", on_click=use_example, args=(ex,), use_container_width=True
        )

with st.form("ask_form", clear_on_submit=False):
    st.text_input(
        "Your question", key="question", placeholder="e.g. How much did I spend on groceries last month?"
    )
    submitted = st.form_submit_button("Ask", type="primary")

if ss.flash:
    st.warning(ss.flash)
    ss.flash = None

run_now = ss.pop("run_now", False)
if (submitted or run_now) and ss.question.strip():
    ask(ss.question)
elif submitted:
    st.warning("Type a question first.")


def sql_panel(r: dict) -> None:
    if not r.get("sql"):
        return
    st.markdown("**SQL**")
    st.code(r["sql"], language="sql")
    if st.toggle("Edit SQL", key=f"edit_{r['audit_id']}"):
        new_sql = st.text_area(
            "Edit the SQL",
            value=r["sql"],
            height=180,
            key=f"sqlbox_{r['audit_id']}",
            label_visibility="collapsed",
        )
        if st.button("Re-run through guardrails", key=f"rerun_{r['audit_id']}"):
            rerun_sql(new_sql)
        st.caption("Edited SQL goes through the same safety checks. It is not confidence-scored.")


def feedback_panel(r: dict) -> None:
    if r["kind"] != "ask" or r["status"] != "success":
        return
    aid = r["audit_id"]
    st.divider()
    if aid in ss.fb_done:
        st.success("Thanks. Saved for review; it will not change any answers until a reviewer approves it.")
        return
    st.markdown("**Was this answer right?**")
    c1, c2, _ = st.columns([1, 1, 5])
    if c1.button("👍 Yes, correct", key=f"up_{aid}"):
        send_feedback(r, "correct")
        st.rerun()
    if c2.button("👎 No, wrong", key=f"down_{aid}"):
        ss.fb_open = aid
    if ss.fb_open == aid:
        with st.form(f"fb_{aid}"):
            comment = st.text_input("What was wrong? (optional)")
            fixed = st.text_area("The SQL it should have used (optional)", value="", height=100)
            if st.form_submit_button("Send feedback"):
                send_feedback(r, "incorrect", comment, fixed)
                st.rerun()


def render(r: dict) -> None:
    if r.get("question") and r["kind"] == "ask":
        st.markdown(f"##### {r['question']}")
    s = r["status"]
    if s == "needs_clarification":
        choice = ui.clarification_card(r)
        if choice is not None:
            ask(r["question"], choice)
            st.rerun()
        return
    if s == "unanswerable":
        st.info(f"**This data cannot answer that.** {r.get('message', '')}")
        ui.confidence_badge(r)
        return
    if s == "blocked":
        ui.blocked_panel(r)
        sql_panel(r)
        return
    if s == "failed":
        st.error(r.get("message") or "The query could not be completed. Try rephrasing the question.")
        ui.attempts_note(r)
        sql_panel(r)
        return
    ui.confidence_badge(r)  # always above the numbers
    ui.guardrail_notes(r)
    ui.alternative_panel(r)
    ui.results_table(r)
    if r.get("explanation"):
        st.markdown(f"**What this does:** {r['explanation']}")
    sql_panel(r)
    ui.evidence_panel(r)
    ui.attempts_note(r)
    feedback_panel(r)


if ss.result:
    st.divider()
    render(ss.result)
else:
    st.markdown("---")
    st.markdown(
        "**How your data stays safe**\n\n"
        "- Queries run **read-only**; nothing can be changed or deleted.\n"
        "- Every query is checked before it runs and **blocked with a reason** if it is risky.\n"
        "- Every answer is **double-checked** and scored, so you know when to look closer."
    )

# ---- sidebar (bottom): history, rendered after the main flow so it is always current ---------------
with st.sidebar:
    st.header("History")
    try:
        items = client.history(ss.source)
    except ApiError:
        items = []
    if not items:
        st.caption("Your questions will appear here.")
    for it in items:
        label = (it["question"] or "Edited SQL")[:46]
        lvl = f" · {it['level']}" if it.get("level") else ""
        st.button(
            f"{ui.STATUS_ICON.get(it['status'], '•')} {label}{lvl}",
            key=f"h_{it['audit_id']}",
            on_click=open_history,
            args=(it["audit_id"],),
            use_container_width=True,
        )
