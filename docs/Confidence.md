# Confidence and hallucination detection (Phase 3)

A query can be valid, safe and still answer the wrong question. Phase 3 adds a **confidence score (0 to 1)**
and the evidence behind it to every successful answer.

```python
out = t2s.ask("How much did I spend on groceries last month?")
out.confidence                       # 0.93
out.validation.confidence.level      # "high" | "medium" | "low" | "cannot_answer"
out.validation.confidence.signals    # the five signals with value, weight and a one-line reason
out.validation.alternative           # the independent second query, shown when it disagrees
```

The model's own `self_confidence` is deliberately **not** an input: self-reported confidence is poorly calibrated.

## The five signals

| Signal | Weight | What it measures |
|---|---:|---|
| `backtranslation` | 0.30 | One call describes the SQL in plain English **without seeing the question**. A second call grades that description against the question **without seeing the SQL**. |
| `multiquery` | 0.25 | An independently written second SQL (different join path or aggregation) returns the same data. |
| `sanity` | 0.20 | Pass rate of deterministic checks on the result (below). |
| `schema_coverage` | 0.15 | The SQL stays inside the tables the question retrieved, and matches the tables and columns the model said it used. |
| `syntax` | 0.10 | 1.0 if the first attempt passed every gate; 0.6 / 0.3 / 0 after one / two / more repairs. |

`score = sum(weight x value) / sum(weight)` over the signals that **were measured**. A skipped or failed signal
drops out and the others are renormalised, so a missing signal never counts as a pass or a fail.
`signals_used` tells you how many of the five the score rests on.

**Hard caps** (applied after the weighted sum):

* The two approaches returned different data: at most **0.5**.
* A guardrail warning fired: at most **0.6**. G006 ("a LIMIT was added") is ignored unless that limit actually cut the result short, because it fires on most queries.
* The question is unanswerable: score 0, level `cannot_answer`, nothing else is consulted (and no validation model calls are made).

**Levels:** high at least 0.8, medium at least 0.5, low below.

### Back-translation

The score is derived from rubric grades, not from a number the model chooses: the judge grades `entity`,
`measure`, `filters`, `time_range`, `grouping`, `output_shape` as match / partial / mismatch / not_applicable.
Match is 1, partial 0.5, mismatch 0; not_applicable dimensions are ignored; any mismatch caps the alignment at 0.6.
Both calls use `LLM_MODEL_JUDGE` (default `claude-haiku-5-5`).

### Multi-query validation

Runs only above a complexity threshold (joins + subqueries/CTEs + windows + GROUP BY + HAVING, default 3), because
it costs one extra generation call and one extra query. The second SQL takes the same path as the first: guardrails,
EXPLAIN gate, read-only executor. Results are compared after canonicalisation: column order and aliases, row
order and numeric noise (tolerance 0.01 / 0.01%) do not matter; a result with extra columns still agrees if the
shared columns match. If the second query cannot be produced or run, the signal is *unavailable* (not a
disagreement). Trade-off: the second prompt shows the first SQL so it can avoid copying its structure; that
can anchor the model, which is why the instruction tells it to re-derive the answer from the question.

### Sanity checks

| Check | Flags |
|---|---|
| `empty_result` | no rows |
| `null_heavy` | a result column mostly NULL (usually a bad join) |
| `count_bounds` | a COUNT that is negative or larger than its table |
| `sum_bounds` | a SUM over a whole table's column larger than the column's own total |
| `date_range` | a date filter outside the data span (just outside = warn, because data can lag; far outside = fail) |
| `join_fanout` | a many-to-many join feeding SUM/AVG/COUNT, or a join that multiplied the rows |
| `duplicate_keys` / `duplicate_rows` | a primary key or most rows repeated |

Each is pass / warn / fail / skip. The pass rate scores pass 1, warn 0.5, fail 0, skip excluded. Table statistics
come from a few fixed read-only aggregate queries, cached per schema version.

## Configuration

`text2sql/config/confidence.yaml` holds weights, caps, levels and thresholds; a source can override pieces with
`validation: confidence.yaml` in its `source.yaml`. `calibrated: false` is shown to the user until the weights have
been tuned (below).

```python
Text2SQL.from_source("wealthlens", validate=False)   # Phase 2 behaviour, no extra model calls
```

Cost per answer: 2 judge-model calls (describe, grade), plus 1 generation-model call and 1 query when multi-query
applies. The two LLM-backed signals run in parallel.

## Calibration

`evals/validation/variants.yaml` has 12 questions with a reference SQL, **40 deliberately wrong variants** (9 wrong
join, 10 wrong filter, 10 wrong aggregation, 11 wrong date range) and **14 correct rewrites**. On the seeded
database every wrong variant returns different data from its reference and every correct rewrite agrees.

```powershell
# no API key: harness check, deterministic signals only (NOT a calibration)
python -m evals.calibrate --offline

# the exit-gate run: needs ANTHROPIC_API_KEY; runs the real judge and a real second approach on every variant
python -m evals.calibrate --live
```

It writes `evals/calibration/report.md` (recall, false-positive rate, per-category recall, per-signal recall,
reliability table with monotonicity check, fitted weights with cross-validated AUC) and `weights.fitted.yaml`.
Adopt the fitted weights only if cross-validated AUC is not worse than the initial weights, then set
`calibrated: true`. With about 50 variants, a tie means "keep the PRD weights".

**Phase 3 acceptance:** recall of at least 80% on the wrong variants; accuracy monotonic across confidence buckets;
the high bucket (at least 0.8) at least 90% correct.

### Status of the numbers

* Offline run (committed as `evals/calibration/report.offline.md`): sanity checks alone flag about a quarter of the
  wrong variants and none of the correct ones, and the result comparison would separate 40 of 40 wrong from
  0 of 14 correct **if** the second approach were right. Sanity alone cannot move a score below 0.8 (its weight is 0.20),
  so the offline recall at the 0.8 threshold is about zero by construction; the LLM-backed signals are what carry
  detection.
* **No live model has been run yet.** The back-translation prompts, the judge rubric and the weights are untested
  against real Claude output until `--live` is run, so the 80% recall target is **not yet demonstrated**.
