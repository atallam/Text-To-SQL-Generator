"""Public API contract. Built from the pipeline's QueryOutcome; never exposes connection strings,
prompts or raw LLM payloads."""

from __future__ import annotations

import datetime as dt
import decimal
import math
from typing import Any, Literal

from pydantic import BaseModel, Field

from text2sql.generation.context import MAX_QUESTION_CHARS
from text2sql.pipeline import QueryOutcome

SESSION_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"
MAX_SQL_CHARS = 20_000

Status = Literal["success", "needs_clarification", "unanswerable", "blocked", "failed"]

# Plain-language explanations for guardrail rule ids (stable ids, see text2sql/guardrails/rules.py).
RULE_HELP: dict[str, tuple[str, str]] = {
    "G000": ("Could not read the SQL", "The query is not valid SQL, so it was not run."),
    "G001": ("More than one statement", "Only one query can run at a time. Chained statements are refused."),
    "G002": (
        "Not a read-only query",
        "Only SELECT queries are allowed. Anything that creates, changes or deletes data is blocked.",
    ),
    "G003": ("Risky construct", "The query uses something that could change data or reach outside the data."),
    "G004": ("Blocked function", "The query calls a database function that is not on the safe list."),
    "G005": (
        "Table not allowed",
        "The query touches a table or schema this connection is not allowed to read.",
    ),
    "G006": ("Row limit", "A row limit was added so a huge result cannot be pulled by accident."),
    "G007": ("Too deeply nested", "The query nests subqueries too deeply to review safely."),
    "G008": (
        "Too expensive",
        "The database estimates this query would scan too much data. Narrow the filters.",
    ),
}


