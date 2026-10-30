"""Executor and EXPLAIN gate mechanics against SQLite (SQLAlchemy stands in for any dialect)."""

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from tests.helpers import source_config
from text2sql.connectors.base import Connector
from text2sql.execution.executor import ExecutionError, Executor
from text2sql.guardrails.config import ExplainRule
from text2sql.guardrails.explain_gate import ExplainGate


class SqliteConnector(Connector):
    dialect = "sqlite"

    def __init__(self):
        super().__init__(source_config(dialect="sqlite", max_rows=5))
        self._engine = create_engine("sqlite://", poolclass=StaticPool)
        with self._engine.begin() as c:
            c.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
            c.exec_driver_sql("INSERT INTO t (name) VALUES " + ",".join(f"('n{i}')" for i in range(20)))

    def engine(self):
        return self._engine

    @contextmanager
    def readonly_connection(self, timeout_ms=None):
        conn = self._engine.connect()
        try:
            yield conn
        finally:
            conn.rollback()
            conn.close()


@pytest.fixture
def connector():
    return SqliteConnector()


def test_rows_are_capped_and_truncation_is_reported(connector):
    res = Executor(connector).run("SELECT id, name FROM t ORDER BY id")
    assert res.row_count == 5 and res.truncated and res.columns == ["id", "name"]
    assert res.to_dataframe().shape == (5, 2)


def test_small_result_is_not_truncated_and_hash_is_stable(connector):
    ex = Executor(connector, max_rows=100)
    a, b = (
        ex.run("SELECT id FROM t WHERE id <= 3 ORDER BY id"),
        ex.run("SELECT id FROM t WHERE id <= 3 ORDER BY id"),
    )
    assert a.row_count == 3 and not a.truncated and a.result_hash == b.result_hash
    assert ex.run("SELECT id FROM t WHERE id <= 4 ORDER BY id").result_hash != a.result_hash


def test_sql_errors_become_repairable_execution_errors(connector):
    with pytest.raises(ExecutionError) as err:
        Executor(connector).run("SELECT nope FROM t")
    assert err.value.kind == "sql_error" and err.value.repairable and "nope" in err.value.message


def test_permission_and_readonly_kinds_are_final():
    assert not ExecutionError("permission", "x").repairable
    assert not ExecutionError("read_only", "x").repairable
    assert ExecutionError("timeout", "x").repairable


def test_explain_gate_is_skipped_for_non_postgres(connector):
    out = ExplainGate(connector, ExplainRule()).check("SELECT 1")
    assert out.ok and out.skipped


def test_explain_gate_can_be_turned_off(connector):
    connector.dialect = "postgresql"
    assert ExplainGate(connector, ExplainRule(mode="off")).check("SELECT 1").skipped


def test_driver_sql_only_changes_percent_style_drivers(connector):
    from text2sql.connectors.postgres import PostgresConnector

    sql = "SELECT * FROM t WHERE name LIKE '%a%' AND x = 'a%b'"
    assert connector.driver_sql(sql) == sql  # sqlite uses qmark parameters
    pg = PostgresConnector(source_config(dialect="postgresql"), url="postgresql+psycopg://u:p@h/db")
    assert pg.driver_sql(sql) == "SELECT * FROM t WHERE name LIKE '%%a%%' AND x = 'a%%b'"
