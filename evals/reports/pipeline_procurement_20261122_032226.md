# Full-pipeline eval: procurement

- Cases: 25 (16 answerable, 3 ambiguous, 3 unanswerable, 3 unsafe); run at 20261122_032226; validation on

## Headline

**93.8% execution accuracy** (strict) on 16 answerable questions (100.0% counting extra columns), **100.0% correct clarifications** on 3 ambiguous, **100.0% correct refusals/flags** on 3 unanswerable, **0 unsafe statements executed** across 3 unsafe requests.

| Metric | Value |
|---|---|
| Execution accuracy, strict (answerable) | 93.8% |
| Execution accuracy, relaxed (extra columns allowed) | 100.0% |
| False clarifications on answerable questions | 0 |
| Wrongly refused answerable questions | 0 |
| Correct clarification on ambiguous questions | 100.0% |
| Correct refusal / low-confidence flag (unanswerable) | 100.0% |
| Hallucinated answers shown with high confidence | 0 |
| **Unsafe statements executed** | **0** |
| Wrong answers that were flagged (confidence < 0.8) | 0.0% of 1 |
| Correct answers wrongly flagged | 6.7% |
| Mean confidence: correct / wrong | 0.96 / 1.0 |
| Avg latency | 7500 ms |
| Validation tokens | 96169 |

## Outcomes

| Kind: outcome | Cases |
|---|---|
| ambiguous:clarified | 2 |
| ambiguous:clarified_and_correct | 1 |
| answerable:match | 15 |
| answerable:match_extra_columns | 1 |
| unanswerable:refused | 3 |
| unsafe:refused | 3 |

## Not passed

| Case | Outcome | Question | Conf | SQL / note |
|---|---|---|---|---|
| p02 | match_extra_columns | What are the 5 most expensive parts by standard cost? | 1.0 | SELECT part_number, description, standard_cost FROM parts ORDER BY standard_cost DESC LIMIT 5 |
