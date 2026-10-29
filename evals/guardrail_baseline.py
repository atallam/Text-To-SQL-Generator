"""Placeholder guardrail so the eval harness runs before Phase 2 exists.

This is deliberately simple (keyword + statement checks). It is NOT the production guardrail:
Phase 2 replaces it with the AST-based allow-list. Plug the real one in with
``python -m evals.run_evals --guardrail text2sql.guardrails:check_sql`` (any callable that takes
the SQL string and returns a bool, or an object with a ``.blocked`` attribute, where True = blocked).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_STRING_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|attach|detach|vacuum|reindex|pragma|grant|revoke)\b",
    re.IGNORECASE,
)
_REPLACE_INTO_RE = re.compile(r"\breplace\s+into\b", re.IGNORECASE)
_READ_START_RE = re.compile(r"^\s*\(*\s*(select|with|values)\b", re.IGNORECASE)


@dataclass
class GuardrailVerdict:
    blocked: bool
    reason: str = ""


def _strip(sql: str) -> str:
    """Remove comments and string literals so keywords inside them are ignored."""
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub(" ", sql)
    return _STRING_RE.sub("''", sql)


def is_read_statement(sql: str) -> bool:
    """True when the statement starts like a read (SELECT / WITH / VALUES)."""
    return bool(_READ_START_RE.match(_strip(sql)))


def basic_guardrail(sql: str) -> GuardrailVerdict:
    text = _strip(sql)
    statements = [s for s in text.split(";") if s.strip()]
    if not statements:
        return GuardrailVerdict(True, "empty statement")
    if len(statements) > 1:
        return GuardrailVerdict(True, "multiple statements")
    if not _READ_START_RE.match(text):
        return GuardrailVerdict(True, "not a SELECT statement")
    match = _FORBIDDEN_RE.search(text) or _REPLACE_INTO_RE.search(text)
    if match:
        return GuardrailVerdict(True, f"forbidden keyword: {match.group(0).lower()}")
    return GuardrailVerdict(False, "")
