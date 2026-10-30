"""Calibration maths and the integrity of the labeled variant set."""

import numpy as np
import pytest

from evals.calibrate import (
    COARSE_EDGES,
    auc,
    detection,
    fit_weights,
    is_monotonic,
    reliability,
    rescore,
    weight_grid,
)
from evals.validation_set import CATEGORIES, load_variants
from text2sql.guardrails.config import load_guardrail_config
from text2sql.guardrails.engine import GuardrailEngine
from text2sql.validation.confidence import ConfidenceScorer
from text2sql.validation.config import SIGNALS, ValidationConfig

CFG = ValidationConfig()


def test_auc_perfect_random_and_ties():
    correct = np.array([True, True, False, False])
    assert auc(np.array([0.9, 0.8, 0.2, 0.1]), correct) == 1.0
    assert auc(np.array([0.1, 0.2, 0.8, 0.9]), correct) == 0.0
    assert auc(np.array([0.5, 0.5, 0.5, 0.5]), correct) == 0.5
    assert np.isnan(auc(np.array([0.5, 0.6]), np.array([True, True])))


def test_detection_recall_and_false_positive_rate():
    scores = np.array([0.95, 0.7, 0.4, 0.85, 0.3])
    correct = np.array([True, False, False, False, True])
    d = detection(scores, correct, 0.8)
    assert d["recall"] == pytest.approx(2 / 3)  # wrong ones: 0.7 and 0.4 flagged, 0.85 missed
    assert d["false_positive_rate"] == pytest.approx(0.5)  # correct ones: 0.3 flagged, 0.95 not
    assert (d["wrong_n"], d["correct_n"]) == (3, 2)


def test_reliability_buckets_and_monotonicity():
    scores = np.array([0.1, 0.3, 0.6, 0.7, 0.9, 0.95])
    correct = np.array([False, False, True, False, True, True])
    table = reliability(scores, correct, COARSE_EDGES)
    assert [r["n"] for r in table] == [2, 2, 2] and [r["accuracy"] for r in table] == [0.0, 0.5, 1.0]
    assert is_monotonic(table)
    bad = reliability(scores, ~correct, COARSE_EDGES)
    assert not is_monotonic(bad)
    assert is_monotonic([{"n": 0, "accuracy": None}, {"n": 3, "accuracy": 0.5}])


def test_weight_grid_sums_to_one_and_respects_the_floor():
    grid = list(weight_grid(list(SIGNALS)))
    assert grid and all(abs(sum(w.values()) - 1.0) < 1e-9 for w in grid)
    assert all(min(w.values()) >= 0.05 - 1e-9 for w in grid)
    assert len({tuple(w.values()) for w in grid}) == len(grid)


def _rec(label, **signals):
    base = dict.fromkeys(SIGNALS, 1.0) | signals
    return {"label": label, "signals": base, "warning_cap": False}


def test_rescore_matches_the_scorer_including_caps_and_missing_signals():
    scorer = ConfidenceScorer(CFG)
    cases = [
        dict(),
        dict(backtranslation=0.4, sanity=0.5),
        dict(multiquery=0.0),
        dict(backtranslation=None),
        dict(multiquery=None, syntax=0.3),
        dict(backtranslation=0.5),
    ]
    for sig in cases:
        rec = _rec("correct", **sig)
        want = scorer.score(
            {k: (v, "") for k, v in rec["signals"].items()},
            disagreement=sig.get("multiquery") == 0.0,
            misaligned=(sig.get("backtranslation") or 1.0) < CFG.backtranslation.flag_below,
        )
        assert rescore([rec], CFG.weights, CFG)[0] == pytest.approx(want.score, abs=1e-4), sig
    capped = _rec("correct") | {"warning_cap": True}
    assert rescore([capped], CFG.weights, CFG)[0] == 0.6


def test_fit_weights_moves_toward_the_informative_signal_and_keeps_the_rest_near_prd():
    # only `sanity` separates the classes; every other signal is uninformative
    recs = [_rec("correct", sanity=1.0) for _ in range(8)] + [_rec("wrong", sanity=0.2) for _ in range(8)]
    w, a = fit_weights(recs, CFG)
    assert a == 1.0 and sum(w.values()) == pytest.approx(1.0)
    assert (
        w["sanity"] >= CFG.weights["sanity"]
    )  # tie-break keeps weights near the PRD's, never lowers the signal
    # nothing informative at all: the PRD weights are kept exactly
    flat = [_rec("correct") for _ in range(5)] + [_rec("wrong") for _ in range(5)]
    kept, _ = fit_weights(flat, CFG)
    assert kept == pytest.approx(CFG.weights)


def test_fit_ignores_signals_that_were_never_measured():
    recs = [_rec("correct", backtranslation=None, multiquery=None, sanity=1.0)] * 4 + [
        _rec("wrong", backtranslation=None, multiquery=None, sanity=0.0)
    ] * 4
    w, _ = fit_weights(recs, CFG)
    assert w["backtranslation"] == 0.0 and w["multiquery"] == 0.0


# -- the labeled set ---------------------------------------------------------------------------------
def test_variant_set_meets_the_acceptance_shape():
    variants = load_variants()
    wrong = [v for v in variants if v.label == "wrong"]
    correct = [v for v in variants if v.label == "correct"]
    assert len(wrong) >= 30, "PRD: at least 30 deliberately wrong SQL variants"
    assert len(correct) >= 10, "need correct rewrites to measure false positives"
    by_cat = {c: sum(1 for v in wrong if v.category == c) for c in CATEGORIES}
    assert all(n >= 5 for n in by_cat.values()), by_cat  # wrong JOIN / filter / aggregation / date range
    assert len({v.id for v in variants}) == len(variants)


def test_every_variant_passes_the_guardrails_statically_and_differs_from_its_reference():
    engine = GuardrailEngine(load_guardrail_config())
    for v in load_variants():
        for sql in (v.sql, v.reference):
            assert engine.check(sql).allowed, f"{v.id}: {engine.check(sql).reason}"
        assert " ".join(v.sql.split()) != " ".join(v.reference.split()), v.id
