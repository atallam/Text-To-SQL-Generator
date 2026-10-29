"""Things the eval runner can score.

A predictor takes an eval case plus the schema text and returns a ``Prediction``. Swap in your
real pipeline later by writing a class with the same two members (``name`` and ``predict``).

  oracle  returns the golden SQL (and refuses unanswerable cases): proves the harness itself
          scores 100%, so any drop later is the system's fault, not the harness's.
  claude  a plain one-shot baseline through the project's LLMClient (no guardrails, no
          validation): the number your full pipeline has to beat.
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from evals.schema import EvalCase


class Prediction(BaseModel):
    sql: str | None = None
    refused: bool = False
    flagged: bool = False  # the system itself marked this answer as low confidence / suspect
    confidence: float | None = None
    explanation: str = ""
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class Predictor(Protocol):
    name: str

    def predict(self, case: EvalCase, schema_text: str) -> Prediction: ...


class OraclePredictor:
    name = "oracle"

    def predict(self, case: EvalCase, schema_text: str) -> Prediction:
        if case.kind == "unanswerable":
            return Prediction(refused=True, confidence=1.0, explanation="oracle: schema cannot answer")
        return Prediction(sql=case.gold_sql, confidence=1.0, explanation="oracle: golden SQL")


class _SQLAnswer(BaseModel):
    """The SQL answer to the user's question, or a refusal when the schema cannot answer it."""

    answerable: bool = Field(description="False when the schema cannot answer the question.")
    sql: str | None = Field(
        default=None, description="One read-only SQLite SELECT statement. Null when not answerable."
    )
    explanation: str = Field(description="One sentence on what the query does, or why it cannot be answered.")
    confidence: float = Field(ge=0, le=1, description="Probability that the SQL answers the question.")


_SYSTEM = (
    "You translate questions into SQLite SQL.\n"
    "- Use ONLY the tables and columns in the provided schema.\n"
    "- Write exactly one read-only SELECT (CTEs allowed). Never write data-modifying or DDL SQL.\n"
    "- If the schema cannot answer the question, or the request asks to change data or the schema, "
    "set answerable=false and sql=null instead of guessing."
)


class ClaudePredictor:
    """One-shot baseline: schema + question -> SQL, through the project's LLM client."""

    name = "claude"

    def __init__(self, model: str | None = None, low_confidence: float = 0.5):
        from text2sql.llm.client import LLMClient  # imported lazily so the harness has no hard dependency

        self._client = LLMClient()
        self._model = model
        self._low_confidence = low_confidence
        if model:
            self.name = f"claude-{model}"

    def predict(self, case: EvalCase, schema_text: str) -> Prediction:
        result = self._client.complete_structured(
            agent_name="eval_baseline_sql",
            system=_SYSTEM,
            user=f"Schema:\n{schema_text}\n\nQuestion: {case.question}",
            response_model=_SQLAnswer,
            model=self._model,
        )
        answer = result.parsed
        refused = (not answer.answerable) or not (answer.sql and answer.sql.strip())
        return Prediction(
            sql=None if refused else answer.sql,
            refused=refused,
            flagged=answer.confidence < self._low_confidence,
            confidence=answer.confidence,
            explanation=answer.explanation,
            latency_ms=result.latency_ms,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
        )


def make_predictor(name: str, model: str | None = None) -> Predictor:
    if name == "oracle":
        return OraclePredictor()
    if name == "claude":
        return ClaudePredictor(model=model)
    raise ValueError(f"Unknown predictor {name!r}. Choose: oracle, claude")
