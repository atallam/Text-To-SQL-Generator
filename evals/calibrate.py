"""Calibrate the confidence score: detection recall, false-positive rate, reliability table, tuned weights.

    # Offline (no API key): deterministic signals only. Proves the harness and shows what sanity checks and an
    # ideal second approach can catch. NOT a calibration of the LLM-backed weights.
    python -m evals.calibrate --offline

    # Live (needs ANTHROPIC_API_KEY): runs back-translation + a real independent second query on every
    # variant, then fits the weights. This is the run that closes the Phase 3 exit gate.
    python -m evals.calibrate --live [--limit N] [--workers 2]

Inputs : evals/validation/variants.yaml (40 wrong + 14 correct SQL variants over the seeded WealthLens DB).
Outputs: evals/calibration/signals[.offline].jsonl, report[.offline].md, and for --live weights.fitted.yaml.
Needs the WealthLens database up (see README: WEALTHLENS_READONLY_URL / WEALTHLENS_ADMIN_URL).

The pure maths (AUC, buckets, weight search) lives at the top of the file and is unit-tested.
"""

# ruff: noqa: E501  (report prose)
from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from evals.validation_set import CATEGORIES, Variant, load_variants
from text2sql.validation.config import SIGNALS, ValidationConfig, load_validation_config

OUT_DIR = Path(__file__).parent / "calibration"
FLAG_BELOW = (
    0.8  # a variant counts as "flagged as possibly wrong" when its confidence is below the high bucket
)
SIGNAL_FLAG_BELOW = {  # per-signal "this signal alone says something is off"
    "backtranslation": 0.75,
    "multiquery": 1.0,
    "sanity": 1.0,
    "schema_coverage": 0.75,
    "syntax": 1.0,
}
COARSE_EDGES = (0.0, 0.5, 0.8, 1.0001)
FINE_EDGES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0001)
WEIGHT_STEP = 0.05
WEIGHT_FLOOR = 0.05  # every signal keeps a minimum say: a tiny sample must not switch a signal off


# =============================================================================================
# maths (pure)
# =============================================================================================
def auc(scores: np.ndarray, correct: np.ndarray) -> float:
    """P(score of a random correct SQL > score of a random wrong SQL); ties count half."""
    pos, neg = scores[correct], scores[~correct]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    diff = pos[:, None] - neg[None, :]
    return float(((diff > 0).sum() + 0.5 * (diff == 0).sum()) / diff.size)


def detection(scores: np.ndarray, correct: np.ndarray, threshold: float = FLAG_BELOW) -> dict[str, float]:
    flagged = scores < threshold
    wrong, right = ~correct, correct
    return {
        "recall": float(flagged[wrong].mean()) if wrong.any() else float("nan"),
        "false_positive_rate": float(flagged[right].mean()) if right.any() else float("nan"),
        "wrong_n": int(wrong.sum()),
        "correct_n": int(right.sum()),
    }


def reliability(scores: np.ndarray, correct: np.ndarray, edges: tuple[float, ...]) -> list[dict[str, Any]]:
    rows = []
    for lo, hi in itertools.pairwise(edges):
        mask = (scores >= lo) & (scores < hi)
        n = int(mask.sum())
        rows.append(
            {
                "lo": lo,
                "hi": min(hi, 1.0),
                "n": n,
                "correct": int(correct[mask].sum()),
                "accuracy": float(correct[mask].mean()) if n else None,
            }
        )
    return rows


def is_monotonic(table: list[dict[str, Any]]) -> bool:
    """Accuracy never decreases as confidence rises (empty buckets are skipped)."""
    acc = [r["accuracy"] for r in table if r["n"] and r["accuracy"] is not None]
    return all(b >= a - 1e-12 for a, b in itertools.pairwise(acc))


