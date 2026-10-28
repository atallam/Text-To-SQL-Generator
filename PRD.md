# PRD: Text-to-SQL Interface with Guardrails

| | |
|---|---|
| **Owner** | Avinash Tallam |
| **Status** | v0.3 - Phase 0 complete (see section 9, Phase 0 notes); Phase 1 next |
| **Tier** | Production |
| **Repo name** | `Text-To-SQL-Generator` (Python package: `text2sql`) |
| **Date** | 2026-10-25 (v0.2) |
| **Source docs** | `Build Guide.md`, `Tech Stack.md` |

---

## 1. Summary

A natural-language interface that turns plain-English questions into SQL, runs them against a real database behind layered safety guardrails, checks that the SQL actually answers the question asked, and shows every result with a decomposed confidence score.

**Positioning (portfolio + learning):** "Safety first." The headline claim is *zero unsafe queries executed*, followed by execution accuracy and hallucination-detection rate, all backed by a reproducible eval suite.

**Personal goals this project serves:** hands-on practice with agentic patterns (bounded self-repair loop, LLM-as-judge, independent multi-path verification), Python-first code, strong UX, and multi-source support, with **WealthLens data as the first target database** and a planned switch to a procurement / supply chain database (fits the SCAIX portfolio). Because the target will change, the system must be database-agnostic by design.

## 2. Problem and Opportunity

- Business users (planners, buyers, analysts) cannot write SQL; analysts become a bottleneck.
- Naive text-to-SQL demos fail in production for three reasons: they execute unsafe SQL, they return confident wrong answers, and they cannot say "I can't answer that".
- This project addresses exactly those three failure modes, measurably.

## 3. Goals and Non-Goals

### Goals
1. Answer English questions over a relational schema with high execution accuracy.
2. Make unsafe execution structurally impossible (defense in depth, not a single check).
3. Detect and surface likely-wrong SQL before the user trusts it.
4. Show confidence with a transparent breakdown.
5. Improve over time via user feedback (the flywheel).
6. Ship as a reproducible, containerized, documented portfolio project with published eval numbers.

### Non-Goals (v1)
- Write operations, DDL, or any data modification, ever.
- Multi-turn conversational memory beyond "re-ask / refine" within a session.
- Fine-tuning models.
- Enterprise auth, SSO, multi-tenant row-level security (a basic API key is enough).
- Supporting non-SQL sources.

## 4. Users and Use Cases

| Persona | Need | Example question |
|---|---|---|
| Business user (buyer / planner) | Self-serve answers, trust signal | "Which suppliers delivered late more than 3 times last quarter?" |
| Power user / analyst | See, edit and re-run SQL | Tweaks generated SQL, re-runs through guardrails |
| Reviewer (recruiter / hiring manager) | Quickly see safety + accuracy | Reads README eval numbers, watches 4-min demo |

## 5. Success Metrics (targets, to be confirmed after baseline)

| Metric | Target | Measured by |
|---|---|---|
| Execution accuracy (answerable questions) | >= 85% | Golden set, result-set match |
| Unsafe queries executed | **0** across all adversarial cases | Guardrail + red-team suite |
| Hallucination detection (flags bad SQL) | >= 80% recall at <= 15% false-positive | Labeled good/bad SQL set |
| Correct refusal on unanswerable questions | >= 90% | Golden set unanswerable slice |
| Confidence calibration | High-confidence bucket (>= 0.8) at >= 90% correct | Reliability table on golden set |
| P95 latency, simple question | < 8 s | API timing logs |
| Cost per question (avg) | Tracked and published | Token logs |

## 6. Scope: Feasibility Notes That Shaped This PRD

The Build Guide is implementable as written. These adjustments are baked into the plan below:

