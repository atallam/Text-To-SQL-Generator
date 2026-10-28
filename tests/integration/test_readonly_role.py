"""Phase 0 exit gate: the read-only role provably cannot write or alter anything.

Runs only when T2S_TEST_ADMIN_URL points at a throwaway Postgres (CI service / pgserver).
"""
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from text2sql.config.sources import SourceConfig
from text2sql.connectors.postgres import PostgresConnector
from text2sql.db.bootstrap import apply_schema, ensure_readonly_role, seed_tables

pytestmark = pytest.mark.integration
ADMIN = os.environ.get("T2S_TEST_ADMIN_URL")
SCHEMA = Path(__file__).resolve().parents[2] / "sources" / "wealthlens" / "schema.sql"
PASSWORD = "test-readonly-pw"


@pytest.fixture(scope="module")
def db():
    if not ADMIN:
        pytest.skip("T2S_TEST_ADMIN_URL not set")
    apply_schema(ADMIN, SCHEMA)
    seed_tables(ADMIN)
    ensure_readonly_role(ADMIN, "t2s_readonly", PASSWORD, "public", 10_000)
    ro_url = make_url(ADMIN).set(username="t2s_readonly", password=PASSWORD)
    return ro_url.render_as_string(hide_password=False)


@pytest.fixture
def cfg():
    return SourceConfig.model_validate(
        dict(
            name="t", display_name="t", dialect="postgresql",
            connection=dict(readonly_url_env="X"), statement_timeout_ms=10_000,
        )
    )


def _fails(engine, stmt):
    with engine.connect() as c:
        with pytest.raises(DBAPIError):
            c.execute(text(stmt))


def test_role_can_select(db):
    e = create_engine(db)
    with e.connect() as c:
        assert c.execute(text("SELECT count(*) FROM transactions")).scalar() > 1000


@pytest.mark.parametrize(
    "stmt",
    [
        "INSERT INTO goals (id,name,target_amount,current_amount,target_date) "
        "VALUES (99,'x',1,1,'2030-01-01')",
        "UPDATE accounts SET name='hacked'",
        "DELETE FROM transactions",
        "DROP TABLE transactions",
        "TRUNCATE transactions",
        "CREATE TABLE evil (id int)",
        "ALTER TABLE accounts ADD COLUMN x int",
        "CREATE ROLE evil LOGIN",
    ],
)
def test_role_cannot_write_or_alter(db, stmt):
    _fails(create_engine(db), stmt)


def test_default_transaction_is_read_only(db):
    e = create_engine(db)
    with e.connect() as c:
        assert c.execute(text("SHOW default_transaction_read_only")).scalar() == "on"


def test_sandbox_blocks_writes_even_for_admin_credentials(db, cfg):
    """Layer-2 check: with admin rights, readonly_connection still refuses writes."""
    conn = PostgresConnector(cfg, url=ADMIN)
    with conn.readonly_connection() as c:
        with pytest.raises(DBAPIError):
            c.execute(text("UPDATE accounts SET name='hacked'"))
    with create_engine(ADMIN).connect() as c:
        assert c.execute(text("SELECT count(*) FROM accounts WHERE name='hacked'")).scalar() == 0


def test_sandbox_statement_timeout(db, cfg):
    conn = PostgresConnector(cfg, url=db)
    with conn.readonly_connection(timeout_ms=300) as c:
        with pytest.raises(DBAPIError):
            c.execute(text("SELECT pg_sleep(3)"))


def test_connector_health_check(db, cfg):
    assert PostgresConnector(cfg, url=db).health_check() is True


