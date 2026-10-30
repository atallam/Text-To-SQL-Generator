# Confidence calibration report (offline)

Generated 2026-10-30 from 54 labeled variants (40 wrong, 14 correct) on the seeded WealthLens database.

> **OFFLINE RUN: not a calibration.** No LLM was called, so `backtranslation` (weight 0.30) and `multiquery`
> (weight 0.25) are missing and the score below rests on sanity checks, schema coverage and syntax alone
> (weights renormalised). Expect low recall: that is what the deterministic checks can catch by themselves.
> The Phase 3 exit gate (recall of at least 80%) needs `python -m evals.calibrate --live`.

## Detection (flagged = confidence below 0.8, i.e. not in the high bucket)

* **Recall on wrong SQL: 0%** (target: at least 80%)
* False-positive rate on correct SQL: 0%
* AUC (correct ranked above wrong): 0.625

| Wrong-SQL category | Variants | Flagged | Recall |
|---|---:|---:|---:|
| wrong_join | 9 | 0 | 0% |
| wrong_filter | 10 | 0 | 0% |
| wrong_aggregation | 10 | 0 | 0% |
| wrong_date_range | 11 | 0 | 0% |

### Each signal on its own

| Signal | Measured on | Recall | False positives |
|---|---:|---:|---:|
| backtranslation | 0 | not measured | not measured |
| multiquery | 0 | not measured | not measured |
| sanity | 54 | 25% | 0% |
| schema_coverage | 54 | 0% | 0% |
| syntax | 54 | 0% | 0% |

### Comparator ceiling (oracle)

If the second approach were a perfect independent solution, the result comparison alone would flag **40 of 40** wrong variants and wrongly flag **0 of 14** correct rewrites. This measures the canonical comparison (aliases, column and row order, tolerance), not the language model: how often a real second approach is itself right is what `--live` measures.

## Reliability (share of SQL in each confidence bucket that is actually correct)

| Confidence | Variants | Correct | Accuracy |
|---|---:|---:|---:|
| 0.0 to 0.5 | 0 | 0 | n/a |
| 0.5 to 0.8 | 0 | 0 | n/a |
| 0.8 to 1.0 | 54 | 14 | 26% |

Monotonic (accuracy never falls as confidence rises): **yes** (coarse), **yes** (5 buckets).

| Confidence | Variants | Correct | Accuracy |
|---|---:|---:|---:|
| 0.0 to 0.2 | 0 | 0 | n/a |
| 0.2 to 0.4 | 0 | 0 | n/a |
| 0.4 to 0.6 | 0 | 0 | n/a |
| 0.6 to 0.8 | 0 | 0 | n/a |
| 0.8 to 1.0 | 54 | 14 | 26% |

Note: this set is 3 to 1 wrong:correct by construction, so absolute accuracy per bucket is lower than it would be on real traffic. Monotonicity and the ordering of buckets are what carry over; re-run the reliability table on real question logs once they exist.

High bucket (at least 0.8): 54 variants, accuracy 26% (target: at least 90%).

## Weights

Not fitted: the LLM-backed signals were not measured in this run.