1. **Guardrails use AST parsing (`sqlglot`), not regex or `sqlparse` alone.** `sqlparse` tokenizes but does not build a reliable tree for depth checks or function allow-listing.
2. **Row-count estimates from `EXPLAIN` are planner guesses.** Treat them as a coarse cost gate, and back them with `statement_timeout` and a hard result cap.
3. **LLM self-reported confidence is poorly calibrated.** The combined score must be calibrated against the golden set, and the weights tuned from data.
4. **SQL exact match is a weak metric** (many valid SQL forms). Execution match is the primary accuracy metric; exact match is informational.
5. **Embeddings:** Anthropic does not offer an embeddings model. Use a local `sentence-transformers` model (free, offline, CI-friendly) or an API such as Voyage. **Decision: local `sentence-transformers` (see section 9.1).**
6. **Supabase vs. docker-compose Postgres:** the Build Guide seeds Postgres in compose, the stack names Supabase. **Decision:** Supabase is the primary database (WealthLens already runs on it). Compose Postgres is retained only for CI and reproducible evals, and as a second source for multi-source testing.
7. **Supabase connection mode matters:** use direct or session-pooler connections. The transaction pooler (port 6543) does not support session-level settings such as `SET TRANSACTION READ ONLY` and `statement_timeout` reliably.
8. **Prompt-injection surface:** sample values and column descriptions pulled from the DB go into the prompt. Treat them as untrusted (truncate, sanitize, delimit).
9. **Cost and latency:** back-translation plus a second independent SQL means ~4 LLM calls per question. Use a smaller model (Haiku-class) for judge tasks and run steps in parallel; run multi-query validation only above a complexity threshold.

## 7. Architecture

```
            +-------------------+
 Question ->|  FastAPI /v1/query|
            +---------+---------+
                      v
        +-------------+--------------+
        | 1. Schema context builder  |  SQLAlchemy introspection + embeddings filter
        |    + glossary + few-shots  |  (Jinja2 prompt templates, versioned)
        +-------------+--------------+
                      v
        +-------------+--------------+
        | 2. Ambiguity check         |--> clarification request (stop)
        +-------------+--------------+
                      v
        +-------------+--------------+
        | 3. SQL generator (Claude)  |  structured output via instructor + Pydantic
        |    bounded repair loop     |  (max_iterations = 3, error fed back)
        +-------------+--------------+
                      v
        +-------------+--------------+
        | 4. Guardrail middleware    |--> blocked + reason logged (stop)
        |    (sqlglot AST + EXPLAIN) |
        +-------------+--------------+
                      v
        +-------------+--------------+
        | 5. Sandboxed executor      |  READ ONLY txn, SELECT-only role,
        |    -> DataFrame            |  statement_timeout, rollback always
        +-------------+--------------+
                      v
        +-------------+--------------+
        | 6. Validation layer        |  back-translation judge, result sanity,
        |                            |  independent 2nd SQL + compare
        +-------------+--------------+
                      v
        +-------------+--------------+
        | 7. Confidence scorer       |  weighted signals + hard caps
        +-------------+--------------+
                      v
          Response -> Streamlit UI -> feedback -> eval/few-shot stores
```

### Module layout (keeps LLM calls separate from business logic)
```
text2sql/
  config/            settings.py, guardrails.yaml, stack_config
  prompts/           *.j2 / *.yaml (versioned)
  llm/               client.py (retry+backoff, Pydantic validation, token logging)
  schema/            introspect.py, relevance.py, glossary.yaml
  generation/        generator.py, ambiguity.py, repair_loop.py
  guardrails/        parser.py, rules.py, explain_gate.py
  execution/         sandbox.py, executor.py
  validation/        backtranslate.py, sanity.py, multiquery.py, confidence.py
  api/               main.py, routes.py, models.py
  store/             history, feedback (app DB, separate from the target DB)
  connectors/        base.py, postgres.py (later: sqlite/duckdb)
ui/                  streamlit_app.py
evals/               golden/*.yaml, run_evals.py, redteam/*.yaml, reports/
tests/               mocked-LLM unit tests
docker/              Dockerfile.api, Dockerfile.ui, seed/*.sql
docker-compose.yml
```