def rescore(records: list[dict], weights: dict[str, float], cfg: ValidationConfig) -> np.ndarray:
    """Vectorised twin of ConfidenceScorer.score for recorded signals (kept equal by a unit test)."""
    names = list(SIGNALS)
    values = np.array(
        [[np.nan if r["signals"].get(n) is None else r["signals"][n] for n in names] for r in records]
    )
    w = np.array([weights.get(n, 0.0) for n in names])
    avail = ~np.isnan(values) & (w > 0)[None, :]
    num = np.where(avail, np.nan_to_num(values) * w, 0.0).sum(axis=1)
    den = np.where(avail, w, 0.0).sum(axis=1)
    scores = np.divide(num, den, out=np.zeros_like(num), where=den > 0)
    mq = names.index("multiquery")
    disagree = np.array([r["signals"].get("multiquery") == 0.0 for r in records]) & ~np.isnan(values[:, mq])
    scores = np.where(disagree, np.minimum(scores, cfg.caps.disagreement), scores)
    warned = np.array([bool(r.get("warning_cap")) for r in records])
    scores = np.where(warned, np.minimum(scores, cfg.caps.guardrail_warning), scores)
    return np.round(scores, 4)


def weight_grid(names: list[str], step: float = WEIGHT_STEP, floor: float = WEIGHT_FLOOR):
    units = round(1 / step)
    floor_u = round(floor / step)
    for combo in itertools.product(range(floor_u, units + 1), repeat=len(names) - 1):
        last = units - sum(combo)
        if last >= floor_u:
            yield dict(zip(names, [c * step for c in (*combo, last)], strict=True))


def fit_weights(records: list[dict], base: ValidationConfig) -> tuple[dict[str, float], float]:
    """Weights (summing to 1) that best separate correct from wrong by AUC. Many weight sets tie on a small
    sample, so ties go to the set closest to the PRD's initial weights: evidence moves the weights, noise
    does not."""
    correct = np.array([r["label"] == "correct" for r in records])
    names = [n for n in SIGNALS if any(r["signals"].get(n) is not None for r in records)]
    best: tuple[float, float, dict[str, float]] | None = None
    for w in weight_grid(names):
        a = auc(rescore(records, w, base), correct)
        dist = sum(abs(w[n] - base.weights.get(n, 0.0)) for n in names)
        key = (round(a, 6), -dist)
        if best is None or key > (round(best[0], 6), -best[1]):
            best = (a, dist, w)
    assert best is not None
    full = {n: best[2].get(n, 0.0) for n in SIGNALS}
    return full, best[0]


def cross_validated_auc(records: list[dict], base: ValidationConfig, folds: int = 5) -> dict[str, float]:
    """Fit on k-1 folds, score the held-out fold; compare with the PRD weights on the same held-out data."""
    idx = np.arange(len(records))
    held_fit: list[float] = []
    held_base: list[float] = []
    all_scores_fit = np.zeros(len(records))
    all_scores_base = rescore(records, base.weights, base)
    for k in range(folds):
        test = idx % folds == k
        train_records = [r for r, t in zip(records, test, strict=True) if not t]
        if len({r["label"] for r in train_records}) < 2:
            continue
        w, _ = fit_weights(train_records, base)
        all_scores_fit[test] = rescore([r for r, t in zip(records, test, strict=True) if t], w, base)
    correct = np.array([r["label"] == "correct" for r in records])
    held_fit.append(auc(all_scores_fit, correct))
    held_base.append(auc(all_scores_base, correct))
    return {"fitted_cv_auc": held_fit[0], "initial_weights_auc": held_base[0]}


# =============================================================================================
# evaluation
# =============================================================================================
def _record(v: Variant, signals: dict[str, float | None], warning_cap: bool, extra: dict) -> dict:
    return {
        "id": v.id,
        "case": v.case,
        "label": v.label,
        "category": v.category,
        "signals": signals,
        "warning_cap": warning_cap,
        **extra,
    }


class _NoLLM:
    """Offline mode must never reach a model."""

    def complete_structured(self, **_kw):
        raise RuntimeError("the offline calibration run does not call an LLM")


