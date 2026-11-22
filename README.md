# Text-To-SQL-Generator

Natural-language questions to SQL, with layered guardrails, hallucination detection and a decomposed
confidence score. Production-tier build; see `PRD.md` for scope, phases and decisions.

**Status:** Phases 0-3 complete (foundation, schema-aware prompts, guardrails, validation and confidence); Phase 4 (API + Streamlit UI) added, pending a first end-to-end run on your machine. Eval numbers will lead this README after Phase 6.

## Quick start (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,evals,embeddings]"   # embeddings = bge-small relevance filter
Copy-Item .env.example .env        # then fill in ANTHROPIC_API_KEY and passwords
docker compose up -d db
python -m scripts.seed_wealthlens   # schema + synthetic data + read-only role
pytest -q                           # integration tests need T2S_TEST_ADMIN_URL
```

For the integration tests (read-only role proof), point `T2S_TEST_ADMIN_URL` at a throwaway Postgres:

```powershell
$env:T2S_TEST_ADMIN_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
pytest -q
```

## Layout

| Path | Purpose |
|---|---|
| `text2sql/config` | Settings and per-source config loader |
| `text2sql/connectors` | Database-agnostic connector interface + Postgres/Supabase |
| `text2sql/db` | Schema, seed and read-only role bootstrap |
| `text2sql/schema` | Introspection, schema snapshot + fingerprint, glossary, relevance filter, refresh provider |
| `text2sql/generation` | Ambiguity handler, few-shot selector, `ContextBuilder` (question -> prompt or clarification) |
| `text2sql/prompts` | Versioned Jinja2 templates (generation, repair, describe, judge, alternative) |
| `text2sql/guardrails` | AST allow-list rules (G000-G008), EXPLAIN cost gate; rules in `text2sql/config/guardrails.yaml` |
| `text2sql/execution` | Read-only sandbox executor: capped DataFrame result, timings, result hash |
| `text2sql/validation` | Phase 3: back-translation judge, result sanity checks, multi-query validation, confidence scorer; weights in `text2sql/config/confidence.yaml` |
| `text2sql/pipeline.py` | `Text2SQL.ask()` / `run_sql()`: the whole path, with a LangGraph repair loop |
| `text2sql/llm` | LLM client: retry, Pydantic-validated structured output, tracing |
| `text2sql/observability` | Structured logging and LLM call tracing |
| `api` | Phase 4: FastAPI service (`/v1/query`, `/v1/query/run-sql`, `/v1/schema`, `/v1/history`, `/v1/feedback`, `/healthz`) |
| `app` | Phase 4: Streamlit UI (confidence badge, editable SQL, clarification cards, history, feedback) |
| `docker` | One image for API + UI; see `docker-compose.yml` |
| `sources/<name>` | Per-source bundle: `source.yaml`, schema, glossary, examples |
| `scripts` | Seeding and utilities |

## Phase 1: schema-aware prompts

`ContextBuilder.prepare(question)` returns either a ready `PromptBundle` (system + user prompt holding only the
relevant tables, glossary definitions and few-shot examples) or a `ClarificationRequest` for ambiguous terms such
as "net worth" or "returns". No LLM call is made in this phase.

```powershell
python -m scripts.dump_schema --source wealthlens --out tests/fixtures/wealthlens_schema.json
python -m evals.table_recall --embedder sentence-transformers   # table-recall gate (>= 95%)
python -m evals.table_recall --embedder lexical                 # offline, no model download
$env:UPDATE_SNAPSHOTS = "1"; pytest tests/unit/test_prompts.py; Remove-Item Env:UPDATE_SNAPSHOTS   # refresh prompt snapshots
```

Measured on the 20-question WealthLens seed set (`evals/golden/wealthlens_seed.yaml`): bge-small 20/20 table
recall, lexical 19/20; ambiguity handling 12/12. The seed set is small and author-written, so treat it as a
smoke gate, not independent evidence. Sample values are never sent to the LLM unless a column is allow-listed
in `source.yaml`.

## Phase 2: generation and safety

```python
from text2sql.pipeline import Text2SQL