## 8. Revised Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | Type hints, Pydantic v2 |
| LLM (generation) | Claude Sonnet 5.5 | Via Anthropic SDK |
| LLM (judge, back-translation) | Claude Haiku 5.5 (configurable) | Cheaper, run in parallel |
| Structured output | `instructor` + Pydantic v2 | Or native tool use; same schema |
| Reliability | `tenacity` | Retry + backoff on every LLM call |
| SQL parsing / safety | **`sqlglot`** (primary), `sqlparse` (syntax sanity) | Dialect-aware AST |
| DB access | SQLAlchemy 2.x + `psycopg` 3 | Introspection and execution |
| Database | **Supabase (PostgreSQL)** primary; compose Postgres for CI/evals | Direct or session-pooler connection; dedicated read-only role |
| Embeddings | **`sentence-transformers` local (`BAAI/bge-small-en-v1.5`)**; `pgvector` in Supabase for few-shot retrieval | Free, offline, CI-friendly; see 9.1 |
| Data handling | `pandas` | Result sets and comparison |
| API | FastAPI + Uvicorn | OpenAPI docs for free |
| Frontend | **Streamlit** (v1) | `streamlit-ace` for editable highlighted SQL |
| App store | Supabase schema `app` (separate from target data); SQLite for local tests | History, feedback, audit log |
| Observability | `structlog` + **Langfuse** | Tokens, latency, success per step, from day one |
| Testing | `pytest`, LLM mocked in CI | Golden set run separately, with real LLM |
| Config / secrets | `python-dotenv` locally | Secrets manager in prod |
| Containers | Docker + docker-compose | db, api, ui |
| Orchestration | **LangGraph** (generate -> guard -> execute -> validate -> repair graph) | Hard `max_iterations`; LLM calls stay out of business logic |

**Verdict on the original stack:** the core choices (Python, Claude, SQLAlchemy, FastAPI, custom guardrails, Docker) are right. The original table is missing parsing (`sqlglot`), structured output (`instructor`/Pydantic), embeddings, a frontend, an app datastore, observability, and testing. Added above.

## 9. Phased Delivery Plan

Effort is in hours, assuming AI-assisted coding at **2 hours/day (about 14 h/week)** and including review and testing. Each phase ends with an **exit gate**; do not start the next phase until it passes.

### Phase 0: Foundation and Data (14 h)
**Deliverables**
- Repo scaffold, config, logging, `.env` handling, pre-commit, CI skeleton (mocked LLM).
- Target database #1: **WealthLens** schema in Supabase (accounts, transactions, categories, holdings, budgets, goals etc.). Portfolio/demo uses a **synthetic seeded copy**, never real personal data (see 9.1).
- Target database #2 (later, Phase 5): synthetic procurement / supply chain schema (suppliers, parts, POs, PO lines, shipments/ASN, inventory, receipts, RMAs).
- **Connector interface defined now** (`connectors/base.py`) so no module hard-codes Postgres or WealthLens table names. Glossary, few-shots and guardrail allow-lists are per-source config files.
- Two DB roles: `app_admin` (seeding) and `readonly_user` (SELECT only, `statement_timeout`, locked `search_path`).
- Business glossary file started (e.g., "late" = receipt date > promised date).
- Baseline LLM client wrapper: retry, Pydantic validation, token and latency logging.

**Exit gate:** `docker compose up` brings up the stack; Supabase connection works with the read-only role; `readonly_user` provably fails on `INSERT/DROP`; one mocked-LLM test passes in CI.

**Phase 0 result (2026-10-28):** exit gate met in a local embedded-Postgres run: 23 tests pass, including 13 integration tests proving the read-only role cannot INSERT/UPDATE/DELETE/DROP/TRUNCATE/CREATE/ALTER, the sandbox blocks writes even with admin credentials, and the statement timeout fires. Not yet verified: docker compose on your machine, the Supabase connection, and CI on GitHub. Deferred to the next sessions: Langfuse adapter (tracer interface and structlog tracer are in place), pre-commit hooks, API-key auth (lands with the API in Phase 4).

### Phase 1: Schema-Aware Prompt Engine (18 h)
**Deliverables**
- SQLAlchemy introspection to a structured schema object: tables, columns and types, PK/FK, sample values for low-cardinality columns (sanitized, capped).
- Prompt constructor (Jinja2, versioned) with relevant schema, FKs, glossary, and 3-5 schema-specific few-shot examples.
- Relevance filter: embed question and table/column descriptions, include tables above threshold, always include FK-neighbors of selected tables.
- Ambiguity handler: structured clarification response listing interpretations with example SQL for each.

**Acceptance criteria**
- Schema context regenerates automatically after a DB change.
- Filter recall: required tables are in the filtered context for >= 95% of golden questions (measured on the early 20-question seed set).
- Ambiguous test questions ("revenue", "late", "top supplier") return clarification, not SQL.

**Exit gate:** table-recall metric hit; prompt snapshot tests committed.

