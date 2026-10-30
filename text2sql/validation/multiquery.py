"""Multi-query validation: write the answer twice, independently, and see whether the data agrees.

For questions above a complexity threshold a second model call writes a *structurally different* SQL
(different join path, CTE vs subquery, conditional aggregation vs join...). Both are executed and compared
after canonicalisation:

* column order and aliases do not matter (columns are matched by their values),
* row order does not matter,
* numbers are compared with a tolerance (rounding differences are not disagreement),
* a result with extra columns still agrees if every column of the narrower one is present in the other.

The second query takes the same safe path as the first (guardrails -> EXPLAIN gate -> read-only executor).
If it cannot be produced or run, the signal is *unavailable* rather than a disagreement, and the other
signals carry the score.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlglot import exp

from text2sql.execution.executor import ExecutionError, ExecutionResult, Executor
from text2sql.generation.context import PromptBundle
from text2sql.generation.generator import GeneratedSQL
from text2sql.guardrails import parser
from text2sql.guardrails.engine import GuardrailEngine
from text2sql.guardrails.explain_gate import ExplainGate
from text2sql.llm.client import LLMClient
from text2sql.observability.logging import get_logger
from text2sql.prompts import LATEST_VERSIONS, SQL_ALTERNATIVE, render
from text2sql.schema.model import SchemaSnapshot
from text2sql.validation.config import MultiQueryConfig

log = get_logger("multiquery")

Kind = Literal["exact", "extra_columns", "row_mismatch", "shape_mismatch", "column_mismatch"]


def complexity(sql: str, dialect: str = "postgres") -> int:
    """Joins + subqueries/CTEs + window functions + GROUP BY + HAVING. Simple lookups score 0-2."""
    tree = parser.parse(sql, dialect).tree
    if tree is None:
        return 0
    score = sum(1 for _ in tree.find_all(exp.Join))
    score += sum(1 for _ in tree.find_all(exp.CTE))
    score += sum(1 for s in tree.find_all(exp.Subquery))
    score += sum(1 for _ in tree.find_all(exp.Window))
    score += 1 if tree.find(exp.Group) is not None else 0
    score += 1 if tree.find(exp.Having) is not None else 0
    return score


# -- canonicalisation ----------------------------------------------------------------------------------
def _norm(v: Any) -> Any:
    """Make values from different SQL formulations comparable: Decimal/int/float -> float, dates -> ISO
    text, strings stripped."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time(0) else v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v).strip()


def _sort_key(v: Any) -> tuple:
    if v is None:
        return (0, 0.0, "")
    if isinstance(v, bool):
        return (1, float(v), "")
    if isinstance(v, float):
        return (2, round(v, 2), "")  # coarse so near-equal numbers sort together
    return (3, 0.0, str(v))


def _close(a: Any, b: Any, abs_tol: float, rel_tol: float) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))
    return a == b


class Comparison(BaseModel):
    agree: bool
    kind: Kind
    detail: str = ""
    row_overlap: float | None = None  # share of rows (multiset) found in both, when shapes are comparable


def compare_results(a: ExecutionResult, b: ExecutionResult, cfg: MultiQueryConfig) -> Comparison:
    ra = [[_norm(v) for v in row] for row in a.rows[: cfg.compare_rows]]
    rb = [[_norm(v) for v in row] for row in b.rows[: cfg.compare_rows]]
    if a.truncated != b.truncated or (a.row_count != b.row_count):
        return Comparison(
            agree=False,
            kind="shape_mismatch",
            detail=f"approach A returned {a.row_count} rows, approach B {b.row_count}.",
            row_overlap=_overlap(ra, rb, cfg) if len(a.columns) == len(b.columns) else None,
        )

    cols_a = _columns(ra, len(a.columns))
    cols_b = _columns(rb, len(b.columns))
    # Match each column of the narrower result to a distinct column of the wider one by value.
    small, big, a_is_small = (cols_a, cols_b, True) if len(cols_a) <= len(cols_b) else (cols_b, cols_a, False)
    mapping: list[int] = []
    used: set[int] = set()
    for sc in small:
        hit = next(
            (j for j, bc in enumerate(big) if j not in used and _col_equal(sc, bc, cfg)),
            None,
        )
        if hit is None:
            return Comparison(
                agree=False,
                kind="column_mismatch",
                detail="the two results do not contain the same values in any matching column layout.",
                row_overlap=_overlap(ra, rb, cfg) if len(cols_a) == len(cols_b) else None,
            )
        used.add(hit)
        mapping.append(hit)

    # Columns line up; now require the *rows* to line up too (not just each column's multiset).
    small_rows = ra if a_is_small else rb
    big_rows = rb if a_is_small else ra
    projected = [[row[j] for j in mapping] for row in big_rows]
    if not _rows_equal(small_rows, projected, cfg):
        return Comparison(
            agree=False,
            kind="row_mismatch",
            detail="columns hold the same values but are paired into different rows.",
            row_overlap=_overlap(small_rows, projected, cfg),
        )
    extra = len(cols_a) != len(cols_b)
    return Comparison(
        agree=True,
        kind="extra_columns" if extra else "exact",
        detail="one result has additional columns; the shared columns match." if extra else "",
        row_overlap=1.0,
    )


