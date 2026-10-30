"""SQL generator: one structured LLM call (no business logic). Safety checks happen downstream."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from text2sql.generation.context import PromptBundle
from text2sql.llm.client import LLMClient, StructuredResult
from text2sql.prompts import LATEST_VERSIONS, SQL_REPAIR, render

MAX_ERROR_CHARS = 500


class GeneratedSQL(BaseModel):
    """The answer to a business question: one read-only SQL query, or a statement it cannot be answered."""

    sql: str = Field(
        default="",
        description="Exactly one read-only SELECT statement (WITH allowed). Empty when unanswerable.",
    )
    explanation: str = Field(description="One or two plain-English sentences on what the query computes.")
    self_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="0 to 1: how sure you are the SQL answers the question as asked.",
    )
    tables_used: list[str] = Field(default_factory=list, description="Tables referenced by the SQL.")
    columns_used: list[str] = Field(default_factory=list, description="Columns referenced, as table.column.")
    unanswerable: bool = Field(
        default=False,
        description="True when the schema cannot answer the question or the request is not a read.",
    )
    unanswerable_reason: str | None = Field(
        default=None, description="What is missing; required if unanswerable."
    )

    @model_validator(mode="after")
    def _consistent(self) -> GeneratedSQL:
        if self.unanswerable:
            if not (self.unanswerable_reason or "").strip():
                raise ValueError("unanswerable_reason is required when unanswerable is true")
            self.sql = ""
        elif not self.sql.strip():
            raise ValueError("sql is required unless unanswerable is true")
        return self


class FailedAttempt(BaseModel):
    n: int
    sql: str
    stage: str  # guardrail | explain | execution
    error: str

    def clipped(self) -> FailedAttempt:
        text = " ".join(self.error.split())
        return self.model_copy(update={"error": text[:MAX_ERROR_CHARS]})


class SQLGenerator:
    def __init__(self, llm: LLMClient, model: str | None = None):
        self.llm = llm
        self.model = model

    def generate(
        self, prompt: PromptBundle, failed: list[FailedAttempt] | None = None
    ) -> StructuredResult[GeneratedSQL]:
        """First attempt, or a repair attempt that sees what went wrong before (the bounded loop's step)."""
        user = prompt.user
        agent = "sql_generator"
        if failed:
            agent = "sql_repair"
            user = render(
                SQL_REPAIR,
                LATEST_VERSIONS[SQL_REPAIR],
                "user",
                original_prompt=prompt.user.rstrip(),
                attempts=[f.clipped().model_dump() for f in failed],
            )
        return self.llm.complete_structured(
            agent_name=agent, system=prompt.system, user=user, response_model=GeneratedSQL, model=self.model
        )