### Phase 2: SQL Generation and Safety Layer (24 h)
**Deliverables**
- Generator returns Pydantic object: `sql`, `explanation`, `self_confidence`, `tables_used`, `columns_used`, `unanswerable` (bool + reason).
- **Bounded repair loop** (agentic element): on syntax/execution error, feed the error back to the model, `max_iterations = 3`, all attempts logged.
- Guardrail middleware (every rule configurable in `guardrails.yaml`, every block logged with rule ID and reason):

| Rule | Behavior |
|---|---|
| Single statement only | Reject `;`-chained statements |
| Statement allow-list | Only `SELECT` / `WITH ... SELECT`; reject DDL, DML, `COPY`, `SELECT INTO`, `FOR UPDATE`, data-modifying CTEs |
| Function deny-list | Block `pg_sleep`, `pg_read_file`, `lo_*`, `dblink`, admin functions |
| Table/schema allow-list | Only schemas/tables registered in the introspected schema; block `pg_catalog` / `information_schema` unless enabled |
| Row limit | Inject/lower `LIMIT` to configured max (default 1000) |
| Subquery depth | Reject depth > 3 (AST-measured) |
| EXPLAIN cost gate | Reject if planner-estimated rows or cost > threshold |
| Timeout | `statement_timeout` (default 10 s) |

- Sandbox: `BEGIN READ ONLY` transaction, always rolled back, executed as the SELECT-only role.
- Executor returns DataFrame (capped), rows returned, execution time, EXPLAIN plan, full audit log entry.

**Acceptance criteria**
- 100% of the 40+ dangerous-query fixtures blocked at the guardrail layer **and** (tested separately by bypassing the guardrail) rejected by the DB role.
- Prompt-injection fixtures (malicious text in a column value or question) do not result in executed unsafe SQL.

**Exit gate:** red-team suite green; zero unsafe executions.

### Phase 3: Hallucination Detection and Confidence (26 h)
**Deliverables**
- **Back-translation:** LLM describes what the SQL answers; an LLM judge (rubric, 0-1) scores alignment with the original question. Judge sees only the question and the SQL description, not the generation reasoning.
- **Result sanity checks:** NULL-heavy columns, empty results where unexpected, implausible aggregates (negative counts, totals greater than table totals), date range outside data span, row explosion after JOIN (fan-out check), duplicate keys.
- **Multi-query validation:** for questions above a complexity threshold, generate an independent second SQL (different JOIN or aggregation strategy), execute both, compare with canonicalization (column order/aliases, row order, numeric tolerance). Present both on disagreement.
- **Confidence scorer** (initial weights, then calibrated):

| Signal | Initial weight |
|---|---|
| Back-translation alignment | 0.30 |
| Multi-query agreement | 0.25 |
| Result sanity pass rate | 0.20 |
| Schema coverage (expected tables/columns) | 0.15 |
| Syntax validity / repair-free first pass | 0.10 |

  Hard caps: disagreement between approaches caps the score at 0.5; guardrail warning caps at 0.6; unanswerable flag short-circuits to "cannot answer".

**Acceptance criteria**
- Labeled set of >= 30 deliberately wrong SQL variants (wrong JOIN, wrong filter, wrong aggregation, wrong date range): detection recall >= 80%.
- Reliability table shows monotonic accuracy vs. confidence buckets.

**Exit gate:** calibration report committed; weights tuned from data, not guessed.

### Phase 4: Query Interface (22 h)
**Deliverables**
- API: `POST /v1/query` (SQL, results, confidence + breakdown, warnings, or clarification request), `GET /v1/schema`, `GET /v1/history`, `POST /v1/feedback`, `POST /v1/query/run-sql` (re-run user-edited SQL, same guardrails), `GET /healthz`.
- Streamlit UI: question box with example chips, SQL panel (syntax highlighted, editable, "re-run" through guardrails), sortable results table, prominent confidence badge with expandable signal breakdown, guardrail warnings in plain language, clarification card with one-click interpretation choice, history sidebar, thumbs up/down.
- Feedback flywheel: incorrect -> candidate eval case (pending review); correct -> candidate few-shot (pending review, then retrieved by similarity). **Human approval step** prevents feedback poisoning.

**UX principles:** never show a result without its confidence; make blocked queries explain *why* in plain language; empty states and loading states show what the system is doing (generating, checking safety, validating).

