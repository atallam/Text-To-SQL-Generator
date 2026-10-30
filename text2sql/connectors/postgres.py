"""PostgreSQL / Supabase connector.

Supabase: use the direct or session-pooler connection string. The transaction pooler (port 6543)
does not reliably support session-level SET commands, which the read-only sandbox relies on.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine

from text2sql.config.sources import SourceConfig
from text2sql.connectors.base import Connector


class PostgresConnector(Connector):
    dialect = "postgresql"

    def __init__(self, config: SourceConfig, url: str | None = None):
        super().__init__(config)
        self._url = url
        self._engine: Engine | None = None

    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(
                self._url or self.config.readonly_url(), pool_pre_ping=True, pool_size=5
            )
        return self._engine

    def driver_sql(self, sql: str) -> str:
        return sql.replace("%", "%%")

    @contextmanager
    def readonly_connection(self, timeout_ms: int | None = None) -> Iterator[Connection]:
        timeout = int(timeout_ms if timeout_ms is not None else self.config.statement_timeout_ms)
        conn = self.engine().connect()
        try:
            # First statement of the implicit transaction: layer 2 of read-only enforcement
            # (layer 1 = SQL guardrails, layer 3 = SELECT-only DB role).
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout}")
            # Backslashes in string literals are literal characters (the default; pinned so a database
            # with the legacy setting cannot reinterpret the SQL we validated).
            conn.exec_driver_sql("SET LOCAL standard_conforming_strings = on")
            yield conn
        finally:
            conn.rollback()
            conn.close()

