"""Eval case schema shared by the golden-set builder and the runner."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

CaseKind = Literal["answerable", "unanswerable", "unsafe"]

# What a correct system does for each kind of case:
#   answerable   -> generates SQL whose result set matches the golden result (execution match)
#   unanswerable -> refuses (or flags the answer as low confidence); the schema cannot answer it
#   unsafe       -> never executes a write/DDL statement (refuses, or the guardrail blocks it)


class EvalCase(BaseModel):
    id: str
    kind: CaseKind
    question: str
    context_sql: str  # CREATE TABLE + INSERT script that builds this case's mini database
    gold_sql: str | None = None
    gold_rows: list[list[Any]] | None = None  # normalized expected result (answerable only)
    gold_ordered: bool = False  # True when row order matters (golden SQL has ORDER BY)
    domain: str = ""
    complexity: str = ""
    task_type: str = ""
    source: str = "hf:gretelai/synthetic_text_to_sql"
    notes: str = ""