**Acceptance criteria:** end-to-end happy path, blocked path, clarification path, and low-confidence path all demonstrable in the UI; OpenAPI docs complete.

### Phase 5: Multi-Source Support (22 h)
- Implement a second connector (compose Postgres with the procurement / supply chain schema) and a SQLite/DuckDB connector for CSV/Parquet upload by non-technical users.
- Per-source config bundle: glossary, few-shot store, guardrail allow-lists, dialect (passed to `sqlglot`).
- UI source selector; schema, history and feedback are scoped per source.
- Each question targets exactly one source (cross-source joins are out of scope).
- **Acceptance:** the same pipeline, unchanged, passes a 15-question smoke set on WealthLens and on the supply chain DB; adding a source needs config only, no code changes in core modules.

### Phase 6: Evaluation Suite (20 h)
**Deliverables**
- Golden dataset, 50+ cases per source (target 60-70 for the primary source, 25+ for the others): simple lookups, multi-table JOINs, GROUP BY aggregations, date ranges, ambiguous phrasing, unanswerable questions. Each has verified SQL and expected result.
- Adversarial/red-team set: DDL/DML, stacked queries, comment tricks, case/encoding tricks, function abuse, catalog probing, prompt injection.
- Eval runner: execution match (primary), SQL exact match (informational), hallucination detection recall and false-positive rate, guardrail effectiveness, refusal accuracy, cost and latency, calibration table. Output to `evals/reports/` as Markdown + JSON.
- Docker-compose finalized: `db` (seeded), `api`, `ui`. README leads with eval numbers.

**Exit gate:** one command (`docker compose up`, then `python -m evals.run_evals`) reproduces the published numbers.

### Phase 7: Portfolio Polish (12 h)
- Demo video (< 4 min): NL -> SQL; guardrail blocks a destructive query; hallucination detector catches a bad translation; multi-query validation resolves a discrepancy.
- Narrative (README + LinkedIn post): "text-to-SQL with X% execution accuracy that blocked 100% of destructive operations and flagged Y% of hallucinated queries before they reached the user." Lead with safety.
- Architecture diagram, "design decisions" doc, limitations section.

### Timeline Summary (start Mon 2026-10-26, 14 h/week)

| Phase | Hours | Cumulative hours | Target finish |
|---|---|---|---|
| 0 Foundation + data + production scaffolding | 14 | 14 | 2026-11-01 |
| 1 Prompt engine | 18 | 32 | 2026-11-12 |
| 2 Generation + safety | 24 | 56 | 2026-11-29 |
| 3 Hallucination detection | 26 | 82 | 2026-12-17 |
| 4 Interface | 22 | 104 | 2026-12-31 |
| 5 Multi-source | 22 | 126 | 2027-01-15 |
| 6 Evals | 20 | 146 | 2027-01-27 |
| 7 Polish | 12 | 158 | 2027-02-04 |

About 11 weeks including a small buffer. Dates assume no gaps; slip is expected where phases hit their exit gates late. Switching the primary target to the supply chain DB is config-only after Phase 5.

## 9.1 Production-Tier Requirements, Embeddings and Dataset Strategy

### Production-tier requirements (apply from Phase 0)
- CI (lint, type-check, mocked-LLM tests) must pass before merge; golden eval runs on a schedule and on prompt changes.
- Langfuse tracing on every pipeline step (agent_name, tool_called, tokens, latency, success).
- Prompts in versioned files; every LLM call wrapped in retry+backoff and Pydantic-validated; hard `max_iterations` on the LangGraph repair loop.
- Secrets via `python-dotenv` locally and a secrets manager in prod; no keys in the repo.
- API key auth, per-key rate limiting, request size limits, CORS allow-list.
- Structured audit log of every query, guardrail decision and result hash.
- Health checks, graceful failure modes (LLM down => clear error, never partial unsafe execution), per-query cost ceiling.
- Dedicated read-only DB role per source; credentials never reach the LLM or the UI.

### Embeddings: recommendation
Use **local `sentence-transformers` with `BAAI/bge-small-en-v1.5`** (small, fast on CPU, strong for short text retrieval). Reasons:
1. The corpus is tiny (hundreds of table/column descriptions and few-shot examples), so a small model is enough.
2. No extra API key, cost, or latency, and CI works offline.
3. Schema and sample values stay on your machine (relevant for financial data).
4. Store few-shot embeddings in Supabase `pgvector`; schema-element embeddings are cached in memory and rebuilt on schema change.
Switch to a hosted embedding API only if the Phase 1 table-recall metric misses its target. The embedding provider sits behind an interface, so this is a config change.

