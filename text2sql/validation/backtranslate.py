"""Back-translation check: does the SQL, read cold, answer the question that was asked?

Two separate calls so neither model can rationalise the other's work:

1. ``SQLDescriber`` sees the schema and the SQL only (never the question) and writes what the query
   computes.
2. ``AlignmentJudge`` sees the question and that description only (never the SQL, the generator's
   explanation or its self-reported confidence) and grades them on a fixed rubric.

The alignment score is computed here from the per-dimension grades, not asked for as a number: models are
poor at calibrated numbers but decent at "does the time range match". Both calls use the judge-class
model (``Settings.llm_model_judge``).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from text2sql.llm.client import LLMClient
from text2sql.prompts import LATEST_VERSIONS, SQL_DESCRIPTION, SQL_JUDGE, render
from text2sql.schema.model import SchemaSnapshot
from text2sql.schema.render import render_schema
from text2sql.validation.config import BackTranslationConfig

Grade = Literal["match", "partial", "mismatch", "not_applicable"]
_GRADE_VALUE = {"match": 1.0, "partial": 0.5, "mismatch": 0.0}
MAX_QUESTION_CHARS = 2000
_TAG_RE = re.compile(r"</?\s*(question|sql|description)\s*>", re.IGNORECASE)  # cannot close our wrappers


def _clean(text: str) -> str:
    return _TAG_RE.sub("", text)


class SQLDescription(BaseModel):
    """What a query computes, in business terms. Produced without knowing the question."""

    summary: str = Field(description="One or two sentences: the question this query answers.")
    entities: list[str] = Field(default_factory=list, description="The business things it is about.")
    measures: list[str] = Field(
        default_factory=list, description="What is computed (e.g. 'sum of debit amounts')."
    )
    filters: list[str] = Field(default_factory=list, description="Every condition, literal values included.")
    grouping: list[str] = Field(default_factory=list, description="The breakdown level, if any.")
    time_range: str | None = Field(default=None, description="The period covered, boundaries included.")
    ordering_limit: str | None = Field(default=None, description="Sort order and row limit, if any.")

    def as_text(self) -> str:
        lines = [f"Summary: {self.summary.strip()}"]
        for label, items in (
            ("About", self.entities),
            ("Computes", self.measures),
            ("Filters", self.filters),
            ("Grouped by", self.grouping),
        ):
            lines.append(f"{label}: " + ("; ".join(items) if items else "(none)"))
        lines.append(f"Time range: {self.time_range or '(none)'}")
        lines.append(f"Ordering/limit: {self.ordering_limit or '(none)'}")
        return "\n".join(lines)


class AlignmentVerdict(BaseModel):
    """The judge's rubric grades. The numeric score is derived, see ``alignment_score``."""

    entity: Grade
    measure: Grade
    filters: Grade
    time_range: Grade
    grouping: Grade
    output_shape: Grade
    issues: list[str] = Field(default_factory=list, description="One short sentence per concrete problem.")


def alignment_score(verdict: AlignmentVerdict, cfg: BackTranslationConfig) -> float:
    """Weighted mean of the graded dimensions (match 1, partial 0.5, mismatch 0), skipping
    ``not_applicable``. Any mismatch caps the score, so one wrong filter cannot hide among five right
    dimensions."""
    total = weight = 0.0
    for dim, w in cfg.dimension_weights.items():
        grade = getattr(verdict, dim, "not_applicable")
        if grade == "not_applicable":
            continue
        total += w * _GRADE_VALUE[grade]
        weight += w
    score = total / weight if weight else 1.0
    if any(getattr(verdict, d, None) == "mismatch" for d in cfg.dimension_weights):
        score = min(score, cfg.mismatch_cap)
    return round(score, 4)


class BackTranslation(BaseModel):
    description: SQLDescription
    verdict: AlignmentVerdict
    alignment: float
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def issues(self) -> list[str]:
        return self.verdict.issues


class BackTranslator:
    def __init__(self, llm: LLMClient, cfg: BackTranslationConfig, model: str | None = None):
        self.llm, self.cfg, self.model = llm, cfg, model

    def check(self, question: str, sql: str, schema: SchemaSnapshot) -> BackTranslation:
        schema_text = render_schema(schema)
        desc = self.llm.complete_structured(
            agent_name="sql_describer",
            system=render(SQL_DESCRIPTION, LATEST_VERSIONS[SQL_DESCRIPTION], "system"),
            user=render(
                SQL_DESCRIPTION, LATEST_VERSIONS[SQL_DESCRIPTION], "user", schema=schema_text, sql=_clean(sql)
            ),
            response_model=SQLDescription,
            model=self.model,
        )
        verdict = self.llm.complete_structured(
            agent_name="sql_judge",
            system=render(SQL_JUDGE, LATEST_VERSIONS[SQL_JUDGE], "system"),
            user=render(
                SQL_JUDGE,
                LATEST_VERSIONS[SQL_JUDGE],
                "user",
                question=_clean(" ".join(question.split())[:MAX_QUESTION_CHARS]),
                description=_clean(desc.parsed.as_text()),
            ),
            response_model=AlignmentVerdict,
            model=self.model,
        )
        return BackTranslation(
            description=desc.parsed,
            verdict=verdict.parsed,
            alignment=alignment_score(verdict.parsed, self.cfg),
            input_tokens=desc.usage.input_tokens + verdict.usage.input_tokens,
            output_tokens=desc.usage.output_tokens + verdict.usage.output_tokens,
        )
