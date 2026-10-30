"""SQL guardrails: AST allow-list, function deny-list, table allow-list, row limit, depth, cost gate."""

from __future__ import annotations

from functools import lru_cache

from text2sql.guardrails.config import GuardrailConfig, load_guardrail_config
from text2sql.guardrails.engine import GuardrailDecision, GuardrailEngine
from text2sql.guardrails.rules import RULE_NAMES, Violation

__all__ = [
    "GuardrailConfig",
    "GuardrailDecision",
    "GuardrailEngine",
    "RULE_NAMES",
    "Violation",
    "check_sql",
    "load_guardrail_config",
]


@lru_cache(maxsize=1)
def _default_engine() -> GuardrailEngine:
    return GuardrailEngine(load_guardrail_config())


def check_sql(sql: str) -> GuardrailDecision:
    """Structural check with default rules and no schema (no table allow-list).

    This is the hook for the eval harness:
    ``python -m evals.run_evals --guardrail text2sql.guardrails:check_sql``
    """
    return _default_engine().check(sql, None)
