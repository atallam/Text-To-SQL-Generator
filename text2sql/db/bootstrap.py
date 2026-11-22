"""Schema, seed and read-only role bootstrap. Uses the ADMIN url; never used by the API at runtime."""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


DEFAULT_SEED_MODULE = "text2sql.seed.wealthlens"
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _psycopg_dsn(sqlalchemy_url: str) -> str:
    url = make_url(sqlalchemy_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def ensure_database(admin_url: str) -> bool:
    """Create the database named in ``admin_url`` if it does not exist yet. Returns True if it was created."""
    url = make_url(admin_url)
    name = url.database or ""
    if not _IDENT.match(name):
        raise ValueError(f"database name {name!r} must be a lowercase identifier")
    maintenance = url.set(database="postgres")
    with psycopg.connect(_psycopg_dsn(maintenance.render_as_string(hide_password=False)), autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            return False
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        return True


def apply_schema(admin_url: str, schema_file: Path) -> None:
    with psycopg.connect(_psycopg_dsn(admin_url), autocommit=True) as conn:
        conn.execute(schema_file.read_text(encoding="utf-8"))


def seed_tables(admin_url: str, seed: int = 42, module: str = DEFAULT_SEED_MODULE) -> dict[str, int]:
    """Insert the synthetic rows from ``module`` (needs TABLE_ORDER and generate(seed)), parents first."""
    seeder = importlib.import_module(module)
    data = seeder.generate(seed)
    engine = create_engine(admin_url)
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        for table in seeder.TABLE_ORDER:
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
