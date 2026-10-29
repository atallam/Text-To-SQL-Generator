"""Build a golden eval set from a Hugging Face text-to-SQL dataset.

    python -m evals.build_golden --answerable 60 --unanswerable 20 --unsafe 20

Three kinds of cases are produced (see evals/schema.py):
  answerable    question + golden SQL whose result is verified by actually running it in SQLite
  unanswerable  a real question paired with a DIFFERENT domain's schema (no shared tables),
                so the only correct behaviour is to refuse
  unsafe        questions whose golden SQL is a write / DDL statement; must never execute

Sampling is seeded, so the same command always yields the same file.
"""
from __future__ import annotations

import argparse
import random
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from evals.compare import normalize_rows
from evals.hf_data import DATASET_ID, SUPPLY_CHAIN_DOMAINS, load_rows
from evals.sandbox import SandboxError, build_db, list_tables, run_select
from evals.schema import EvalCase

ANSWERABLE_TASKS = {"analytics and reporting", "data retrieval"}
UNSAFE_TASKS = {"data manipulation", "data definition"}

_READ_RE = re.compile(r"^\s*\(*\s*(select|with)\b", re.IGNORECASE)
_WRITE_RE = re.compile(
    r"^\s*(insert|update|delete|create|alter|drop|truncate|replace|merge)\b", re.IGNORECASE
)
_NONDETERMINISTIC_RE = re.compile(
    r"\b(random|now|current_date|current_time|current_timestamp|localtime)\b|date\s*\(\s*'now'", re.IGNORECASE
)
_ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)

# Share of answerable cases per complexity level (sums to 1.0).
COMPLEXITY_MIX: dict[str, float] = {
    "basic SQL": 0.20,
    "aggregation": 0.20,
    "single join": 0.20,
    "multiple_joins": 0.10,
    "subqueries": 0.10,
    "window functions": 0.10,
    "set operations": 0.05,
    "CTEs": 0.05,
}

MAX_GOLD_ROWS = 200


def _quotas(total: int, mix: dict[str, float]) -> dict[str, int]:
    """Largest-remainder split of ``total`` across the mix."""
    raw = {k: total * w for k, w in mix.items()}
    quotas = {k: int(v) for k, v in raw.items()}
    leftover = total - sum(quotas.values())
    for k in sorted(raw, key=lambda k: raw[k] - quotas[k], reverse=True)[:leftover]:
        quotas[k] += 1
    return quotas


def _validate_answerable(row: dict[str, Any]) -> tuple[list[list[Any]], set[str]] | None:
    """Run the golden SQL in SQLite. Returns (normalized rows, table names) or None if unusable."""
    try:
        conn = build_db(row["sql_context"])
    except SandboxError:
        return None
    try:
        res = run_select(conn, row["sql"], max_rows=MAX_GOLD_ROWS)
        tables = {t.lower() for t in list_tables(conn)}
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if res.truncated or not res.rows:
        return None
    if all(all(v is None for v in r) for r in res.rows):
        return None
    return normalize_rows(res.rows), tables


def _take_valid(rows: Iterable[dict[str, Any]], need: int, used: set[str]):
    """Yield (row, gold_rows, tables) for up to ``need`` rows whose golden SQL runs cleanly."""
    found = 0
    for row in rows:
        if found >= need:
            return
        rid = str(row["id"])
        if rid in used:
            continue
        verdict = _validate_answerable(row)
        if verdict is None:
            continue
        used.add(rid)
        found += 1
        yield row, verdict[0], verdict[1]


def _answerable_case(row: dict[str, Any], gold_rows: list[list[Any]]) -> EvalCase:
    return EvalCase(
        id=f"ans-{row['id']}",
        kind="answerable",
        question=row["sql_prompt"].strip(),
        context_sql=row["sql_context"],
        gold_sql=row["sql"].strip(),
        gold_rows=gold_rows,
        gold_ordered=bool(_ORDER_BY_RE.search(row["sql"])),
        domain=row["domain"],
        complexity=row["sql_complexity"],
        task_type=row["sql_task_type"],
    )


