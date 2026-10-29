"""Phase 1 against a real PostgreSQL (CI service / pgserver). Skipped unless T2S_TEST_ADMIN_URL is set.

Proves what the offline tests cannot: introspection of real catalog metadata, that every few-shot
example and every glossary example actually runs, and that a DDL change flows into the schema context.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.helpers import FIXTURES
from text2sql.config.settings import Settings
from text2sql.config.sources import load_source
from text2sql.connectors.postgres import PostgresConnector
from text2sql.db.bootstrap import apply_schema, ensure_readonly_role, seed_tables
from text2sql.generation.context import ContextBuilder
from text2sql.generation.examples import load_examples
from text2sql.schema.embeddings import LexicalEmbedder
from text2sql.schema.glossary import Glossary
from text2sql.schema.introspect import current_fingerprint, introspect_connector
from text2sql.schema.model import SchemaSnapshot
from text2sql.schema.provider import SchemaProvider

pytestmark = pytest.mark.integration
ADMIN = os.environ.get("T2S_TEST_ADMIN_URL")
SOURCE_DIR = Path(__file__).resolve().parents[2] / "sources" / "wealthlens"
PASSWORD = "test-readonly-pw"


@pytest.fixture(scope="module")
def db():
    if not ADMIN:
        pytest.skip("T2S_TEST_ADMIN_URL not set")
    apply_schema(ADMIN, SOURCE_DIR / "schema.sql")
    seed_tables(ADMIN)
    ensure_readonly_role(ADMIN, "t2s_readonly", PASSWORD, "public", 10_000)
    ro = make_url(ADMIN).set(username="t2s_readonly", password=PASSWORD)
    return ro.render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def source():
    return load_source("wealthlens", Settings(_env_file=None))


@pytest.fixture
def connector(db, source):
    conn = PostgresConnector(source, url=db)
    yield conn
    conn.dispose()


def run(connector, sql):
    with connector.readonly_connection() as conn:
        return conn.exec_driver_sql(sql).fetchall()


def test_introspection_matches_the_committed_fixture(connector):
    """Offline tests rely on tests/fixtures/wealthlens_schema.json; keep it in step with schema.sql."""
    live = introspect_connector(connector)
    fixture = SchemaSnapshot.model_validate_json(
        (FIXTURES / "wealthlens_schema.json").read_text(encoding="utf-8")
    )
    assert live.model_dump() == fixture.model_dump(), (
        "schema fixture is stale. Regenerate: python -m scripts.dump_schema --source wealthlens "
        "--out tests/fixtures/wealthlens_schema.json"
    )


def test_real_catalog_metadata_is_captured(connector):
    snap = introspect_connector(connector)
    tx = snap.table("transactions")
    assert tx.comment and "amount is always positive" in tx.comment
    assert tx.column("amount").comment
    assert tx.column("txn_type").allowed_values == [
        "debit",
        "credit",
    ]  # parsed from Postgres' ANY(ARRAY[..]) form
    assert snap.table("asset_prices").primary_key == ["asset_id", "price_date"]
    assert {fk.ref_table for fk in tx.foreign_keys} == {"accounts", "categories"}


def test_sample_values_follow_the_source_policy(connector, source):
    snap = introspect_connector(connector)
    allowed = set(source.sample_values.allow)
    for t in snap.tables:
        for c in t.columns:
            if c.sample_values:
                assert f"{t.name}.{c.name}" in allowed, f"{t.name}.{c.name} leaked sample values"
    assert snap.table("accounts").column("currency").sample_values  # an allow-listed column does get samples
    assert snap.table("transactions").column("merchant").sample_values == []


def test_every_few_shot_example_runs(connector, source):
    for ex in load_examples(source.base_dir / source.examples, source.today_sql):
        rows = run(connector, ex.sql)
        if "over budget" not in ex.question:  # may legitimately be empty
            assert rows, f"example returned no rows: {ex.question}"


def test_every_glossary_example_runs(connector, source):
    glossary = Glossary.load(source.base_dir / source.glossary, source.today_sql)
    checked = 0
    for term in glossary.terms.values():
        for interp in term.interpretations:
            assert run(connector, interp.example_sql), f"{term.name} / {interp.label} returned no rows"
            checked += 1
    assert checked >= 8


def test_ddl_change_flows_into_the_schema_context(db, source, connector):
    tick = iter(range(0, 10_000, 100))
    provider = SchemaProvider(
        build=lambda: introspect_connector(connector),
        fingerprint=lambda: current_fingerprint(source, connector.engine()),
        refresh_s=60,
        clock=lambda: next(tick),
    )
    before = provider.get()
    assert "notes" not in [c.name for c in before.table("goals").columns]

    admin = create_engine(ADMIN)
    try:
        with admin.begin() as c:
            c.execute(text("ALTER TABLE goals ADD COLUMN notes text"))
        after = provider.get()  # the fake clock has moved past the refresh window
        assert after.fingerprint != before.fingerprint
        assert "notes" in [c.name for c in after.table("goals").columns]
    finally:
        with admin.begin() as c:
            c.execute(text("ALTER TABLE goals DROP COLUMN IF EXISTS notes"))
        admin.dispose()


def test_from_source_wires_everything_against_a_real_database(connector, source):
    builder = ContextBuilder.from_source(
        source, connector=connector, embedder=LexicalEmbedder(), settings=Settings(_env_file=None)
    )
    ready = builder.prepare("How much did I spend on groceries last month?")
    assert ready.status == "ready"
    assert {"transactions", "categories"} <= set(ready.prompt.tables)
    assert "TABLE transactions" in ready.prompt.user and "DATE '2026-09-30'" in ready.prompt.system

    ask = builder.prepare("What is my net worth?")
    assert ask.status == "needs_clarification" and ask.prompt is None

    again = builder.prepare("List my accounts")
    assert again.prompt.schema_fingerprint == ready.prompt.schema_fingerprint  # schema cached, not re-read
