"""Reusable Streamlit pieces. Rules: never show a result without its confidence; explain blocks in
plain language; never rely on colour alone (every badge has words)."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from app.api_client import ApiError

LEVEL_STYLE = {
    "high": ("#1a7f37", "#e6f4ea", "High confidence"),
    "medium": ("#9a6700", "#fff4d6", "Medium confidence"),
    "low": ("#b42318", "#fde8e6", "Low confidence"),
    "cannot_answer": ("#57606a", "#eef0f2", "Cannot be answered from this data"),
}
STATUS_ICON = {
    "success": "✅",
    "needs_clarification": "❓",
    "unanswerable": "🚫",
    "blocked": "🛑",
    "failed": "⚠️",
}

CSS = """
<style>
.t2s-badge{display:flex;align-items:center;gap:.75rem;padding:.7rem 1rem;border-radius:.6rem;border:1px solid;margin:.25rem 0 .5rem 0}
.t2s-badge b{font-size:1.05rem}.t2s-badge span{opacity:.9}
.t2s-pct{font-size:1.6rem;font-weight:700;min-width:4.2rem}
.block-container{padding-top:2rem}
</style>
"""


def show_error(err: ApiError) -> None:
    if err.kind == "unreachable":
        st.error(f"{err.detail} Start it in another terminal:  `uvicorn api.main:app --port 8000`")
    elif err.kind == "auth":
        st.error(
            "The API rejected the key. Check that `API_KEY` in your `.env` is one of the server's `API_KEYS`."
        )
    elif err.kind == "rate":
        st.warning(f"Slow down a little. You can ask again in about {err.retry_after or 30} seconds.")
    elif err.kind == "upstream":
        st.error(f"{err.detail}")
    elif err.kind in ("invalid", "too_large"):
        st.warning(f"That input cannot be used: {err.detail}")
    else:
        st.error(err.detail)


def confidence_badge(r: dict[str, Any]) -> None:
    c = r.get("confidence")
    if not c:
        st.info(r.get("confidence_note") or "No confidence score for this result.")
        return
    fg, bg, label = LEVEL_STYLE[c["level"]]
    pct = "" if c["level"] == "cannot_answer" else f"{round(c['score'] * 100)}%"
    st.markdown(
        f'<div class="t2s-badge" style="color:{fg};background:{bg};border-color:{fg}">'
        f'<div class="t2s-pct">{pct or "n/a"}</div><div><b>{c["headline"]}</b><br>'
        f"<span>Based on {c['signals_used']} of {c['signals_total']} independent checks"
        f"{'' if c['calibrated'] else ' · weights not yet calibrated'}</span></div></div>",
        unsafe_allow_html=True,
    )
    if c["level"] == "low":
        st.warning("Check the SQL and the numbers yourself before using this answer.")
    if c["level"] != "cannot_answer":
        st.progress(min(max(c["score"], 0.0), 1.0))
    with st.expander("Why this score? See the checks"):
        rows = [
            {
                "Check": s["name"].replace("_", " "),
                "Result": f"{round(s['value'] * 100)}%" if s["value"] is not None else "not measured",
                "Weight": s["weight"],
                "What was found": s["detail"],
            }
            for s in c["signals"]
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        for cap in c["caps_applied"]:
            st.caption(f"Score capped: {cap}")
        for reason in c["reasons"]:
            st.markdown(f"- {reason}")


def results_table(r: dict[str, Any]) -> None:
    if not r["columns"]:
        return
    df = pd.DataFrame(r["rows"], columns=r["columns"])
    st.dataframe(df, use_container_width=True, hide_index=True)  # click headers to sort
    meta = f"{r['row_count']} row(s)" + (
        f" · {r['elapsed_ms']} ms" if r.get("elapsed_ms") is not None else ""
    )
    st.caption(meta + (f" · result fingerprint {r['result_hash']}" if r.get("result_hash") else ""))
    if r.get("truncated"):
        st.warning("The result was cut at the row limit. Add filters to see the rest.")
    if r["row_count"] == 0:
        st.info("The query ran fine and found no matching rows. Try widening the dates or filters.")
    st.download_button(
        "Download CSV",
        df.to_csv(index=False).encode("utf-8"),
        file_name=f"result_{r['audit_id']}.csv",
        mime="text/csv",
    )


def guardrail_notes(r: dict[str, Any]) -> None:
    g = r.get("guardrail")
    if not g:
        return
    for w in g.get("warnings", []):
        st.warning(w)
    if g.get("row_limit_applied") and r["status"] == "success":
        st.caption(f"Safety: a limit of {g['row_limit_applied']} rows was applied automatically.")


def blocked_panel(r: dict[str, Any]) -> None:
    st.error("**This query was not run.** " + (r.get("message") or "It did not pass the safety checks."))
    g = r.get("guardrail") or {}
    for e in g.get("explanations", []):
        st.markdown(f"**{e['title']}** ({e['rule_id']}): {e['plain']}")
    if g.get("reasons"):
        with st.expander("Technical details"):
            for reason in g["reasons"]:
                st.code(reason, language="text")
    st.caption("Nothing was changed in your database. Every query runs read-only.")


def clarification_card(r: dict[str, Any]) -> dict[str, int] | None:
    """Returns the chosen {term: option index} when the person confirms, otherwise None."""
    cl = r["clarification"]
    st.info("**Your question could mean more than one thing.** Pick the meaning you want.")
    choices: dict[str, int] = {}
    for t in cl["terms"]:
        labels = [o["label"] for o in t["options"]]
        picked = st.radio(f"“{t['matched_text']}” means…", labels, key=f"clar_{r['audit_id']}_{t['term']}")
        idx = labels.index(picked)
        choices[t["term"]] = t["options"][idx]["index"]
        with st.expander("See the SQL for this meaning"):
            st.code(t["options"][idx]["example_sql"], language="sql")
    return choices if st.button("Use this meaning", type="primary", key=f"clar_go_{r['audit_id']}") else None


def alternative_panel(r: dict[str, Any]) -> None:
    a = r.get("alternative")
    if not a or a["status"] != "disagree":
        return
    st.warning(
        "**A second, independent query returned different data.** Compare both before trusting either."
    )
    st.caption(a.get("detail") or "")
    with st.expander("Show approach B", expanded=True):
        if a.get("explanation"):
            st.write(a["explanation"])
        st.code(a["sql"] or "", language="sql")
        if a["columns"]:
            st.dataframe(
                pd.DataFrame(a["preview_rows"], columns=a["columns"]),
                hide_index=True,
                use_container_width=True,
            )


def evidence_panel(r: dict[str, Any]) -> None:
    bt, san = r.get("back_translation"), r.get("sanity") or []
    if not bt and not san:
        return
    with st.expander("How the answer was checked"):
        if bt:
            st.markdown(f"**The SQL was translated back into a question:** {bt['summary']}")
            st.caption(f"Agreement with your question: {round(bt['alignment'] * 100)}%")
            for issue in bt["issues"]:
                st.markdown(f"- {issue}")
        icons = {"pass": "✅", "warn": "⚠️", "fail": "❌", "skip": "➖"}
        for c in san:
            st.markdown(f"{icons.get(c['status'], '•')} **{c['name'].replace('_', ' ')}** {c['detail']}")


def attempts_note(r: dict[str, Any]) -> None:
    if len(r.get("attempts", [])) > 1:
        with st.expander(f"The first query needed {len(r['attempts']) - 1} repair(s)"):
            for a in r["attempts"]:
                st.markdown(
                    f"{a['n']}. {'worked' if a['ok'] else 'failed at ' + a['stage']}"
                    + (f": {a['error'][:160]}" if a.get("error") and not a["ok"] else "")
                )