def build_cases(
    rows: list[dict[str, Any]],
    *,
    n_answerable: int = 60,
    n_unanswerable: int = 20,
    n_unsafe: int = 20,
    seed: int = 7,
    domains: Iterable[str] | None = SUPPLY_CHAIN_DOMAINS,
) -> list[EvalCase]:
    rng = random.Random(seed)
    domain_set = set(domains) if domains else None
    in_domain = [r for r in rows if domain_set is None or r["domain"] in domain_set]

    # ---- answerable: stratified by complexity, topped up from any level ------------------
    pool = [
        r
        for r in in_domain
        if r["sql_task_type"] in ANSWERABLE_TASKS
        and _READ_RE.match(r["sql"])
        and not _NONDETERMINISTIC_RE.search(r["sql"])
    ]
    rng.shuffle(pool)
    by_complexity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in pool:
        by_complexity[r["sql_complexity"]].append(r)

    used: set[str] = set()
    answerable: list[tuple[EvalCase, set[str]]] = []
    for complexity, quota in _quotas(n_answerable, COMPLEXITY_MIX).items():
        for row, gold, tables in _take_valid(by_complexity.get(complexity, []), quota, used):
            answerable.append((_answerable_case(row, gold), tables))
    if len(answerable) < n_answerable:
        for row, gold, tables in _take_valid(pool, n_answerable - len(answerable), used):
            answerable.append((_answerable_case(row, gold), tables))

    # ---- unanswerable: a real question against a schema that cannot answer it -------------
    unanswerable: list[EvalCase] = []
    donors = list(answerable)
    for row, _gold, _tables in _take_valid(pool, n_unanswerable * 3, used):
        if len(unanswerable) >= n_unanswerable:
            break
        own_tables = {t.lower() for t in re.findall(r"create\s+table\s+(\w+)", row["sql_context"], re.I)}
        rng.shuffle(donors)
        donor = next(
            (d for d, d_tables in donors if d.domain != row["domain"] and not (d_tables & own_tables)),
            None,
        ) or next((d for d, d_tables in donors if not (d_tables & own_tables)), None)
        if donor is None:
            continue
        unanswerable.append(
            EvalCase(
                id=f"unans-{row['id']}",
                kind="unanswerable",
                question=row["sql_prompt"].strip(),
                context_sql=donor.context_sql,
                domain=donor.domain,
                complexity=row["sql_complexity"],
                task_type=row["sql_task_type"],
                notes=f"schema swapped: question from {row['domain']}, schema from {donor.id}",
            )
        )

    # ---- unsafe: write / DDL requests (in-domain first, then any domain) ------------------
    def unsafe_pool(source: list[dict[str, Any]]) -> list[dict[str, Any]]:
        picked = [r for r in source if r["sql_task_type"] in UNSAFE_TASKS and _WRITE_RE.match(r["sql"])]
        rng.shuffle(picked)
        return picked

    in_ids = {str(r["id"]) for r in in_domain}
    candidates = unsafe_pool(in_domain) + unsafe_pool([r for r in rows if str(r["id"]) not in in_ids])
    unsafe: list[EvalCase] = []
    for row in candidates:
        if len(unsafe) >= n_unsafe:
            break
        try:
            build_db(row["sql_context"]).close()
        except SandboxError:
            continue
        unsafe.append(
            EvalCase(
                id=f"unsafe-{row['id']}",
                kind="unsafe",
                question=row["sql_prompt"].strip(),
                context_sql=row["sql_context"],
                gold_sql=row["sql"].strip(),
                domain=row["domain"],
                complexity=row["sql_complexity"],
                task_type=row["sql_task_type"],
            )
        )

    return [c for c, _ in answerable] + unanswerable + unsafe


def write_jsonl(cases: Iterable[EvalCase], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for case in cases:
            fh.write(case.model_dump_json() + "\n")
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build a golden eval set from a Hugging Face dataset.")
    p.add_argument("--dataset", default=DATASET_ID)
    p.add_argument("--split", default="test", help="Dataset split to sample from (default: test)")
    p.add_argument("--answerable", type=int, default=60)
    p.add_argument("--unanswerable", type=int, default=20)
    p.add_argument("--unsafe", type=int, default=20)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument(
        "--domains",
        default=",".join(SUPPLY_CHAIN_DOMAINS),
        help='Comma-separated dataset domains, or "all" (default: supply-chain-adjacent domains)',
    )
    p.add_argument("--out", type=Path, default=Path("evals/golden/hf_gretel_supply_chain.jsonl"))
    args = p.parse_args(argv)

    domains = None if args.domains.strip().lower() == "all" else [d.strip() for d in args.domains.split(",")]
    print(f"Loading {args.dataset} [{args.split}] ...")
    rows = load_rows(args.dataset, args.split)
    cases = build_cases(
        rows,
        n_answerable=args.answerable,
        n_unanswerable=args.unanswerable,
        n_unsafe=args.unsafe,
        seed=args.seed,
        domains=domains,
    )
    n = write_jsonl(cases, args.out)

    counts = defaultdict(int)
    for c in cases:
        counts[c.kind] += 1
    print(f"Wrote {n} cases to {args.out}")
    for kind in ("answerable", "unanswerable", "unsafe"):
        print(f"  {kind:<13} {counts[kind]}")
    short = {"answerable": args.answerable, "unanswerable": args.unanswerable, "unsafe": args.unsafe}
    for kind, want in short.items():
        if counts[kind] < want:
            print(f"  note: only {counts[kind]} {kind} cases available (asked for {want}); widen --domains")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
