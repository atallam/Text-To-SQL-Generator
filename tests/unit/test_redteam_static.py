"""Red-team suite, guardrail layer only (no database): every dangerous fixture must be blocked, and
harmless look-alikes must not be. The database-role half of the suite is in
tests/integration/test_phase2_postgres.py."""

import pytest
import yaml

from tests.helpers import ROOT
from text2sql.guardrails import GuardrailEngine, load_guardrail_config

FIXTURES = yaml.safe_load((ROOT / "evals" / "redteam" / "queries.yaml").read_text(encoding="utf-8"))
STATIC = [q for q in FIXTURES["queries"] if q.get("layer") != "explain"]


@pytest.fixture(scope="module")
def engine(wealthlens_source):
    return GuardrailEngine(load_guardrail_config(wealthlens_source))


def test_there_are_at_least_forty_dangerous_fixtures():
    assert len(FIXTURES["queries"]) >= 40
    assert len({q["id"] for q in FIXTURES["queries"]}) == len(FIXTURES["queries"])


@pytest.mark.parametrize("case", STATIC, ids=lambda c: c["id"])
def test_dangerous_query_is_blocked(engine, wealthlens_snapshot, case):
    decision = engine.check(case["sql"], wealthlens_snapshot)
    assert decision.blocked, f"NOT BLOCKED: {case['sql']!r} -> {decision.sql!r}"
    assert decision.sql is None  # nothing executable leaves a blocked decision
    assert set(case["rules"]) & set(decision.rule_ids), (case["rules"], decision.rule_ids, decision.reason)


@pytest.mark.parametrize("case", FIXTURES["must_allow"], ids=lambda c: c["id"])
def test_harmless_lookalike_is_allowed(engine, wealthlens_snapshot, case):
    decision = engine.check(case["sql"], wealthlens_snapshot)
    assert decision.allowed, f"FALSE POSITIVE: {case['sql']!r} -> {decision.reason}"
    assert decision.sql
