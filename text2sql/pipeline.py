"""End-to-end pipeline (Phases 1-3): question -> context -> generate/repair -> guardrails -> sandbox
-> validation and confidence.

    t2s = Text2SQL.from_source("wealthlens")
    out = t2s.ask("How much did I spend on groceries last month?")
    if out.status == "success": out.execution.to_dataframe()
    if out.status == "needs_clarification": ...ask the user, then t2s.ask(q, clarifications={...})

``run_sql`` is the same safety path for SQL a person typed or edited (no LLM involved).
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from text2sql.config.settings import Settings, get_settings
from text2sql.config.sources import SourceConfig, load_source
from text2sql.connectors.base import Connector
from text2sql.connectors.registry import get_connector
from text2sql.execution.executor import ExecutionError, ExecutionResult, Executor
from text2sql.generation.ambiguity import ClarificationRequest
from text2sql.generation.context import ContextBuilder
from text2sql.generation.generator import GeneratedSQL, SQLGenerator
from text2sql.generation.repair_loop import Attempt, RepairLoop
from text2sql.guardrails.config import load_guardrail_config
from text2sql.guardrails.engine import GuardrailEngine
from text2sql.guardrails.explain_gate import ExplainGate
from text2sql.llm.client import LLMClient
from text2sql.observability.logging import get_logger
from text2sql.validation import ValidationReport, Validator, load_validation_config

log = get_logger("pipeline")

Status = Literal["success", "needs_clarification", "unanswerable", "blocked", "failed"]


class GuardrailSummary(BaseModel):
    allowed: bool
    rule_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    row_limit_applied: int | None = None
    plan_cost: float | None = None
    plan_rows: float | None = None


class QueryOutcome(BaseModel):
    """Everything the API/UI/eval needs, and the audit record (``audit_id``) for the whole request."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    audit_id: str
    status: Status
    question: str | None = None
    message: str = ""
    sql: str | None = None  # the guardrail-approved SQL that ran (or the last one that was refused)
    generated: GeneratedSQL | None = None
    execution: ExecutionResult | None = None
    guardrail: GuardrailSummary | None = None
    clarification: ClarificationRequest | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    prompt_version: str | None = None
    validation: ValidationReport | None = None  # Phase 3: confidence score + the evidence behind it
    timings_ms: dict[str, int] = Field(default_factory=dict)

    @property
    def confidence(self) -> float | None:
        return self.validation.confidence.score if self.validation else None


def _summary(decision: Any, explain: Any) -> GuardrailSummary | None:
    if decision is None:
        return None
    ids = list(decision.rule_ids)
    reasons = [str(v) for v in decision.violations]
    if explain is not None and explain.violations:
        ids = sorted({*ids, *(v.rule_id for v in explain.violations)})
        reasons += [str(v) for v in explain.violations]
    return GuardrailSummary(
        allowed=decision.allowed and (explain is None or explain.ok),
        rule_ids=ids,
        reasons=reasons,
        warnings=list(decision.warnings),
        tables=decision.tables,
        row_limit_applied=decision.row_limit_applied,
        plan_cost=getattr(explain, "total_cost", None),
        plan_rows=getattr(explain, "plan_rows", None),
    )


