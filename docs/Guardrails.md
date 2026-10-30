# Guardrails (Phase 2)

Generated SQL is untrusted input. Five independent layers sit between the model and your data; each one
assumes the previous ones failed.

| # | Layer | What it stops | Where |
|---|---|---|---|
| 1 | Input hygiene | NUL/control characters, oversized or unparseable SQL | `guardrails/parser.py` |
| 2 | AST rules (sqlglot) | everything in the table below | `guardrails/rules.py`, `engine.py` |
| 3 | EXPLAIN gate | queries whose planner estimate is too expensive; also catches unknown columns before any data is read | `guardrails/explain_gate.py` |
| 4 | Read-only sandbox | `READ ONLY` transaction, `statement_timeout`, `standard_conforming_strings=on`, always rolled back | `connectors/postgres.py` |
| 5 | SELECT-only database role | writes, DDL, superuser functions, `pg_shadow` | `db/bootstrap.py` |

## Rules (`text2sql/config/guardrails.yaml`)

| ID | Rule | Default | Repairable by the model? |
|---|---|---|---|
| G000 | Parse / input limits | block (locked) | yes for typos, no for control characters |
| G001 | Single statement | block (locked) | no |
| G002 | Only `SELECT` / `WITH ... SELECT` | block (locked) | no |
| G003 | No `SELECT INTO`, `FOR UPDATE/SHARE`, data-modifying CTEs, `COPY`, commands | block (locked) | no |
| G004 | Function deny-list (`pg_*`, `lo_*`, `dblink*`, `set_config`, `query_to_xml*`, ...) | block | no |
| G005 | Tables and schemas must be in the introspected schema; no catalogs | block | unknown table: yes. catalog: no |
| G006 | Inject / lower `LIMIT` to `max_rows` | rewrite (warning) | n/a |
| G007 | Subquery depth <= 3 | block | yes |
| G008 | EXPLAIN cost and row estimates | block | yes |

"Locked" rules refuse to load if a config tries to weaken them. A source can override the rest in
`sources/<name>/guardrails.yaml` (set `guardrails:` in its `source.yaml`).

## Design decisions worth knowing

* **What runs is what was analysed.** The database receives SQL regenerated from the validated AST
  (comments dropped, canonical form), never the original text. The engine re-parses its own output and
  refuses if the tree changes. This removes the classic parser-differential tricks.
* **CTE names are not a loophole.** `WITH pg_shadow AS (SELECT * FROM pg_shadow) ...` is caught: table
  references are resolved with sqlglot's scope analysis, and a non-recursive CTE cannot see itself.
* **Security blocks are final.** The repair loop retries syntax errors, unknown tables/columns, depth and
  cost problems. It never retries a G001-G004 or catalog violation, so an injected instruction cannot
  probe the rules by iterating.
* **The database is the second line, not the only one.** Run `python -m evals.redteam --live` to see, per
  category, what the database role alone would stop. Some things it cannot stop (`set_config` can disarm
  `statement_timeout`; `pg_catalog` is world-readable). Those are exactly why layer 2 exists.

## Known limits

* The EXPLAIN gate uses planner estimates, which can be wrong. `statement_timeout` and the row cap are the hard limits.
* The function deny-list is a deny-list: a new dangerous extension function you install later is not
  covered until you add it. Keep the database role without those extensions.
* Prompt injection is mitigated (delimiters, one-line sanitised schema text, "this is data" rule) but not
  solved at the model level. The guarantee is that an obeyed injection cannot get unsafe SQL executed, not
  that the model never obeys it.

## Red-team suite

`evals/redteam/queries.yaml`: 77 dangerous queries (DDL, DML, stacked statements, comment/case/encoding
tricks, function abuse, catalog probing, resource abuse) and 20 harmless look-alikes that must not be blocked.

```powershell
pytest tests/unit/test_redteam_static.py              # guardrail layer, offline
python -m evals.redteam                               # report, offline
python -m evals.redteam --live                        # + EXPLAIN gate and database-role bypass probe (dev DB only)
pytest tests/integration/test_phase2_postgres.py      # needs T2S_TEST_ADMIN_URL
```