def _columns(rows: list[list[Any]], n: int) -> list[list[Any]]:
    return [sorted((r[i] for r in rows), key=_sort_key) for i in range(n)]


def _col_equal(x: list[Any], y: list[Any], cfg: MultiQueryConfig) -> bool:
    return len(x) == len(y) and all(
        _close(p, q, cfg.abs_tol, cfg.rel_tol) for p, q in zip(x, y, strict=False)
    )


def _row_key(row: list[Any]) -> tuple:
    return tuple(_sort_key(v) for v in row)


def _rows_equal(x: list[list[Any]], y: list[list[Any]], cfg: MultiQueryConfig) -> bool:
    if len(x) != len(y):
        return False
    sx, sy = sorted(x, key=_row_key), sorted(y, key=_row_key)
    return all(
        len(r1) == len(r2)
        and all(_close(p, q, cfg.abs_tol, cfg.rel_tol) for p, q in zip(r1, r2, strict=False))
        for r1, r2 in zip(sx, sy, strict=False)
    )


def _overlap(x: list[list[Any]], y: list[list[Any]], cfg: MultiQueryConfig) -> float:
    """Fraction of rows of the larger result that have an (approximately) equal row in the other."""
    if not x and not y:
        return 1.0
    pool = [list(r) for r in y]
    hits = 0
    for row in x:
        for k, cand in enumerate(pool):
            if len(cand) == len(row) and all(
                _close(p, q, cfg.abs_tol, cfg.rel_tol) for p, q in zip(row, cand, strict=False)
            ):
                hits += 1
                del pool[k]
                break
    return round(hits / max(len(x), len(y)), 4)


# -- the second approach -------------------------------------------------------------------------------------
class AlternativeRun(BaseModel):
    """What happened when approach B was attempted."""

    status: Literal["agree", "disagree", "unavailable"]
    reason: str = ""  # why unavailable
    sql: str | None = None
    explanation: str | None = None
    comparison: Comparison | None = None
    columns: list[str] = Field(default_factory=list)
    preview_rows: list[list[Any]] = Field(default_factory=list)  # first rows of B, for showing both
    row_count: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class MultiQueryValidator:
    def __init__(
        self,
        *,
        llm: LLMClient,
        engine: GuardrailEngine,
        gate: ExplainGate,
        executor: Executor,
        cfg: MultiQueryConfig,
        model: str | None = None,
        preview_rows: int = 10,
    ):
        self.llm, self.engine, self.gate, self.executor = llm, engine, gate, executor
        self.cfg, self.model, self.preview_rows = cfg, model, preview_rows

    def applies(self, sql: str) -> bool:
        return (
            self.cfg.enabled and complexity(sql, self.engine.config.dialect) >= self.cfg.complexity_threshold
        )

    def run(
        self, prompt: PromptBundle, primary_sql: str, primary: ExecutionResult, schema: SchemaSnapshot
    ) -> AlternativeRun:
        try:
            res = self.llm.complete_structured(
                agent_name="sql_alternative",
                system=prompt.system,
                user=render(
                    SQL_ALTERNATIVE,
                    LATEST_VERSIONS[SQL_ALTERNATIVE],
                    "user",
                    original_prompt=prompt.user.rstrip(),
                    primary_sql=primary_sql,
                ),
                response_model=GeneratedSQL,
                model=self.model,
            )
        except Exception as exc:  # noqa: BLE001  never let validation break an answer
            log.warning("alternative_generation_failed", error=repr(exc)[:200])
            return AlternativeRun(
                status="unavailable", reason=f"second query could not be generated: {exc}"[:300]
            )
        alt = res.parsed
        run = AlternativeRun(
            status="unavailable",
            sql=alt.sql or None,
            explanation=alt.explanation,
            input_tokens=res.usage.input_tokens,
            output_tokens=res.usage.output_tokens,
        )
        if alt.unanswerable or not alt.sql.strip():
            run.reason = "the second approach declared the question unanswerable"
            return run
        decision = self.engine.check(alt.sql, schema)
        if not decision.allowed:
            run.reason = f"second query was refused by the guardrails: {decision.reason}"[:300]
            return run
        plan = self.gate.check(decision.sql)
        if not plan.ok:
            run.reason = f"second query failed the plan check: {plan.error or 'cost gate'}"[:300]
            return run
        run.sql = decision.sql
        try:
            other = self.executor.run(decision.sql)
        except ExecutionError as exc:
            run.reason = f"second query failed to run: {exc.message}"[:300]
            return run
        run.columns, run.row_count = other.columns, other.row_count
        run.preview_rows = [list(r) for r in other.rows[: self.preview_rows]]
        run.comparison = compare_results(primary, other, self.cfg)
        run.status = "agree" if run.comparison.agree else "disagree"
        return run
