"""Red-team report: run evals/redteam/queries.yaml through the guardrails (and optionally the live database).

    python -m evals.redteam                       # guardrail layer only, offline (saved schema fixture)
    python -m evals.redteam --live --source wealthlens   # also: EXPLAIN gate + DB-role bypass probe

Exit code 1 if ANY dangerous query is not blocked, or any look-alike is falsely blocked.
The bypass probe runs each query directly in the read-only sandbox (guardrail skipped) to show what the
database role alone would do. It is meant for a throwaway/dev database, never a production one.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy.exc import DBAPIError

from text2sql.config.sources import load_source
from text2sql.guardrails import GuardrailEngine, load_guardrail_config
from text2sql.guardrails.explain_gate import ExplainGate
from text2sql.schema.model import SchemaSnapshot

ROOT = Path(__file__).resolve().parents[1]
QUERIES = ROOT / "evals" / "redteam" / "queries.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "wealthlens_schema.json"


def _category(case_id: str) -> str:
    return case_id.split("-", 1)[0]


def run(source_name: str, live: bool) -> dict:
    fixtures = yaml.safe_load(QUERIES.read_text(encoding="utf-8"))
    source = load_source(source_name)
    cfg = load_guardrail_config(source)
    engine = GuardrailEngine(cfg)
    connector = gate = None
    if live:
        from text2sql.connectors.registry import get_connector
        from text2sql.schema.introspect import introspect_connector

        connector = get_connector(source)
        snapshot = introspect_connector(connector)
        gate = ExplainGate(connector, cfg.explain, cfg.statement_timeout_ms)
    else:
        snapshot = SchemaSnapshot.model_validate_json(FIXTURE.read_text(encoding="utf-8"))

    rows, escaped = [], []
    for case in fixtures["queries"]:
        decision = engine.check(case["sql"], snapshot)
        layer = "guardrail"
        blocked = decision.blocked
        if case.get("layer") == "explain":
            layer = "explain gate"
            if live and decision.allowed:
                blocked = not gate.check(decision.sql).ok
            elif not live:
                blocked, layer = None, "explain gate (needs --live)"
        db = None
        if (
            live
            and case.get("db_layer")
            and not case.get("skip_db_bypass")
            and case.get("layer") != "explain"
        ):
            try:
                with connector.readonly_connection(800) as conn:
                    res = conn.exec_driver_sql(connector.driver_sql(case["sql"]))
                    if res.returns_rows:
                        res.fetchall()
                db = "allowed"
            except DBAPIError:
                db = "rejected"
        if blocked is False:
            escaped.append(case["id"])
        rows.append(
            {
                "id": case["id"],
                "category": _category(case["id"]),
                "layer": layer,
                "blocked": blocked,
                "rules": decision.rule_ids,
                "expected_db": case.get("db_layer"),
                "db": db,
            }
        )

    false_positives = [c["id"] for c in fixtures["must_allow"] if engine.check(c["sql"], snapshot).blocked]
    return {
        "rows": rows,
        "escaped": escaped,
        "false_positives": false_positives,
        "n_allow_cases": len(fixtures["must_allow"]),
        "live": live,
    }


def render(result: dict) -> str:
    rows = result["rows"]
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    lines = [
        "# Red-team report",
        "",
        f"**{sum(1 for r in rows if r['blocked'])}/{len(rows)} dangerous queries blocked**"
        f" ({len(result['escaped'])} escaped), "
        f"{result['n_allow_cases'] - len(result['false_positives'])}/{result['n_allow_cases']} harmless look-alikes allowed.",  # noqa: E501
        "",
        "| Category | Cases | Blocked | DB role alone: rejected / would run |",
        "|---|---|---|---|",
    ]
    for cat, items in sorted(by_cat.items()):
        blocked = sum(1 for r in items if r["blocked"])
        if result["live"]:
            rej = sum(1 for r in items if r["db"] == "rejected")
            run_ = sum(1 for r in items if r["db"] == "allowed")
            db_cell = f"{rej} / {run_}"
        else:
            db_cell = "(run with --live)"
        lines.append(f"| {cat} | {len(items)} | {blocked} | {db_cell} |")
    if result["live"]:
        drift = [r["id"] for r in rows if r["db"] and r["db"] != r["expected_db"]]
        lines += ["", f"Database behaviour differs from the fixture's expectation for: {drift or 'none'}"]
        only = [r["id"] for r in rows if r["db"] == "allowed"]
        lines += [
            "",
            f"Queries the database role would run if the guardrail were bypassed (guardrail is the only defence): {', '.join(only) or 'none'}",  # noqa: E501
        ]
    if result["escaped"]:
        lines += ["", f"**ESCAPED:** {', '.join(result['escaped'])}"]
    if result["false_positives"]:
        lines += ["", f"**FALSE POSITIVES:** {', '.join(result['false_positives'])}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", default="wealthlens")
    p.add_argument(
        "--live", action="store_true", help="use the live read-only database (EXPLAIN gate + bypass probe)"
    )
    p.add_argument("--report-dir", type=Path, default=ROOT / "evals" / "reports")
    args = p.parse_args(argv)
    result = run(args.source, args.live)
    text = render(result)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    stem = args.report_dir / f"redteam_{datetime.now():%Y%m%d_%H%M%S}"
    stem.with_suffix(".md").write_text(text, encoding="utf-8")
    stem.with_suffix(".json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(text)
    print(f"Report: {stem.with_suffix('.md')}")
    return 1 if result["escaped"] or result["false_positives"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
