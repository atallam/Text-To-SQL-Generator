"""In-memory SQLite sandbox used by the evals.

Every Gretel case ships its own tiny database (a CREATE TABLE + INSERT script). We load it into an
in-memory SQLite connection and then lock it down twice, mirroring the production safety model:
``PRAGMA query_only`` (read-only transaction) plus an authorizer that only allows reads (the
SELECT-only role). Even a statement that slips past the guardrail cannot change anything.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

# sqlite3 authorizer action codes, written as numbers so this works on every Python build.
_READ, _SELECT, _FUNCTION, _RECURSIVE = 20, 21, 31, 33
_ALLOWED_ACTIONS = {_READ, _SELECT, _FUNCTION, _RECURSIVE}


class SandboxError(RuntimeError):
    """The case's context script could not be loaded into SQLite."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool


def _read_only_authorizer(action: int, *_args) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def build_db(context_sql: str) -> sqlite3.Connection:
    """Load the context script, then lock the connection to read-only."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(context_sql)
        conn.commit()
    except sqlite3.Error as exc:
        conn.close()
        raise SandboxError(str(exc)) from exc
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_read_only_authorizer)
    return conn


def run_select(
    conn: sqlite3.Connection, sql: str, *, max_rows: int = 1000, timeout_s: float = 5.0
) -> QueryResult:
    """Execute one statement with a wall-clock timeout. Raises sqlite3.Error on any failure."""
    deadline = time.monotonic() + timeout_s
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows + 1)
        columns = [d[0] for d in cur.description or []]
    finally:
        conn.set_progress_handler(None, 0)
    return QueryResult(columns=columns, rows=rows[:max_rows], truncated=len(rows) > max_rows)


def list_tables(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return [r[0] for r in cur.fetchall()]


def describe_schema(conn: sqlite3.Connection, sample_rows: int = 3) -> str:
    """DDL plus a few sample rows per table: the schema context handed to a predictor."""
    parts: list[str] = []
    cur = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    for name, ddl in cur.fetchall():
        parts.append((ddl or "").strip().rstrip(";") + ";")
        if sample_rows:
            quoted = '"' + name.replace('"', '""') + '"'
            rows = conn.execute(f"SELECT * FROM {quoted} LIMIT {int(sample_rows)}").fetchall()
            for row in rows:
                parts.append(f"-- sample: {repr(row)[:200]}")
    return "\n".join(parts)
