"""Executor: run guardrail-approved SQL in the read-only sandbox and return a capped, audited result.

Layers between the generated SQL and the data (each independent):
  1. guardrails (AST allow-list)  2. READ ONLY transaction, always rolled back (connector)
  3. SELECT-only database role    4. statement_timeout   5. hard row cap applied here.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DBAPIError

from text2sql.connectors.base import Connector
from text2sql.guardrails.explain_gate import db_error_message
from text2sql.observability.logging import get_logger

log = get_logger("executor")

ErrorKind = Literal["timeout", "permission", "read_only", "sql_error", "connection"]

# PostgreSQL SQLSTATE -> kind
_STATES = {"57014": "timeout", "42501": "permission", "25006": "read_only"}


class ExecutionError(Exception):
    def __init__(self, kind: ErrorKind, message: str):
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.message = message

    @property
    def repairable(self) -> bool:
        """The model can fix a SQL error or an over-expensive query; permission problems are final."""
        return self.kind in ("sql_error", "timeout")


class ExecutionResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    columns: list[str]
    rows: list[list[Any]] = Field(repr=False)
    row_count: int
    truncated: bool  # more rows existed than max_rows
    elapsed_ms: int
    result_hash: str  # sha256 over columns + rows: audit evidence of exactly what was returned
    sql: str

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=self.columns)


def _hash(columns: list[str], rows: list[list[Any]]) -> str:
    payload = json.dumps([columns, rows], default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def classify(exc: DBAPIError) -> ExecutionError:
    state = getattr(getattr(exc, "orig", None), "sqlstate", None) or ""
    kind: ErrorKind = _STATES.get(state) or ("connection" if exc.connection_invalidated else "sql_error")  # type: ignore[assignment]
    return ExecutionError(kind, db_error_message(exc))


class Executor:
    def __init__(self, connector: Connector, max_rows: int | None = None, timeout_ms: int | None = None):
        self.connector = connector
        self.max_rows = max_rows if max_rows is not None else connector.config.max_rows
        self.timeout_ms = timeout_ms

    def run(self, safe_sql: str) -> ExecutionResult:
        """Execute SQL that has already passed the guardrails. Raises ExecutionError."""
        t0 = time.perf_counter()
        try:
            with self.connector.readonly_connection(self.timeout_ms) as conn:
                result = conn.execution_options(stream_results=True).exec_driver_sql(
                    self.connector.driver_sql(safe_sql)
                )
                columns = list(result.keys())
                fetched = result.fetchmany(self.max_rows + 1)
                result.close()
        except DBAPIError as exc:
            err = classify(exc)
            log.warning("execution_error", kind=err.kind, error=err.message)
            raise err from exc
        truncated = len(fetched) > self.max_rows
        rows = [list(r) for r in fetched[: self.max_rows]]
        elapsed = int((time.perf_counter() - t0) * 1000)
        out = ExecutionResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed,
            result_hash=_hash(columns, rows),
            sql=safe_sql,
        )
        log.info("executed", rows=out.row_count, truncated=truncated, ms=elapsed, result_hash=out.result_hash)
        return out