### Dataset strategy (WealthLens first)
- WealthLens contains personal financial data. **Do not send raw personal data to the LLM or publish it.**
- Per-column sensitivity tags in the source config: `sample_values: allow | redact | never`. Default `never` for account numbers, names, free-text notes and merchant descriptions.
- Portfolio, README, demo video and evals use a **synthetic WealthLens dataset** with the same schema.
- Results shown in the UI for real data stay in your session; the LLM sees schema, glossary and approved samples only, never result rows (the back-translation judge sees SQL, not data; sanity checks run locally).
- Add finance glossary terms early ("spend", "net worth", "savings rate", "XIRR") since these are the main ambiguity risk.

## 10. Data Model (app store, separate from the target DB)

- `queries`: id, session_id, question, generated_sql, final_sql, explanation, confidence, signals (JSON), status (ok / blocked / clarification / error), prompt_version, model, tokens, latency_ms, created_at
- `guardrail_events`: id, query_id, rule_id, decision, reason, created_at
- `feedback`: id, query_id, verdict (correct / incorrect), comment, review_status (pending / approved / rejected), promoted_as (few_shot / eval_case / none)
- `few_shots`: id, question, sql, embedding, source (seed / feedback), approved_by

## 11. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Guardrail bypass via SQL tricks | Critical | AST parsing, allow-list not deny-list, DB role as 2nd layer, red-team suite |
| Prompt injection via DB values | High | Sanitize and delimit sample values, cap length, red-team cases |
| Confidence is miscalibrated | High | Calibrate on golden set; publish reliability table |
| LLM cost/latency from many calls | Medium | Haiku for judges, parallel calls, multi-query only for complex questions, caching schema context |
| Golden set too small or biased | Medium | Include hard/ambiguous/unanswerable slices; add feedback-derived cases |
| Supabase pooler incompatibility | Medium | Use direct/session connection; compose Postgres as default |
| Feedback loop poisons few-shots | Medium | Human approval before promotion |
| Same-model judge shares generator blind spots | Medium | Judge sees limited context; consider different model tier for the judge |
| Sensitive financial data reaches the LLM or the public | High | Column sensitivity tags, synthetic demo data, LLM never sees result rows |
| Scope creep (React, extra sources) | Medium | Streamlit only; Phase 5 limited to two extra connectors |

## 12. Definition of Done (v1)

- [ ] All guardrail and red-team cases pass; zero unsafe queries executed
- [ ] Execution accuracy, detection rate, refusal accuracy, calibration published in README
- [ ] `docker compose up` yields a working system from a clean clone
- [ ] Mocked-LLM test suite passes in CI; golden eval runnable with real LLM
- [ ] Same pipeline runs on WealthLens and the supply chain DB with config-only changes
- [ ] Production-tier checklist (section 9.1) complete
- [ ] Demo video and narrative complete

## 13. Decisions and Remaining Questions

### Decisions locked (2026-10-25)
| # | Decision |
|---|---|
| 1 | Tier: **Production** |
| 2 | First target DB: WealthLens (Supabase); later a procurement / supply chain DB |
| 3 | Supabase is primary DB; compose Postgres for CI/evals and second source |
| 4 | Frontend: Streamlit |
| 5 | Embeddings: local `bge-small-en-v1.5`, `pgvector` for few-shots (revisit if table recall < 95%) |
| 6 | Orchestration: LangGraph |
| 7 | Observability: Langfuse |
| 8 | Budget: not fixed; track cost per question and per eval run, set a soft ceiling after Phase 2 |
| 9 | Time: 2 h/day |
| 10 | Multi-source support is in v1 (Phase 5) |
| 11 | Repo name: `Text-To-SQL-Generator` |

### Remaining questions
1. Is the WealthLens Supabase project the live one with real data, or a dev project? (Decides the read-only role setup and whether to seed synthetic data from day one.)
2. Is the Supabase plan free tier? (Free projects pause when idle, which affects demos and CI.)
3. Should the new repo live next to your other AI projects, and do you want it created with your `aiscaffold.py` before Phase 0 code starts?