t2s = Text2SQL.from_source("wealthlens")
out = t2s.ask("How much did I spend on groceries last month?")
out.status        # success | needs_clarification | unanswerable | blocked | failed
out.sql           # the guardrail-approved SQL that ran
out.execution.to_dataframe()
```

```powershell
python -m scripts.ask "How much did I spend on groceries last month?"
python -m scripts.ask --sql "SELECT name FROM accounts"      # your own SQL, same guardrails
python -m evals.redteam                                       # 77 dangerous + 20 harmless queries, offline
python -m evals.redteam --live                                # + EXPLAIN gate and database-role bypass probe (dev DB only)
python -m evals.run_evals --guardrail text2sql.guardrails:check_sql
```

Five layers: input hygiene, AST rules, EXPLAIN cost gate, `READ ONLY` transaction with timeout, SELECT-only
role. Design, rule table and known limits are in `docs/Guardrails.md`. Results on the red-team suite: 77/77
blocked by the guardrail layer, 20/20 harmless look-alikes allowed; with the guardrail bypassed the database
role alone stops 61 of the 74 probed queries and the remaining 13 (catalog reads, `set_config`, ...) are the
reason layer 2 exists. Prompt-injection scenarios use a deliberately compromised model that obeys every
injected instruction: nothing unsafe executes.

## Phase 3: confidence and hallucination detection

```python
out = t2s.ask("How much did I spend on groceries last month?")
out.confidence                      # 0.93
out.validation.confidence.level     # high | medium | low | cannot_answer
out.validation.confidence.signals   # five signals, each with value, weight and a one-line reason
out.validation.alternative          # the independent second query, shown when it disagrees
```

Five signals (initial weights from the PRD): back-translation alignment 0.30 (a blind describe-then-judge pair
on the Haiku-class model), multi-query agreement 0.25, result sanity pass rate 0.20, schema coverage 0.15,
first-pass syntax 0.10. Hard caps: the two approaches disagree -> at most 0.5; a guardrail warning -> at most 0.6;
unanswerable -> "cannot answer". A signal that could not be measured drops out and the rest are renormalised.
Details, limits and the calibration procedure are in `docs/Confidence.md`.

```powershell
python -m scripts.ask "How much did I spend on groceries last month?"   # prints the confidence breakdown
python -m scripts.ask --no-validate "..."                               # Phase 2 behaviour, fewer model calls
python -m evals.calibrate --offline        # no API key: deterministic signals only (not a calibration)
python -m evals.calibrate --live           # needs ANTHROPIC_API_KEY: the run that tunes the weights
```

Status: 40 wrong and 14 correct SQL variants are committed in `evals/validation/variants.yaml`. The weights are
still the PRD's initial ones (`calibrated: false`); the live calibration run has not been done yet.

## Phase 4: API and UI

Setup once (PowerShell), then run the two processes in separate terminals:

```powershell
pip install -e ".[dev,api,ui]"
# .env needs ANTHROPIC_API_KEY, API_KEYS=<random string>, API_KEY=<the same string>
python -c "import secrets; print(secrets.token_urlsafe(32))"   # generate a key

uvicorn api.main:app --reload --port 8000        # API docs: http://localhost:8000/docs
streamlit run app/streamlit_app.py               # UI:       http://localhost:8501
```

Or everything in containers (Postgres, seed, API, UI): `docker compose up --build`.

- **Auth and limits:** `X-API-Key` header, per-key rate limit (`API_RATE_LIMIT_PER_MINUTE`), request size limit, CORS allow-list. With no `API_KEYS` set the API refuses requests instead of running open.
- **Edited SQL:** `POST /v1/query/run-sql` re-runs SQL a person changed through the same guardrails, sandbox and read-only role. It is not confidence-scored.
- **Feedback flywheel with a human gate:** thumbs-up becomes a *candidate few-shot example*, thumbs-down a *candidate eval case*. Nothing changes answers until an admin (`API_ADMIN_KEYS`) approves it via `/v1/feedback/{id}/review`. Approved examples are re-checked by the guardrails and written to `sources/<name>/examples.approved.yaml`; approved eval cases go to `evals/feedback/<name>.jsonl` for you to review and move into the golden set.
- **History** lives in `data/app.db` (SQLite, git-ignored), per session.

## Second source: procurement (synthetic)

A direct-materials procurement database (suppliers, parts, POs and lines, PO changes/pushouts, ASN shipments,
receipts, inventory, RMAs; 280 POs ending 2026-09-30). It is a config-only source: `sources/procurement/` plus a
seed generator, no changes to the pipeline.

```powershell
# .env: copy the PROCUREMENT_* lines from .env.example; use your own Postgres admin password in PROCUREMENT_ADMIN_URL
python -m scripts.seed_source procurement   # creates the database if missing, schema, data, read-only role
# restart uvicorn, then pick "Procurement" in the UI's Data source box
python -m scripts.ask --source procurement "Which suppliers had the most pushouts in the last 90 days?"
```

`docker compose up --build` seeds both sources. The glossary asks for clarification on "spend" (ordered vs received value)
and "on time" (against the promised vs the originally requested date). It also tells the model to attribute anything to a
supplier through `purchase_orders.supplier_id`, not `supplier_parts` (the part catalog).

## Evaluation data (Hugging Face)

Test data comes from [`gretelai/synthetic_text_to_sql`](https://huggingface.co/datasets/gretelai/synthetic_text_to_sql).
Every row ships its own mini database, so answers are scored by **executing** SQL, not by comparing text.
The builder keeps supply-chain-adjacent domains (logistics, maritime, manufacturing, retail, ...) and writes
three kinds of cases: answerable questions with verified results, unanswerable questions (a real question
against a schema that cannot answer it), and unsafe write/DDL requests that must never execute.

```powershell
pip install -e ".[dev,evals]"
python -m evals.build_golden --answerable 60 --unanswerable 20 --unsafe 20   # optional: the file is committed
python -m evals.run_evals --predictor oracle     # harness sanity check, must be 100% / 0 unsafe executed
python -m evals.run_evals --predictor claude     # one-shot Claude baseline (needs ANTHROPIC_API_KEY)
```

New to evals? Start with [`docs/Evals-101.md`](docs/Evals-101.md).

Reports land in `evals/reports/`. To score the real pipeline, add a predictor in `evals/predictors.py` and pass
the Phase 2 guardrail with `--guardrail text2sql.guardrails:check_sql`. The run exits 1 if any unsafe statement executes.

## Safety model (built up across phases)

1. SQL guardrails (AST-based allow-list) - Phase 2
2. Read-only transaction + statement timeout - **done** (`readonly_connection`)
3. SELECT-only database role - **done** (`ensure_readonly_role`)
4. Answer validation and confidence score - Phase 3 (weights not yet calibrated on live model output)

Demo data is fully synthetic. Never point the LLM at real personal data without setting column
sample-value policy in `source.yaml`.
