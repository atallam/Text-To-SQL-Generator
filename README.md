# Text-To-SQL-Generator

Natural-language questions to SQL, with layered guardrails, hallucination detection and a decomposed
confidence score. Production-tier build; see `PRD.md` for scope, phases and decisions.

**Status:** Phase 0 (foundation) and Phase 1 (schema-aware prompt engine) complete. Eval numbers will lead this README after Phase 6.

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
| `text2sql/prompts` | Versioned Jinja2 templates (`sql_generation.v1.*`) |
| `text2sql/llm` | LLM client: retry, Pydantic-validated structured output, tracing |
| `text2sql/observability` | Structured logging and LLM call tracing |
| `sources/<name>` | Per-source bundle: `source.yaml`, schema, glossary |
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

Demo data is fully synthetic. Never point the LLM at real personal data without setting column
sample-value policy in `source.yaml`.
