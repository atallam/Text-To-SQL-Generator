"""Ask a question from the command line (or run your own SQL through the same guardrails).

    python -m scripts.ask "How much did I spend on groceries last month?"
    python -m scripts.ask "How are my investments performing?" --clarify returns=0
    python -m scripts.ask --sql "SELECT name FROM accounts"
    python -m scripts.ask "..." --show-prompt     # print the exact prompt sent to the model

Needs ANTHROPIC_API_KEY and the source's read-only URL in .env (see .env.example).
"""

from __future__ import annotations

import argparse
import sys

from text2sql.config.settings import get_settings
from text2sql.observability.logging import configure_logging
from text2sql.pipeline import QueryOutcome, Text2SQL

ICON = {
    "success": "OK",
    "needs_clarification": "?",
    "unanswerable": "CANNOT ANSWER",
    "blocked": "BLOCKED",
    "failed": "FAILED",
}


def parse_clarifications(items: list[str]) -> dict[str, int | str]:
    out: dict[str, int | str] = {}
    for item in items:
        term, sep, choice = item.partition("=")
        if not sep:
            raise SystemExit(f"--clarify expects term=choice, got {item!r}")
        out[term.strip()] = int(choice) if choice.strip().isdigit() else choice.strip()
    return out


def show(out: QueryOutcome, rows: int) -> None:
    print(
        f"\n[{ICON[out.status]}] {out.status}  (audit {out.audit_id}, {out.timings_ms.get('request', 0)} ms)"
    )
    if out.message and out.status != "success":
        print(out.message)
    if out.clarification:
        for term in out.clarification.terms:
            print(f"\n'{term.term}' could mean:")
            for i, opt in enumerate(term.options):
                print(f"  {i}. {opt.label}")
            print(f"  -> re-run with: --clarify {term.term}=<number>")
    if out.generated and out.generated.explanation and not out.generated.unanswerable:
        print(f"\nWhat it does: {out.generated.explanation}")
    if out.sql:
        print(f"\nSQL:\n{out.sql}")
    if out.guardrail:
        g = out.guardrail
        for w in g.warnings:
            print(f"  note: {w}")
        if g.rule_ids and out.status != "success":
            print(f"  guardrail rules: {', '.join(g.rule_ids)}")
        if g.plan_cost is not None:
            print(f"  planner estimate: cost {g.plan_cost:,.0f}, ~{g.plan_rows:,.0f} rows")
    if len(out.attempts) > 1:
        print("\nAttempts:")
        for a in out.attempts:
            print(
                f"  {a.n}. {'ok' if a.ok else 'failed at ' + a.stage}{': ' + a.error[:120] if a.error and not a.ok else ''}"  # noqa: E501
            )
    if out.execution:
        e = out.execution
        print(
            f"\n{e.row_count} row(s){' (truncated)' if e.truncated else ''}, {e.elapsed_ms} ms, result hash {e.result_hash}\n"  # noqa: E501
        )
        print(e.to_dataframe().head(rows).to_string(index=False))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("question", nargs="?")
    p.add_argument("--source", default=None)
    p.add_argument("--sql", help="run this SQL through the guardrails instead of asking the model")
    p.add_argument("--clarify", action="append", default=[], metavar="TERM=CHOICE")
    p.add_argument("--rows", type=int, default=20, help="rows to print")
    p.add_argument("--max-iterations", type=int, default=3)
    p.add_argument("--show-prompt", action="store_true")
    p.add_argument("--verbose", action="store_true", help="show structured logs")
    args = p.parse_args(argv)
    if not args.question and not args.sql:
        p.error("give a question or --sql")

    settings = get_settings()
    configure_logging("INFO" if args.verbose else "CRITICAL", json=False)
    t2s = Text2SQL.from_source(
        args.source or settings.default_source, max_iterations=args.max_iterations, settings=settings
    )
    if args.sql:
        show(t2s.run_sql(args.sql), args.rows)
        return 0
    if args.show_prompt:
        prepared = t2s.builder.prepare(args.question, parse_clarifications(args.clarify))
        if prepared.prompt:
            print(prepared.prompt.system, "\n", "-" * 60, "\n", prepared.prompt.user, "\n", "=" * 60, sep="")
    out = t2s.ask(args.question, parse_clarifications(args.clarify))
    show(out, args.rows)
    return 0 if out.status in ("success", "needs_clarification", "unanswerable") else 1


if __name__ == "__main__":
    sys.exit(main())