def build_stack(live: bool):
    """Pipeline pieces against the configured WealthLens database."""
    from text2sql.config.settings import get_settings
    from text2sql.llm.client import LLMClient
    from text2sql.pipeline import Text2SQL
    from text2sql.validation import Validator

    settings = get_settings()
    llm = LLMClient(settings) if live else _NoLLM()
    extra: dict[str, Any] = {}
    if not live:  # offline never builds prompts, so skip the embedding model download
        from text2sql.schema.embeddings import LexicalEmbedder

        extra["embedder"] = LexicalEmbedder()
    t2s = Text2SQL.from_source("wealthlens", llm=llm, settings=settings, validate=False, **extra)
    cfg = load_validation_config(
        t2s.builder.source,
        overrides={"multiquery": {"complexity_threshold": 0}}
        if live
        else None,  # second approach for EVERY variant
    )
    validator = (
        Validator.build(
            llm=llm, engine=t2s.engine, gate=t2s.gate, executor=t2s.executor, cfg=cfg, settings=settings
        )
        if live
        else None
    )
    return t2s, cfg, validator


def _execute(t2s, sql: str):
    decision = t2s.engine.check(sql, t2s.builder.schema.get())
    if not decision.allowed:
        raise RuntimeError(f"guardrails refused a labeled variant: {decision.reason}")
    return decision, t2s.executor.run(decision.sql)


def evaluate_offline(t2s, cfg: ValidationConfig, v: Variant, refs: dict) -> dict:
    """Deterministic signals only. The oracle comparison (reference SQL as a perfect second approach) is
    recorded separately as ``oracle_agrees`` and never enters the score: it is a ceiling, not a measurement."""
    from text2sql.validation.confidence import schema_coverage_signal, syntax_signal
    from text2sql.validation.multiquery import compare_results
    from text2sql.validation.sanity import SanityChecker, TableStats

    schema = t2s.builder.schema.get()
    decision, result = _execute(t2s, v.sql)
    if v.case not in refs:
        refs[v.case] = _execute(t2s, v.reference)[1]
    sanity = SanityChecker(cfg.sanity, schema, TableStats(t2s.executor, schema)).run(decision.sql, result)
    agree = compare_results(refs[v.case], result, cfg.multiquery).agree
    cov, _ = schema_coverage_signal(
        sql_tables=decision.tables, sql_columns=[], context_tables=decision.tables, generated=None
    )
    signals = {
        "backtranslation": None,
        "multiquery": None,
        "sanity": sanity.pass_rate,
        "schema_coverage": cov,
        "syntax": syntax_signal(1)[0],
    }
    flags = [f"{c.name}: {c.detail}" for c in sanity.flagged]
    return _record(v, signals, False, {"oracle_agrees": agree, "sanity_flags": flags})


def evaluate_live(t2s, validator, v: Variant) -> dict:
    from text2sql.generation.generator import GeneratedSQL
    from text2sql.generation.repair_loop import Attempt, LoopResult

    schema = t2s.builder.schema.get()
    prepared = t2s.builder.prepare(v.question)
    if (
        prepared.status != "ready"
    ):  # ambiguous term: take the first interpretation, as a user clicking "1" would
        choices = {t.term: 0 for t in prepared.clarification.terms}
        prepared = t2s.builder.prepare(v.question, choices)
    decision, result = _execute(t2s, v.sql)
    loop = LoopResult(
        status="success",
        generated=GeneratedSQL(
            sql=v.sql, explanation="", self_confidence=0.0, tables_used=decision.tables, columns_used=[]
        ),
        decision=decision,
        execution=result,
        attempts=[Attempt(n=1, stage="execution", ok=True)],
    )
    report = validator.validate(v.question, prepared.prompt, loop, schema)
    signals = {s.name: s.value for s in report.confidence.signals}
    warned = any("guardrail warning" in c for c in report.confidence.caps_applied)
    extra = {
        "score_recorded": report.confidence.score,
        "level": report.confidence.level,
        "issues": report.back_translation.issues if report.back_translation else [],
        "errors": report.errors,
        "tokens": [report.input_tokens, report.output_tokens],
    }
    return _record(v, signals, warned, extra)


