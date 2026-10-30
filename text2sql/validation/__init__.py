"""Phase 3: is the answer right? Hallucination detection and a confidence score.

    validator = Validator.build(llm, engine, gate, executor, cfg)
    report = validator.validate(question, prompt, loop_result, schema)
    report.confidence.score, report.confidence.level, report.confidence.signals

Every signal is optional: a failure inside one never breaks the answer, it only removes that signal.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field
from sqlglot import exp

from text2sql.config.settings import Settings, get_settings
from text2sql.execution.executor import Executor
from text2sql.generation.context import PromptBundle
from text2sql.generation.repair_loop import LoopResult
from text2sql.guardrails import parser
from text2sql.guardrails.engine import GuardrailEngine
from text2sql.guardrails.explain_gate import ExplainGate
from text2sql.llm.client import LLMClient
from text2sql.observability.logging import get_logger
from text2sql.schema.model import SchemaSnapshot
from text2sql.validation.backtranslate import BackTranslation, BackTranslator
from text2sql.validation.confidence import (
    ConfidenceReport,
    ConfidenceScorer,
    schema_coverage_signal,
    syntax_signal,
)
from text2sql.validation.config import ValidationConfig, load_validation_config
from text2sql.validation.multiquery import AlternativeRun, MultiQueryValidator
from text2sql.validation.sanity import SanityChecker, SanityReport, TableStats

log = get_logger("validation")

__all__ = ["ValidationReport", "Validator", "ValidationConfig", "load_validation_config", "ConfidenceReport"]


class ValidationReport(BaseModel):
    confidence: ConfidenceReport
    back_translation: BackTranslation | None = None
    sanity: SanityReport | None = None
    alternative: AlternativeRun | None = None  # approach B; show next to the answer when it disagrees
    errors: list[str] = Field(default_factory=list)  # signals that could not be measured, and why
    timings_ms: dict[str, int] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0


class Validator:
    def __init__(
        self,
        *,
        cfg: ValidationConfig,
        backtranslator: BackTranslator,
        multiquery: MultiQueryValidator,
        executor: Executor,
        dialect: str = "postgres",
    ):
        self.cfg, self.backtranslator, self.multiquery = cfg, backtranslator, multiquery
        self.executor, self.dialect = executor, dialect
        self.scorer = ConfidenceScorer(cfg)
        self._stats: dict[str, TableStats] = {}

    @classmethod
    def build(
        cls,
        *,
        llm: LLMClient,
        engine: GuardrailEngine,
        gate: ExplainGate,
        executor: Executor,
        cfg: ValidationConfig | None = None,
        settings: Settings | None = None,
    ) -> Validator:
        cfg = cfg or load_validation_config()
        settings = settings or get_settings()
        judge_model = settings.llm_model_judge
        return cls(
            cfg=cfg,
            backtranslator=BackTranslator(llm, cfg.backtranslation, judge_model),
            # the second SQL is a generation task, so it uses the generation model (model=None -> default)
            multiquery=MultiQueryValidator(
                llm=llm, engine=engine, gate=gate, executor=executor, cfg=cfg.multiquery
            ),
            executor=executor,
            dialect=engine.config.dialect,
        )

    def _table_stats(self, schema: SchemaSnapshot) -> TableStats:
        stats = self._stats.get(schema.fingerprint)
        if stats is None:
            self._stats = {schema.fingerprint: TableStats(self.executor, schema)}
            stats = self._stats[schema.fingerprint]
        return stats

    # ------------------------------------------------------------------------------------------------
    def cannot_answer(self, reason: str) -> ValidationReport:
        return ValidationReport(confidence=self.scorer.cannot_answer(reason))

    def validate(
        self, question: str, prompt: PromptBundle, result: LoopResult, schema: SchemaSnapshot
    ) -> ValidationReport:
        if result.status == "unanswerable":
            return self.cannot_answer(result.message)
        t0 = time.perf_counter()
        decision, execution, generated = result.decision, result.execution, result.generated
        if result.status != "success" or decision is None or execution is None or generated is None:
            raise ValueError("validate() needs a successful result")
        sql = decision.sql
        errors: list[str] = []
        timings: dict[str, int] = {}

        def timed(name: str, fn):
            start = time.perf_counter()
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001  a failed signal is a missing signal, never a failed answer
                log.warning("validation_signal_failed", signal=name, error=repr(exc)[:300])
                errors.append(f"{name}: {exc}"[:300])
                return None
            finally:
                timings[name] = int((time.perf_counter() - start) * 1000)

        # The two LLM-backed signals are independent of each other: run them side by side.
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_bt = pool.submit(
                timed, "backtranslation", lambda: self.backtranslator.check(question, sql, schema)
            )
            f_mq = (
                pool.submit(timed, "multiquery", lambda: self.multiquery.run(prompt, sql, execution, schema))
                if self.multiquery.applies(sql)
                else None
            )
            sanity: SanityReport | None = timed(
                "sanity",
                lambda: SanityChecker(self.cfg.sanity, schema, self._table_stats(schema)).run(
                    sql, execution, self.dialect
                ),
            )
            bt: BackTranslation | None = f_bt.result()
            alt: AlternativeRun | None = f_mq.result() if f_mq else None

        signals: dict[str, tuple[float | None, str]] = {}
        if bt is not None:
            detail = "; ".join(bt.issues) or "the description of the SQL matches the question"
            signals["backtranslation"] = (bt.alignment, detail)
        if alt is not None and alt.status != "unavailable":
            cmp_ = alt.comparison
            if alt.status == "agree":
                signals["multiquery"] = (1.0, "an independent second query returned the same data")
            else:
                why = f"{cmp_.kind}: {cmp_.detail}" if cmp_ else "different data"
                signals["multiquery"] = (0.0, f"an independent second query disagreed ({why})")
        elif alt is not None:
            errors.append(f"multiquery unavailable: {alt.reason}")
        if sanity is not None:
            flagged = "; ".join(f"{c.name}: {c.detail}" for c in sanity.flagged)
            signals["sanity"] = (sanity.pass_rate, flagged or "all applicable checks passed")
        tree = parser.parse(sql, self.dialect).tree
        columns = [c.name for c in tree.find_all(exp.Column)] if tree else []
        signals["schema_coverage"] = schema_coverage_signal(
            sql_tables=decision.tables,
            sql_columns=columns,
            context_tables=prompt.tables,
            generated=generated,
        )
        signals["syntax"] = syntax_signal(len(result.attempts))

        confidence = self.scorer.score(
            signals,
            disagreement=alt is not None and alt.status == "disagree",
            misaligned=bt is not None and bt.alignment < self.cfg.backtranslation.flag_below,
            warnings=list(decision.warnings),
            truncated=execution.truncated,
        )
        timings["total"] = int((time.perf_counter() - t0) * 1000)
        in_tok = (bt.input_tokens if bt else 0) + (alt.input_tokens if alt else 0)
        out_tok = (bt.output_tokens if bt else 0) + (alt.output_tokens if alt else 0)
        return ValidationReport(
            confidence=confidence,
            back_translation=bt,
            sanity=sanity,
            alternative=alt,
            errors=errors,
            timings_ms=timings,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
