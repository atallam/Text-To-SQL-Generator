# Confidence calibration report (live)

Generated 2026-10-30 from 54 labeled variants (40 wrong, 14 correct) on the seeded WealthLens database.

## Detection (flagged = confidence below 0.8, i.e. not in the high bucket)

* **Recall on wrong SQL: 100%** (target: at least 80%)
* False-positive rate on correct SQL: 14%
* AUC (correct ranked above wrong): 0.966

| Wrong-SQL category | Variants | Flagged | Recall |
|---|---:|---:|---:|
| wrong_join | 9 | 9 | 100% |
| wrong_filter | 10 | 10 | 100% |
| wrong_aggregation | 10 | 10 | 100% |
| wrong_date_range | 11 | 11 | 100% |

### Each signal on its own

| Signal | Measured on | Recall | False positives |
|---|---:|---:|---:|
| backtranslation | 54 | 92% | 7% |
| multiquery | 54 | 100% | 7% |
| sanity | 54 | 25% | 0% |
| schema_coverage | 54 | 0% | 0% |
| syntax | 54 | 0% | 0% |

## Reliability (share of SQL in each confidence bucket that is actually correct)

| Confidence | Variants | Correct | Accuracy |
|---|---:|---:|---:|
| 0.0 to 0.5 | 2 | 0 | 0% |
| 0.5 to 0.8 | 40 | 2 | 5% |
| 0.8 to 1.0 | 12 | 12 | 100% |

Monotonic (accuracy never falls as confidence rises): **yes** (coarse), **yes** (5 buckets).

| Confidence | Variants | Correct | Accuracy |
|---|---:|---:|---:|
| 0.0 to 0.2 | 0 | 0 | n/a |
| 0.2 to 0.4 | 0 | 0 | n/a |
| 0.4 to 0.6 | 41 | 1 | 2% |
| 0.6 to 0.8 | 1 | 1 | 100% |
| 0.8 to 1.0 | 12 | 12 | 100% |

Note: this set is 3 to 1 wrong:correct by construction, so absolute accuracy per bucket is lower than it would be on real traffic. Monotonicity and the ordering of buckets are what carry over; re-run the reliability table on real question logs once they exist.

High bucket (at least 0.8): 12 variants, accuracy 100% (target: at least 90%).

## Weights

| Signal | PRD initial | Fitted |
|---|---:|---:|
| backtranslation | 0.30 | 0.10 |
| multiquery | 0.25 | 0.50 |
| sanity | 0.20 | 0.20 |
| schema_coverage | 0.15 | 0.10 |
| syntax | 0.10 | 0.10 |

Fitted by grid search (step 0.05, floor 0.05 per signal), maximising AUC; ties go to the weights closest to the initial ones. In-sample AUC 0.999. 5-fold cross-validated AUC: fitted 0.993 vs initial weights 0.966.

Adopt the fitted weights only if the cross-validated AUC is not worse than the initial weights; with this few variants a tie means "keep the PRD weights".