# =============================================================================================
# report
# =============================================================================================
def _pct(x: float | None) -> str:
    return "n/a" if x is None or x != x else f"{100 * x:.0f}%"


def _table(rows: list[dict[str, Any]]) -> list[str]:
    out = ["| Confidence | Variants | Correct | Accuracy |", "|---|---:|---:|---:|"]
    for r in rows:
        out.append(f"| {r['lo']:.1f} to {r['hi']:.1f} | {r['n']} | {r['correct']} | {_pct(r['accuracy'])} |")
    return out


def build_report(
    records: list[dict],
    cfg: ValidationConfig,
    *,
    mode: str,
    fitted: tuple[dict, float] | None,
    cv: dict | None,
) -> str:
    correct = np.array([r["label"] == "correct" for r in records])
    scores = rescore(records, cfg.weights, cfg)
    det = detection(scores, correct)
    live = mode == "live"
    lines = [
        f"# Confidence calibration report ({mode})",
        "",
        f"Generated {date.today().isoformat()} from {len(records)} labeled variants "
        f"({det['wrong_n']} wrong, {det['correct_n']} correct) on the seeded WealthLens database.",
        "",
    ]
    if not live:
        lines += [
            "> **OFFLINE RUN: not a calibration.** No LLM was called, so `backtranslation` (weight 0.30) and `multiquery`",
            "> (weight 0.25) are missing and the score below rests on sanity checks, schema coverage and syntax alone",
            "> (weights renormalised). Expect low recall: that is what the deterministic checks can catch by themselves.",
            "> The Phase 3 exit gate (recall of at least 80%) needs `python -m evals.calibrate --live`.",
            "",
        ]
    lines += [
        "## Detection (flagged = confidence below 0.8, i.e. not in the high bucket)",
        "",
        f"* **Recall on wrong SQL: {_pct(det['recall'])}** (target: at least 80%)",
        f"* False-positive rate on correct SQL: {_pct(det['false_positive_rate'])}",
        f"* AUC (correct ranked above wrong): {auc(scores, correct):.3f}",
        "",
        "| Wrong-SQL category | Variants | Flagged | Recall |",
        "|---|---:|---:|---:|",
    ]
    for cat in CATEGORIES:
        mask = np.array([r["category"] == cat for r in records])
        if mask.any():
            n, hit = int(mask.sum()), int((scores[mask] < FLAG_BELOW).sum())
            lines.append(f"| {cat} | {n} | {hit} | {_pct(hit / n)} |")
    lines += [
        "",
        "### Each signal on its own",
        "",
        "| Signal | Measured on | Recall | False positives |",
        "|---|---:|---:|---:|",
    ]
    for name in SIGNALS:
        have = np.array([r["signals"].get(name) is not None for r in records])
        if not have.any():
            lines.append(f"| {name} | 0 | not measured | not measured |")
            continue
        vals = np.array([r["signals"][name] if r["signals"].get(name) is not None else 1.0 for r in records])
        flagged = vals < SIGNAL_FLAG_BELOW[name]
        w, c = have & ~correct, have & correct
        lines.append(
            f"| {name} | {int(have.sum())} | {_pct(flagged[w].mean() if w.any() else None)} "
            f"| {_pct(flagged[c].mean() if c.any() else None)} |"
        )
    if not live and all("oracle_agrees" in r for r in records):
        agrees = np.array([r["oracle_agrees"] for r in records])
        wrong = ~correct
        lines += [
            "",
            "### Comparator ceiling (oracle)",
            "",
            "If the second approach were a perfect independent solution, the result comparison alone would flag "
            f"**{int((~agrees[wrong]).sum())} of {int(wrong.sum())}** wrong variants and wrongly flag "
            f"**{int((~agrees[correct]).sum())} of {int(correct.sum())}** correct rewrites. This measures the "
            "canonical comparison (aliases, column and row order, tolerance), not the language model: how often a "
            "real second approach is itself right is what `--live` measures.",
        ]
    coarse, fine = reliability(scores, correct, COARSE_EDGES), reliability(scores, correct, FINE_EDGES)
    lines += [
        "",
        "## Reliability (share of SQL in each confidence bucket that is actually correct)",
        "",
        *_table(coarse),
        "",
        f"Monotonic (accuracy never falls as confidence rises): **{'yes' if is_monotonic(coarse) else 'NO'}** "
        f"(coarse), **{'yes' if is_monotonic(fine) else 'NO'}** (5 buckets).",
        "",
        *_table(fine),
        "",
        "Note: this set is 3 to 1 wrong:correct by construction, so absolute accuracy per bucket is lower than it "
        "would be on real traffic. Monotonicity and the ordering of buckets are what carry over; re-run the "
        "reliability table on real question logs once they exist.",
    ]
    high = coarse[-1]
    lines += [
        "",
        f"High bucket (at least 0.8): {high['n']} variants, accuracy {_pct(high['accuracy'])} "
        "(target: at least 90%).",
    ]
    if fitted and cv:
        w, a = fitted
        lines += [
            "",
            "## Weights",
            "",
            "| Signal | PRD initial | Fitted |",
            "|---|---:|---:|",
            *[f"| {n} | {cfg.weights.get(n, 0):.2f} | {w.get(n, 0):.2f} |" for n in SIGNALS],
            "",
            f"Fitted by grid search (step {WEIGHT_STEP}, floor {WEIGHT_FLOOR} per signal), maximising AUC; ties go to "
            f"the weights closest to the initial ones. In-sample AUC {a:.3f}. 5-fold cross-validated AUC: fitted "
            f"{cv['fitted_cv_auc']:.3f} vs initial weights {cv['initial_weights_auc']:.3f}.",
            "",
            "Adopt the fitted weights only if the cross-validated AUC is not worse than the initial weights; with this "
            'few variants a tie means "keep the PRD weights".',
        ]
    elif not live:
        lines += ["", "## Weights", "", "Not fitted: the LLM-backed signals were not measured in this run."]
    return "\n".join(lines) + "\n"