class Text2SQL:
    def __init__(
        self,
        *,
        builder: ContextBuilder,
        generator: SQLGenerator,
        engine: GuardrailEngine,
        gate: ExplainGate,
        executor: Executor,
        max_iterations: int = 3,
        validator: Validator | None = None,
    ):
        self.builder, self.engine, self.gate, self.executor = builder, engine, gate, executor
        self.validator = validator  # None = no confidence scoring (Phase 2 behaviour)
        self.loop = RepairLoop(
            generator=generator,
            engine=engine,
            gate=gate,
            executor=executor,
            schema=builder.schema,
            max_iterations=max_iterations,
        )

    @classmethod
    def from_source(
        cls,
        source: str | SourceConfig,
        *,
        llm: LLMClient | None = None,
        connector: Connector | None = None,
        settings: Settings | None = None,
        max_iterations: int = 3,
        validate: bool = True,
        **builder_kwargs: Any,
    ) -> Text2SQL:
        settings = settings or get_settings()
        cfg = load_source(source, settings) if isinstance(source, str) else source
        connector = connector or get_connector(cfg)
        guard_cfg = load_guardrail_config(cfg)
        builder = ContextBuilder.from_source(cfg, connector=connector, settings=settings, **builder_kwargs)
        llm = llm or LLMClient(settings)
        engine = GuardrailEngine(guard_cfg)
        gate = ExplainGate(connector, guard_cfg.explain, guard_cfg.statement_timeout_ms)
        executor = Executor(connector, guard_cfg.row_limit.max_rows, guard_cfg.statement_timeout_ms)
        validator = (
            Validator.build(
                llm=llm,
                engine=engine,
                gate=gate,
                executor=executor,
                cfg=load_validation_config(cfg),
                settings=settings,
            )
            if validate
            else None
        )
        return cls(
            builder=builder,
            generator=SQLGenerator(llm),
            engine=engine,
            gate=gate,
            executor=executor,
            max_iterations=max_iterations,
            validator=validator,
        )

    # -- natural language ---------------------------------------------------------------------
    def ask(self, question: str, clarifications: dict[str, int | str] | None = None) -> QueryOutcome:
        audit_id, t0 = uuid.uuid4().hex[:12], time.perf_counter()
        prepared = self.builder.prepare(question, clarifications)
        if prepared.status == "needs_clarification":
            return self._finish(
                QueryOutcome(
                    audit_id=audit_id,
                    status="needs_clarification",
                    question=question,
                    clarification=prepared.clarification,
                    message="The question needs clarification.",
                ),
                t0,
                prepared.timings_ms,
            )
        prompt = prepared.prompt
        res = self.loop.run(prompt)
        out = QueryOutcome(
            audit_id=audit_id,
            status=res.status,
            question=question,
            message=res.message,
            sql=res.decision.sql
            if res.decision is not None and res.decision.allowed
            else res.generated.sql
            if res.generated
            else None,
            generated=res.generated,
            execution=res.execution,
            guardrail=_summary(res.decision, res.explain),
            attempts=res.attempts,
            prompt_version=prompt.version,
        )
        timings = dict(prepared.timings_ms)
        if self.validator is not None and res.status in ("success", "unanswerable"):
            v0 = time.perf_counter()
            try:
                out.validation = self.validator.validate(question, prompt, res, self.builder.schema.get())
            except Exception as exc:  # noqa: BLE001  the answer stands even if scoring fails
                log.error("validation_error", audit_id=audit_id, error=repr(exc)[:300])
            timings["validation"] = int((time.perf_counter() - v0) * 1000)
        return self._finish(out, t0, timings)

    # -- user-supplied SQL (same safety path, no LLM) -----------------------------------------
    def run_sql(self, sql: str) -> QueryOutcome:
        audit_id, t0 = uuid.uuid4().hex[:12], time.perf_counter()
        decision = self.engine.check(sql, self.builder.schema.get())
        explain = None
        if decision.allowed:
            explain = self.gate.check(decision.sql)
        out = QueryOutcome(
            audit_id=audit_id,
            status="blocked",
            sql=decision.sql or sql,
            guardrail=_summary(decision, explain),
        )
        if not decision.allowed:
            out.message = decision.reason
        elif not explain.ok:
            out.message = explain.error or "; ".join(v.message for v in explain.violations or [])
            out.status = "failed" if explain.error else "blocked"
        else:
            try:
                out.execution, out.status = self.executor.run(decision.sql), "success"
            except ExecutionError as exc:
                out.status, out.message = (
                    ("blocked" if exc.kind in ("permission", "read_only") else "failed"),
                    exc.message,
                )
        return self._finish(out, t0, {})

    # -----------------------------------------------------------------------------------------
    def _finish(self, out: QueryOutcome, t0: float, timings: dict[str, int]) -> QueryOutcome:
        out.timings_ms = {**timings, "request": int((time.perf_counter() - t0) * 1000)}
        log.info(
            "audit",
            audit_id=out.audit_id,
            status=out.status,
            question_hash=hashlib.sha256((out.question or "").encode()).hexdigest()[:12],
            attempts=len(out.attempts),
            rule_ids=out.guardrail.rule_ids if out.guardrail else [],
            result_hash=out.execution.result_hash if out.execution else None,
            rows=out.execution.row_count if out.execution else None,
            confidence=out.confidence,
            confidence_level=out.validation.confidence.level if out.validation else None,
            ms=out.timings_ms.get("request"),
        )
        return out
