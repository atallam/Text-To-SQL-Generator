"""Phase 3 against a real PostgreSQL (skipped unless T2S_TEST_ADMIN_URL is set).

Everything except the model is real: sanity checks run against the seeded WealthLens data, the second query
goes through the real guardrails -> EXPLAIN gate -> read-only role, and the scores come out of the real
pipeline. The model is scripted so each scenario is exact.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from tests.helpers import ScriptedLLM, gen
from text2sql.config.settings import Settings
from text2sql.config.sources import load_source
from text2sql.connectors.postgres import PostgresConnector
from text2sql.db.bootstrap import apply_schema, ensure_readonly_role, seed_tables
from text2sql.execution.executor import Executor
from text2sql.generation.context import ContextBuilder
from text2sql.generation.generator import SQLGenerator
from text2sql.guardrails import GuardrailEngine, load_guardrail_config
from text2sql.guardrails.explain_gate import ExplainGate
from text2sql.pipeline import QueryOutcome, Text2SQL
from text2sql.schema.embeddings import LexicalEmbedder
from text2sql.validation import Validator, load_validation_config
from text2sql.validation.backtranslate import AlignmentVerdict, SQLDescription
from text2sql.validation.sanity import TableStats

pytestmark = pytest.mark.integration
ADMIN = os.environ.get("T2S_TEST_ADMIN_URL")
SOURCE_DIR = Path(__file__).resolve().parents[2] / "sources" / "wealthlens"

GOOD = AlignmentVerdict(
    entity="match",
    measure="match",
    filters="match",
    time_range="match",
    grouping="match",
    output_shape="match",
)
BAD = GOOD.model_copy(update={"measure": "mismatch", "issues": ["counts rows instead of summing amounts"]})

GROCERIES = (
    "SELECT COALESCE(SUM(t.amount), 0) AS grocery_spend FROM transactions t JOIN categories c "
    "ON c.id = t.category_id WHERE c.name = 'Groceries' AND c.kind = 'expense' AND t.txn_type = 'debit' "
    "AND t.txn_date >= DATE '2026-08-01' AND t.txn_date < DATE '2026-09-01'"
)
GROCERIES_ALT = (
    "SELECT SUM(amount) AS total FROM transactions WHERE txn_type = 'debit' AND category_id IN "
    "(SELECT id FROM categories WHERE name = 'Groceries' AND kind = 'expense') "
    "AND txn_date BETWEEN DATE '2026-08-01' AND DATE '2026-08-31'"
)
FANOUT = (
    "SELECT c.name AS category, b.amount AS budget FROM budgets b JOIN categories c ON c.id = b.category_id "
    "JOIN transactions t ON t.category_id = b.category_id WHERE b.month = DATE '2026-09-01' ORDER BY c.name"
)


@pytest.fixture(scope="module")
def db():
    if not ADMIN:
        pytest.skip("T2S_TEST_ADMIN_URL not set")
    apply_schema(ADMIN, SOURCE_DIR / "schema.sql")
    seed_tables(ADMIN)
    ensure_readonly_role(ADMIN, "t2s_readonly", "test-readonly-pw", "public", 10_000)
    return make_url(ADMIN).set(username="t2s_readonly", password="test-readonly-pw").render_as_string(False)


@pytest.fixture(scope="module")
def source():
    return load_source("wealthlens", Settings(_env_file=None))


@pytest.fixture
def connector(db, source):
    conn = PostgresConnector(source, url=db)
    yield conn
    conn.dispose()


def pipeline(connector, source, llm, *, threshold=3, validate=True) -> Text2SQL:
    gcfg = load_guardrail_config(source)
    engine = GuardrailEngine(gcfg)
    gate = ExplainGate(connector, gcfg.explain, gcfg.statement_timeout_ms)
    executor = Executor(connector, gcfg.row_limit.max_rows, gcfg.statement_timeout_ms)
    vcfg = load_validation_config(source, overrides={"multiquery": {"complexity_threshold": threshold}})
    validator = (
        Validator.build(
            llm=llm, engine=engine, gate=gate, executor=executor, cfg=vcfg, settings=Settings(_env_file=None)
        )
        if validate
        else None
    )
    return Text2SQL(
        builder=ContextBuilder.from_source(source, connector=connector, embedder=LexicalEmbedder()),
        generator=SQLGenerator(llm),
        engine=engine,
        gate=gate,
        executor=executor,
        validator=validator,
    )


def script(primary_sql, *, alt_sql=None, verdict=GOOD, alt_error=None):
    def route(agent, system, user):
        if agent == "sql_generator":
            return gen(primary_sql, tables_used=["transactions", "categories"])
        if agent == "sql_describer":
            return SQLDescription(summary="described")
        if agent == "sql_judge":
            return verdict
        if alt_error:
            raise alt_error
        return gen(alt_sql)

    return ScriptedLLM(route)


Q = "How much did I spend on groceries last month?"


def test_correct_query_with_agreeing_second_approach_is_high_confidence(connector, source):
    llm = script(GROCERIES, alt_sql=GROCERIES_ALT)
    out = pipeline(connector, source, llm, threshold=0).ask(Q)
    assert out.status == "success" and out.validation is not None
    v = out.validation
    assert v.alternative.status == "agree" and v.alternative.comparison.kind == "exact"
    assert v.confidence.level == "high" and out.confidence == v.confidence.score >= 0.9
    assert v.confidence.signals_used == 5 and not v.errors
    assert out.timings_ms["validation"] > 0
    assert {c["agent"] for c in llm.calls} == {
        "sql_generator",
        "sql_describer",
        "sql_judge",
        "sql_alternative",
    }


def test_second_approach_with_different_data_caps_confidence_and_is_shown(connector, source):
    wrong_alt = GROCERIES_ALT.replace("2026-08-31", "2026-09-30")  # a different month
    out = pipeline(connector, source, script(GROCERIES, alt_sql=wrong_alt), threshold=0).ask(Q)
    v = out.validation
    assert v.alternative.status == "disagree" and v.confidence.score <= 0.5
    assert v.alternative.preview_rows and v.alternative.sql
    assert out.status == "success" and out.execution.row_count == 1  # the answer is still returned, flagged


def test_judge_flags_a_misaligned_sql(connector, source):
    good = pipeline(connector, source, script(GROCERIES), threshold=99).ask(Q)
    bad = pipeline(connector, source, script(GROCERIES, verdict=BAD), threshold=99).ask(Q)
    assert bad.confidence < good.confidence - 0.1
    assert bad.validation.back_translation.issues == ["counts rows instead of summing amounts"]


def test_simple_questions_skip_the_second_approach_by_default(connector, source):
    llm = script(GROCERIES, alt_sql=GROCERIES_ALT)
    out = pipeline(connector, source, llm, threshold=3).ask(Q)
    assert out.validation.alternative is None and "sql_alternative" not in {c["agent"] for c in llm.calls}
    assert out.validation.confidence.signals_used == 4


def test_join_fanout_is_caught_by_sanity_checks_on_real_data(connector, source):
    out = pipeline(connector, source, script(FANOUT), threshold=99).ask(
        "What is my budget per category in September 2026?"
    )
    flagged = {c.name for c in out.validation.sanity.flagged}
    assert out.status == "success" and {"duplicate_rows", "join_fanout"} <= flagged
    assert (
        out.validation.confidence.signals[2].name == "sanity"
        and out.validation.confidence.signals[2].value < 1.0
    )


def test_date_filter_outside_the_data_is_flagged(connector, source):
    sql = GROCERIES.replace("2026-08-01", "2030-08-01").replace("2026-09-01", "2030-09-01")
    out = pipeline(connector, source, script(sql), threshold=99).ask(Q)
    checks = {c.name: c for c in out.validation.sanity.checks}
    assert checks["date_range"].status == "fail" and "after the last date" in checks["date_range"].detail


def test_table_stats_read_real_values(connector, source):
    stats = TableStats(
        Executor(connector, 1000, 10_000),
        pipeline(connector, source, ScriptedLLM(), validate=False).builder.schema.get(),
    )
    assert stats.row_count("transactions") > 1000
    lo, hi = stats.date_span("transactions", "txn_date")
    assert lo.year == 2024 and hi.year == 2026
    assert stats.column_sum("transactions", "amount") > 0
    assert stats.row_count("no_such_table") is None  # failure -> None, never an exception


def test_unanswerable_short_circuits_with_no_validation_calls(connector, source):
    llm = ScriptedLLM(gen("", unanswerable=True, unanswerable_reason="there is no supplier data"))
    out = pipeline(connector, source, llm).ask("Which supplier is late?")
    assert out.status == "unanswerable"
    assert out.validation.confidence.level == "cannot_answer" and out.confidence == 0.0
    assert len(llm.calls) == 1  # only the generation call


def test_blocked_and_failed_answers_have_no_confidence(connector, source):
    llm = ScriptedLLM(gen("DELETE FROM transactions"))
    out = pipeline(connector, source, llm).ask("delete everything")
    assert out.status == "blocked" and out.validation is None and out.confidence is None
    assert [c["agent"] for c in llm.calls] == ["sql_generator"]


def test_a_crashing_validator_never_costs_the_answer(connector, source):
    t2s = pipeline(connector, source, script(GROCERIES))

    class Boom:
        def validate(self, *a, **k):
            raise RuntimeError("scoring exploded")

    t2s.validator = Boom()
    out = t2s.ask(Q)
    assert out.status == "success" and out.execution.row_count == 1 and out.validation is None


def test_disabled_validation_keeps_phase2_behaviour(connector, source):
    llm = script(GROCERIES)
    out = pipeline(connector, source, llm, validate=False).ask(Q)
    assert out.status == "success" and out.validation is None
    assert {c["agent"] for c in llm.calls} == {"sql_generator"}


def test_outcome_serialises_with_the_confidence_breakdown(connector, source):
    out = pipeline(connector, source, script(GROCERIES, alt_sql=GROCERIES_ALT), threshold=0).ask(Q)
    again = QueryOutcome.model_validate_json(out.model_dump_json())
    sigs = {s.name for s in again.validation.confidence.signals}
    assert sigs == {"backtranslation", "multiquery", "sanity", "schema_coverage", "syntax"}
    assert again.validation.confidence.calibrated is False