# =============================================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--live", action="store_true")
    ap.add_argument(
        "--limit", type=int, default=0, help="only the first N variants (spreads wrong/correct evenly)"
    )
    ap.add_argument("--workers", type=int, default=2, help="concurrent variants for --live")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args(argv)

    variants = load_variants()
    if args.limit:
        variants = variants[:: max(1, len(variants) // args.limit)][: args.limit]
    t2s, cfg, validator = build_stack(args.live)
    suffix = "" if args.live else ".offline"
    args.out.mkdir(parents=True, exist_ok=True)

    if args.live:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            records = list(pool.map(lambda v: evaluate_live(t2s, validator, v), variants))
    else:
        refs: dict = {}
        records = [evaluate_offline(t2s, cfg, v, refs) for v in variants]

    (args.out / f"signals{suffix}.jsonl").write_text(
        "\n".join(json.dumps(r, default=str) for r in records) + "\n", encoding="utf-8"
    )
    fitted = cv = None
    if args.live:
        fitted = fit_weights(records, cfg)
        cv = cross_validated_auc(records, cfg)
        (args.out / "weights.fitted.yaml").write_text(
            yaml.safe_dump({"calibrated": False, "weights": {k: round(v, 2) for k, v in fitted[0].items()}}),
            encoding="utf-8",
        )
    report = build_report(records, cfg, mode="live" if args.live else "offline", fitted=fitted, cv=cv)
    (args.out / f"report{suffix}.md").write_text(report, encoding="utf-8")
    print(report)
    tally = Counter(r["label"] for r in records)
    print(f"wrote {args.out} ({dict(tally)})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
