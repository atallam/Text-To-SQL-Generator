"""Procurement source against a real PostgreSQL (CI service / pgserver). Skipped unless T2S_TEST_ADMIN_URL is set.

Proves the new source needs config only: its schema seeds, every few-shot and glossary example runs and
returns rows, and the schema is introspected and rendered by the same core code as WealthLens.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from text2sql.config.settings import Settings
from text2sql.config.sources import load_source
from text2sql.connectors.postgres import PostgresConnector
from text2sql.db.bootstrap import apply_schema, ensure_readonly_role, seed_tables
from text2sql.generation.examples import load_examples
from text2sql.schema.glossary import Glossary
from text2sql.schema.introspect import introspect_connector

pytestmark = pytest.mark.integration
ADMIN = os.environ.get("T2S_TEST_ADMIN_URL")
PASSWORD = "test-readonly-pw"
TABLES = (
    "rmas, receipts, shipment_lines, shipments, po_changes, po_lines, purchase_orders, "
    "inventory, supplier_parts, parts, suppliers"
)


@pytest.fixture(scope="module")
def source():
    return load_source("procurement", Settings(_env_file=None))


@pytest.fixture(scope="module")
def db(source):
    if not ADMIN:
        pytest.skip("T2S_TEST_ADMIN_URL not set")
    apply_schema(ADMIN, source.base_dir / source.schema_file)
    seed_tables(ADMIN, module=source.seed_module)
    ensure_readonly_role(ADMIN, "t2s_readonly", PASSWORD, "public", 10_000)
    ro = make_url(ADMIN).set(username="t2s_readonly", password=PASSWORD)
    yield ro.render_as_string(hide_password=False)
    engine = create_engine(ADMIN)  # leave the shared test database as WealthLens-only
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {TABLES} CASCADE"))
    engine.dispose()


@pytest.fixture
def connector(db, source):
    conn = PostgresConnector(source, url=db)
    yield conn
    conn.dispose()


def run(connector, sql):
    with connector.readonly_connection() as conn:
        return conn.exec_driver_sql(sql).fetchall()


def test_schema_is_introspected(connector):
    snap = introspect_connector(connector)
    names = {t.name for t in snap.tables}
    assert {"suppliers", "parts", "purchase_orders", "po_lines", "po_changes", "shipments", "receipts", "rmas"} <= names
    # CHECK-constrained columns expose their allowed values to the prompt
    assert set(snap.table("po_lines").column("status").allowed_values) == {"open", "partially_received", "received", "cancelled"}
    assert snap.table("shipments").column("carrier").sample_values  # allow-listed free-text column gets samples
    assert snap.table("suppliers").column("name").sample_values == []  # names never reach the LLM


def test_every_few_shot_example_runs(connector, source):
    examples = load_examples(source.base_dir / source.examples, source.today_sql)
    assert len(examples) >= 10
    for ex in examples:
        assert run(connector, ex.sql), f"example returned no rows: {ex.question}"


def test_every_glossary_example_runs(connector, source):
    glossary = Glossary.load(source.base_dir / source.glossary, source.today_sql)
    checked = 0
    for term in glossary.terms.values():
        for interp in term.interpretations:
            assert run(connector, interp.example_sql), f"{term.name} / {interp.label} returned no rows"
            checked += 1
    assert checked >= 4


def test_readonly_role_cannot_write(connector):
    with pytest.raises(Exception):  # noqa: B017  any DB error proves the role is read-only
        with connector.readonly_connection() as conn:
            conn.exec_driver_sql("DELETE FROM suppliers")
