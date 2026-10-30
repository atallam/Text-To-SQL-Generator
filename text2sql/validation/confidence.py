"""Confidence scorer: combine independent evidence into one 0-1 number a person can act on.

The model's own ``self_confidence`` is NOT an input: self-reported confidence is poorly calibrated. The
five signals are measured from outside the generation step.

    score = sum(weight_i * value_i) / sum(weight_i)      over the signals that are available
    then hard caps:  approaches disagree -> <= 0.5     guardrail warning -> <= 0.6
    "cannot answer" short-circuits: score 0, no other signal is consulted

A signal that could not be measured (multi-query skipped for a simple question, judge call failed) drops
out and the remaining weights are renormalised, so a missing signal never counts as a pass or a fail. The
report says which signals were used so the UI can show "based on 3 of 5 checks".
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from text2sql.generation.generator import GeneratedSQL
from text2sql.validation.config import ValidationConfig

Level = Literal["high", "medium", "low", "cannot_answer"]


class Signal(BaseModel):
    name: str
    value: float | None  # 0..1, or None when not measured
    weight: float  # configured weight (before renormalisation)
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None


class ConfidenceReport(BaseModel):
    score: float
    level: Level
    signals: list[Signal] = Field(default_factory=list)
    caps_applied: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)  # short human-readable notes, most important first
    calibrated: bool = False  # False until the weights have been tuned from data (evals/calibrate.py)

    @property
    def signals_used(self) -> int:
        return sum(1 for s in self.signals if s.available)


def syntax_signal(attempts: int) -> tuple[float, str]:
    """1.0 when the first generation passed every gate; each repair round loses confidence."""
    if attempts <= 1:
        return 1.0, "passed guardrails, planner and execution on the first attempt"
    value = {2: 0.6, 3: 0.3}.get(attempts, 0.0)
    return value, f"needed {attempts} attempts (repairs suggest the model was guessing)"


def schema_coverage_signal(
    *,
    sql_tables: list[str],
    sql_columns: list[str],
    context_tables: list[str],
    generated: GeneratedSQL | None,
) -> tuple[float, str]:
    """Does the SQL stay within what the question's context offered, and does it match what the model
    said it used? Three parts, averaged: (1) share of SQL tables that were in the retrieved context,
    (2) agreement between the model's declared and the actual tables, (3) the same for columns."""
    parts: list[tuple[float, str]] = []
    tables = {t.lower() for t in sql_tables}
    if tables and context_tables:
        inside = len(tables & {t.lower() for t in context_tables}) / len(tables)
        parts.append((inside, f"{inside:.0%} of the tables used were in the question's relevant context"))
    if generated is not None and generated.tables_used and tables:
        declared = {t.lower().split(".")[-1] for t in generated.tables_used}
        parts.append((_jaccard(tables, declared), "tables declared by the model vs used in the SQL"))
    if generated is not None and generated.columns_used and sql_columns:
        actual = {c.lower().split(".")[-1] for c in sql_columns}
        declared = {c.lower().split(".")[-1] for c in generated.columns_used}
        # one-sided: columns the SQL uses but the model never mentioned (extras it declared are harmless)
        parts.append((len(actual & declared) / len(actual), "columns used vs columns declared"))
    if not parts:
        return 1.0, "nothing to compare"
    return round(sum(p for p, _ in parts) / len(parts), 4), "; ".join(f"{d}: {v:.0%}" for v, d in parts)


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


class ConfidenceScorer:
    def __init__(self, cfg: ValidationConfig):
        self.cfg = cfg

    def cannot_answer(self, reason: str) -> ConfidenceReport:
        return ConfidenceReport(
            score=0.0,
            level="cannot_answer",
            reasons=[reason or "The schema cannot answer this question."],
            calibrated=self.cfg.calibrated,
        )

    def level_for(self, score: float) -> Level:
        if score >= self.cfg.levels.high:
            return "high"
        return "medium" if score >= self.cfg.levels.medium else "low"

    def score(
        self,
        signals: dict[str, tuple[float | None, str]],
        *,
        disagreement: bool = False,
        warnings: list[str] | None = None,
        truncated: bool = False,
    ) -> ConfidenceReport:
        sigs = [
            Signal(
                name=name,
                value=None
                if (got := signals.get(name, (None, "not measured")))[0] is None
                else _clip(got[0]),
                weight=self.cfg.weights.get(name, 0.0),
                detail=got[1],
            )
            for name in self.cfg.weights
        ]
        used = [s for s in sigs if s.available and s.weight > 0]
        if not used:
            return ConfidenceReport(
                score=0.0,
                level="low",
                signals=sigs,
                reasons=["No validation signal could be measured."],
                calibrated=self.cfg.calibrated,
            )
        total = sum(s.weight for s in used)
        score = sum(s.weight * s.value for s in used) / total  # type: ignore[operator]

        caps: list[str] = []
        reasons: list[str] = []
        if disagreement and score > self.cfg.caps.disagreement:
            score = self.cfg.caps.disagreement
            caps.append(f"two independent approaches returned different data (capped at {score})")
        relevant = [w for w in (warnings or []) if not self._ignored(w, truncated)]
        if relevant and score > self.cfg.caps.guardrail_warning:
            score = self.cfg.caps.guardrail_warning
            caps.append(f"guardrail warning: {relevant[0]} (capped at {score})")

        for s in sorted(used, key=lambda s: (s.value, -s.weight)):  # weakest evidence first
            if s.value < 0.7:  # type: ignore[operator]
                reasons.append(f"{s.name}: {s.detail}")
        reasons = [*caps, *reasons]
        score = round(score, 4)
        return ConfidenceReport(
            score=score,
            level=self.level_for(score),
            signals=sigs,
            caps_applied=caps,
            reasons=reasons,
            calibrated=self.cfg.calibrated,
        )

    def _ignored(self, warning: str, truncated: bool) -> bool:
        """G006 ("a LIMIT was added") is background noise unless it actually cut the answer short."""
        rule = warning.split(" ", 1)[0]
        return rule in self.cfg.warning_ignore_rules and not (rule == "G006" and truncated)


def _clip(v: float) -> float:
    return max(0.0, min(1.0, float(v)))
