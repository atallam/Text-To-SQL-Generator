"""Schema, seed and read-only role bootstrap. Uses the ADMIN url; never used by the API at runtime."""
from __future__ import annotations

import re
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from text2sql.seed.wealthlens import TABLE_ORDER, generate

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _psycopg_dsn(sqlalchemy_url: str) -> str:
    url = make_url(sqlalchemy_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def apply_schema(admin_url: str, schema_file: Path) -> None:
    with psycopg.connect(_psycopg_dsn(admin_url), autocommit=True) as conn:
        conn.execute(schema_file.read_text(encoding="utf-8"))


def seed_tables(admin_url: str, seed: int = 42) -> dict[str, int]:
    data = generate(seed)
    engine = create_engine(admin_url)
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        for table in TABLE_ORDER:
            rows = data[table]
            cols = list(rows[0].keys())
            stmt = text(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"
            )
            conn.execute(stmt, rows)
            counts[table] = len(rows)
    engine.dispose()
    return counts


def ensure_readonly_role(
    admin_url: str,
    role: str,
    password: str,
    schema: str = "public",
    statement_timeout_ms: int = 10_000,
) -> None:
    """Create/refresh a SELECT-only role: the second line of defence behind the guardrails."""
    if not _IDENT.match(role) or not _IDENT.match(schema):
        raise ValueError("role and schema must be lowercase identifiers")
    timeout = int(statement_timeout_ms)
    with psycopg.connect(_psycopg_dsn(admin_url), autocommit=True) as conn:
        db = conn.execute("SELECT current_database()").fetchone()[0]
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL(
                "{} ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(sql.SQL(verb), sql.Identifier(role), sql.Literal(password))
        )
        r, s = sql.Identifier(role), sql.Identifier(schema)
        stmts = [
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db), r),
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(s, r),
            sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(s, r),
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(s, r),
            sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(s, r),
            sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(r),
            sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(r, sql.Literal(f"{timeout}ms")),
            sql.SQL("ALTER ROLE {} SET search_path = {}").format(r, s),
        ]
        for stmt in stmts:
            conn.execute(stmt)