def _safe(value: Any) -> Any:
    """JSON-safe cell: Decimal -> float, dates -> ISO strings, NaN/inf -> None, anything else -> str."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, decimal.Decimal):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    return str(value)


# ---- requests ---------------------------------------------------------------------------------
class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    source: str | None = Field(default=None, description="Data source name; default source if omitted.")
    clarifications: dict[str, int | str] = Field(
        default_factory=dict, description="Answers to a clarification card: {term: option index or label}."
    )
    session_id: str | None = Field(default=None, pattern=SESSION_PATTERN)


class RunSqlRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=MAX_SQL_CHARS)
    source: str | None = None
    session_id: str | None = Field(default=None, pattern=SESSION_PATTERN)


class FeedbackRequest(BaseModel):
    audit_id: str = Field(min_length=6, max_length=32)
    verdict: Literal["correct", "incorrect"]
    comment: str | None = Field(default=None, max_length=1000)
    corrected_sql: str | None = Field(default=None, max_length=MAX_SQL_CHARS)
    session_id: str | None = Field(default=None, pattern=SESSION_PATTERN)


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]


# ---- response parts -----------------------------------------------------------------------------
class SignalOut(BaseModel):
    name: str
    value: float | None
    weight: float
    detail: str = ""
    measured: bool


class ConfidenceOut(BaseModel):
    score: float
    level: Literal["high", "medium", "low", "cannot_answer"]
    headline: str
    calibrated: bool
    signals_used: int
    signals_total: int
    signals: list[SignalOut]
    caps_applied: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class RuleExplanation(BaseModel):
    rule_id: str
    title: str
    plain: str


class GuardrailOut(BaseModel):
    allowed: bool
    rule_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    explanations: list[RuleExplanation] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    row_limit_applied: int | None = None
    plan_cost: float | None = None
    plan_rows: float | None = None


class ClarificationOptionOut(BaseModel):
    index: int
    label: str
    example_sql: str


class AmbiguousTermOut(BaseModel):
    term: str
    matched_text: str
    definition: str | None = None
    options: list[ClarificationOptionOut]


class ClarificationOut(BaseModel):
    question: str
    message: str
    terms: list[AmbiguousTermOut]


class AlternativeOut(BaseModel):
    status: Literal["agree", "disagree", "unavailable"]
    reason: str = ""
    detail: str = ""
    sql: str | None = None
    explanation: str | None = None
    columns: list[str] = Field(default_factory=list)
    preview_rows: list[list[Any]] = Field(default_factory=list)
    row_count: int | None = None


class BackTranslationOut(BaseModel):
    summary: str
    alignment: float
    issues: list[str] = Field(default_factory=list)


class SanityOut(BaseModel):
    name: str
    status: str
    detail: str = ""


class AttemptOut(BaseModel):
    n: int
    ok: bool
    stage: str
    error: str | None = None
    rule_ids: list[str] = Field(default_factory=list)


class QueryResponse(BaseModel):
    audit_id: str
    source: str
    session_id: str
    kind: Literal["ask", "run_sql"]
    status: Status
    question: str | None = None
    message: str = ""
    sql: str | None = None
    explanation: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int | None = None
    result_hash: str | None = None
    confidence: ConfidenceOut | None = None
    confidence_note: str | None = None  # why there is no score (e.g. user-edited SQL)
    guardrail: GuardrailOut | None = None
    clarification: ClarificationOut | None = None
    alternative: AlternativeOut | None = None
    back_translation: BackTranslationOut | None = None
    sanity: list[SanityOut] = Field(default_factory=list)
    attempts: list[AttemptOut] = Field(default_factory=list)
    timings_ms: dict[str, int] = Field(default_factory=dict)


class HistoryItem(BaseModel):
    audit_id: str
    source: str
    kind: str
    question: str | None
    sql: str | None
    status: str
    confidence: float | None
    level: str | None
    created_at: float


class SourceInfo(BaseModel):
    name: str
    display_name: str
    dialect: str
    default: bool
    examples: list[str] = Field(default_factory=list)


class ColumnOut(BaseModel):
    name: str
    type: str
    nullable: bool
    primary_key: bool
    comment: str | None = None
    allowed_values: list[str] | None = None


class ForeignKeyOut(BaseModel):
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


class TableOut(BaseModel):
    name: str
    comment: str | None = None
    columns: list[ColumnOut]
    foreign_keys: list[ForeignKeyOut] = Field(default_factory=list)


class SchemaResponse(BaseModel):
    source: str
    dialect: str
    fingerprint: str
    tables: list[TableOut]


class FeedbackResponse(BaseModel):
    id: int
    candidate: Literal["few_shot", "eval_case"]
    review_status: str = "pending"
    message: str


# ---- conversion ---------------------------------------------------------------------------------
def _confidence(report: Any) -> ConfidenceOut:
    c = report.confidence
    signals = [
        SignalOut(name=s.name, value=s.value, weight=s.weight, detail=s.detail, measured=s.available)
        for s in c.signals
    ]
    if c.level == "cannot_answer":
        headline = "The data cannot answer this question."
    else:
        headline = {
            "high": "High confidence",
            "medium": "Medium confidence: worth a quick check",
            "low": "Low confidence: verify before relying on it",
        }[c.level]
    return ConfidenceOut(
        score=round(c.score, 3),
        level=c.level,
        headline=headline,
        calibrated=c.calibrated,
        signals_used=c.signals_used,
        signals_total=len(signals),
        signals=signals,
        caps_applied=list(c.caps_applied),
        reasons=list(c.reasons),
    )


def _guardrail(g: Any) -> GuardrailOut:
    expl = [
        RuleExplanation(rule_id=r, title=RULE_HELP[r][0], plain=RULE_HELP[r][1])
        for r in g.rule_ids
        if r in RULE_HELP
    ]
    return GuardrailOut(
        allowed=g.allowed,
        rule_ids=g.rule_ids,
        reasons=g.reasons,
        warnings=g.warnings,
        explanations=expl,
        tables=g.tables,
        row_limit_applied=g.row_limit_applied,
        plan_cost=g.plan_cost,
        plan_rows=g.plan_rows,
    )


def to_response(
    out: QueryOutcome, *, source: str, session_id: str, kind: Literal["ask", "run_sql"]
) -> QueryResponse:
    resp = QueryResponse(
        audit_id=out.audit_id,
        source=source,
        session_id=session_id,
        kind=kind,
        status=out.status,
        question=out.question,
        message=out.message,
        sql=out.sql,
        explanation=out.generated.explanation if out.generated and out.generated.explanation else None,
        timings_ms=out.timings_ms,
        attempts=[
            AttemptOut(n=a.n, ok=a.ok, stage=str(a.stage), error=a.error, rule_ids=a.rule_ids)
            for a in out.attempts
        ],
    )
    if out.execution is not None:
        e = out.execution
        resp.columns, resp.row_count, resp.truncated = e.columns, e.row_count, e.truncated
        resp.rows = [[_safe(v) for v in row] for row in e.rows]
        resp.elapsed_ms, resp.result_hash = e.elapsed_ms, e.result_hash
    if out.guardrail is not None:
        resp.guardrail = _guardrail(out.guardrail)
    if out.clarification is not None:
        cl = out.clarification
        resp.clarification = ClarificationOut(
            question=cl.question,
            message=cl.message or cl.render_message(),
            terms=[
                AmbiguousTermOut(
                    term=t.term,
                    matched_text=t.matched_text,
                    definition=t.definition,
                    options=[
                        ClarificationOptionOut(index=o.index, label=o.label, example_sql=o.example_sql)
                        for o in t.options
                    ],
                )
                for t in cl.terms
            ],
        )
    v = out.validation
    if v is not None:
        resp.confidence = _confidence(v)
        if v.alternative is not None:
            a = v.alternative
            resp.alternative = AlternativeOut(
                status=a.status,
                reason=a.reason,
                detail=a.comparison.detail if a.comparison else "",
                sql=a.sql,
                explanation=a.explanation,
                columns=a.columns,
                preview_rows=[[_safe(x) for x in r] for r in a.preview_rows],
                row_count=a.row_count,
            )
        if v.back_translation is not None:
            bt = v.back_translation
            resp.back_translation = BackTranslationOut(
                summary=bt.description.summary,
                alignment=round(bt.alignment, 3),
                issues=list(bt.verdict.issues),
            )
        if v.sanity is not None:
            resp.sanity = [SanityOut(name=c.name, status=c.status, detail=c.detail) for c in v.sanity.checks]
    elif kind == "run_sql":
        resp.confidence_note = (
            "This is SQL you wrote or edited, so it is not scored. It still passed the same safety checks."
        )
    elif out.status in ("failed", "blocked"):
        resp.confidence_note = "No answer was produced, so there is nothing to score."
    elif out.status == "needs_clarification":
        resp.confidence_note = "Pick a meaning first; the answer will be scored."
    else:
        resp.confidence_note = "Confidence scoring was not available for this answer. Treat it with care."
    return resp
