"""Confidence scorer: weights, renormalisation, caps, short-circuit, signal helpers."""

import pytest

from tests.helpers import gen
from text2sql.validation.confidence import ConfidenceScorer, schema_coverage_signal, syntax_signal
from text2sql.validation.config import ValidationConfig, load_validation_config

CFG = ValidationConfig()
ALL_ONE = {n: (1.0, "ok") for n in CFG.weights}


def scorer(**over):
    return ConfidenceScorer(load_validation_config(overrides=over) if over else CFG)


def test_prd_initial_weights_are_the_defaults():
    assert CFG.weights == {
        "backtranslation": 0.30,
        "multiquery": 0.25,
        "sanity": 0.20,
        "schema_coverage": 0.15,
        "syntax": 0.10,
    }
    assert sum(CFG.weights.values()) == pytest.approx(1.0)
    assert CFG.caps.disagreement == 0.5 and CFG.caps.guardrail_warning == 0.6


def test_yaml_defaults_match_the_pydantic_defaults():
    assert load_validation_config().model_dump() == ValidationConfig().model_dump()
    assert load_validation_config().calibrated is False


def test_all_signals_perfect_is_one_and_high():
    r = scorer().score(ALL_ONE)
    assert r.score == 1.0 and r.level == "high" and r.signals_used == 5 and not r.caps_applied


def test_weighted_sum():
    sig = {**ALL_ONE, "backtranslation": (0.0, "bad"), "sanity": (0.5, "meh")}
    assert scorer().score(sig).score == pytest.approx(1 - 0.30 - 0.10, abs=1e-6)


def test_missing_signal_renormalises_instead_of_counting_as_pass_or_fail():
    sig = {k: v for k, v in ALL_ONE.items() if k != "multiquery"}
    r = scorer().score(sig)
    assert r.score == 1.0 and r.signals_used == 4
    sig["backtranslation"] = (0.5, "x")
    assert scorer().score(sig).score == pytest.approx((0.15 + 0.20 + 0.15 + 0.10) / 0.75, abs=1e-3)


def test_no_signals_is_low_not_a_crash():
    r = scorer().score({})
    assert r.score == 0.0 and r.level == "low"


def test_disagreement_caps_at_half_even_when_everything_else_is_perfect():
    sig = {**ALL_ONE, "multiquery": (0.0, "differs")}
    r = scorer().score(sig, disagreement=True)
    assert r.score <= 0.5 and r.level == "medium" and r.caps_applied
    assert scorer().score({**ALL_ONE}, disagreement=True).score == 0.5


def test_guardrail_warning_caps_at_point_six():
    r = scorer().score(ALL_ONE, warnings=["G007 subquery_depth: nesting 4 exceeds 3"])
    assert r.score == 0.6 and "guardrail warning" in r.caps_applied[0]


def test_row_limit_notice_is_not_a_warning_unless_the_result_was_cut():
    w = ["G006 row_limit: LIMIT 1000 applied"]
    assert scorer().score(ALL_ONE, warnings=w).score == 1.0
    assert scorer().score(ALL_ONE, warnings=w, truncated=True).score == 0.6


def test_both_caps_take_the_lower_value():
    r = scorer().score(ALL_ONE, disagreement=True, warnings=["G007 x"])
    assert r.score == 0.5 and len(r.caps_applied) == 1  # second cap would not lower it further


def test_unanswerable_short_circuits():
    r = scorer().cannot_answer("no supplier table")
    assert r.score == 0.0 and r.level == "cannot_answer" and r.signals == [] and "no supplier" in r.reasons[0]


def test_levels():
    s = scorer()
    assert [s.level_for(x) for x in (0.95, 0.8, 0.79, 0.5, 0.49)] == [
        "high",
        "high",
        "medium",
        "medium",
        "low",
    ]


def test_reasons_list_weak_signals_with_caps_first():
    sig = {**ALL_ONE, "backtranslation": (0.4, "filter mismatch"), "multiquery": (0.0, "differs")}
    r = scorer().score(sig, disagreement=True)
    assert r.reasons[0].startswith("two independent") and any("filter mismatch" in x for x in r.reasons)


def test_values_are_clipped_and_self_confidence_is_not_an_input():
    r = scorer().score({"backtranslation": (7.0, "x"), "sanity": (-3.0, "y")})
    assert 0.0 <= r.score <= 1.0
    assert "self_confidence" not in {s.name for s in r.signals}


def test_custom_weights_and_validation():
    only_bt = scorer(
        weights={"backtranslation": 1.0, "multiquery": 0, "sanity": 0, "schema_coverage": 0, "syntax": 0}
    )
    assert only_bt.score({"backtranslation": (0.4, "x"), "sanity": (1.0, "y")}).score == 0.4
    with pytest.raises(ValueError):
        ValidationConfig(weights={"made_up": 1.0})
    with pytest.raises(ValueError):
        ValidationConfig(weights={"sanity": 0, "syntax": 0})


def test_syntax_signal_penalises_repairs():
    assert syntax_signal(1)[0] == 1.0 and syntax_signal(2)[0] == 0.6 and syntax_signal(3)[0] == 0.3
    assert syntax_signal(7)[0] == 0.0


def test_schema_coverage_full_and_partial():
    g = gen("SELECT 1", tables_used=["transactions", "categories"], columns_used=["transactions.amount"])
    full, _ = schema_coverage_signal(
        sql_tables=["transactions", "categories"],
        sql_columns=["amount"],
        context_tables=["transactions", "categories", "accounts"],
        generated=g,
    )
    assert full == 1.0
    partial, detail = schema_coverage_signal(
        sql_tables=["transactions", "budgets"],  # budgets was not retrieved; model claimed categories
        sql_columns=["amount", "month"],  # model never mentioned month
        context_tables=["transactions", "categories"],
        generated=g,
    )
    assert partial < 0.6 and "context" in detail


def test_schema_coverage_with_nothing_to_compare_is_neutral():
    assert schema_coverage_signal(sql_tables=[], sql_columns=[], context_tables=[], generated=None)[0] == 1.0
