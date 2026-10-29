"""SQLAlchemy introspection: database -> SchemaSnapshot.

Works on any SQLAlchemy dialect (Postgres/Supabase today, SQLite/DuckDB later). Comments, CHECK
constraints and sample values are collected when the database supports them.

Safety: structure is read through the Inspector; sample values are only queried for columns the
source's ``sample_values`` policy allows, through the read-only connection, never more than
``max_distinct`` + 1 rows per column, and ``redact`` mode masks values down to their shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy import column as sa_column
from sqlalchemy import inspect, select
from sqlalchemy import table as sa_table
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql import sqltypes

from text2sql.config.sources import SampleValuePolicy, SourceConfig
from text2sql.connectors.base import Connector
from text2sql.observability.logging import get_logger
from text2sql.schema.model import Column, ForeignKey, SchemaSnapshot, Table, compute_fingerprint

log = get_logger("introspect")

ConnectionFactory = Callable[[], AbstractContextManager[Connection]]

_STRING_LITERAL = re.compile(r"'((?:[^']|'')*)'")
_CHECK_COLUMN = re.compile(
    r"""^[\s(]*"?(?P<col>[A-Za-z_]\w*)"?(?:::\w+)?\s*(?:\)\s*)?(?:IN\s*\(|=\s*ANY\s*\()""", re.I
)
_BOOLEAN_JOIN = re.compile(r"\b(AND|OR)\b", re.I)


def parse_check_enum(sqltext: str) -> tuple[str, list[str]] | None:
    """Recognise ``col IN ('a','b')`` and Postgres' ``col = ANY (ARRAY['a'::text, ...])`` CHECKs.

    Returns (column, values) for a single-column, all-string-literal membership test, else None.
    """
    if _BOOLEAN_JOIN.search(sqltext):
        return None
    m = _CHECK_COLUMN.match(sqltext)
    if not m:
        return None
    values = [v.replace("''", "'") for v in _STRING_LITERAL.findall(sqltext)]
    return (m.group("col"), values) if values else None


def mask_value(value: str) -> str:
    """Reduce a value to its shape (letters -> x/X, digits -> 9) so formats survive but content does not."""
    out = []
    for ch in value:
        if ch.isdigit():
            out.append("9")
        elif ch.isalpha():
            out.append("X" if ch.isupper() else "x")
        else:
            out.append(ch)
    return "".join(out)


def _wants_samples(col_type: object) -> bool:
    return isinstance(col_type, sqltypes.String)  # TEXT, VARCHAR, CHAR, Enum


def introspect_structure(engine: Engine, cfg: SourceConfig) -> list[Table]:
    """Tables, columns, keys, comments and CHECK-derived value lists. No data is read."""
    insp = inspect(engine)
    schema = cfg.schema_name if engine.dialect.name != "sqlite" else None
    excluded = {t.lower() for t in cfg.exclude_tables}
    tables: list[Table] = []
    for name in sorted(insp.get_table_names(schema=schema)):
        if name.lower() in excluded:
            continue
        pk = insp.get_pk_constraint(name, schema=schema)
        pk_cols = list(pk.get("constrained_columns") or [])

        enums: dict[str, list[str]] = {}
        try:
            for ck in insp.get_check_constraints(name, schema=schema):
                parsed = parse_check_enum(ck.get("sqltext", ""))
                if parsed:
                    enums[parsed[0]] = parsed[1]
        except NotImplementedError:  # dialect without CHECK reflection
            pass

        try:
            table_comment = (insp.get_table_comment(name, schema=schema) or {}).get("text")
        except NotImplementedError:
            table_comment = None

        columns = [
            Column(
                name=c["name"],
                type=c["type"].compile(dialect=engine.dialect).lower(),
                nullable=bool(c.get("nullable", True)),
                primary_key=c["name"] in pk_cols,
                comment=c.get("comment"),
                allowed_values=enums.get(c["name"]),
            )
            for c in insp.get_columns(name, schema=schema)
        ]
        fks = [
            ForeignKey(
                columns=list(fk["constrained_columns"]),
                ref_table=fk["referred_table"],
                ref_columns=list(fk["referred_columns"]),
            )
            for fk in insp.get_foreign_keys(name, schema=schema)
            if fk.get("referred_table")
        ]
        tables.append(
            Table(name=name, comment=table_comment, columns=columns, primary_key=pk_cols, foreign_keys=fks)
        )
    return tables


def _fetch_samples(
    conn: Connection, table: str, column: str, schema: str | None, policy: SampleValuePolicy, mode: str
) -> list[str]:
    stmt = (
        select(sa_column(column))
        .select_from(sa_table(table, schema=schema))
        .where(sa_column(column).is_not(None))
        .distinct()
        .order_by(sa_column(column))
        .limit(policy.max_distinct + 1)
    )
    values = [str(v[0]).strip() for v in conn.execute(stmt).all()]
    if len(values) > policy.max_distinct:
        return []  # high cardinality: samples would be noise and may leak records
    if any(len(v) > policy.max_value_length for v in values):
        return []  # looks like free text
    return [mask_value(v) for v in values] if mode == "redact" else values


def attach_samples(
    tables: list[Table], cfg: SourceConfig, engine: Engine, connection: ConnectionFactory | None = None
) -> None:
    """Fill ``sample_values`` in place according to the source's policy."""
    policy = cfg.sample_values
    schema = cfg.schema_name if engine.dialect.name != "sqlite" else None
    candidates = [
        (t, c, policy.mode_for(t.name, c.name))
        for t in tables
        for c in t.columns
        if c.allowed_values is None and policy.mode_for(t.name, c.name) != "never"
    ]
    if not candidates:
        return
    opener: ConnectionFactory = connection or engine.connect
    # Column types are needed to skip non-text columns; reflect once.
    insp = inspect(engine)
    text_cols = {
        (t.name, c["name"])
        for t in tables
        for c in insp.get_columns(t.name, schema=schema)
        if _wants_samples(c["type"])
    }
    with opener() as conn:
        for t, c, mode in candidates:
            if (t.name, c.name) not in text_cols:
                continue
            try:
                c.sample_values = _fetch_samples(conn, t.name, c.name, schema, policy, mode)
            except Exception as exc:  # a failed sample must never break introspection
                log.warning("sample_values_failed", table=t.name, column=c.name, error=repr(exc)[:200])


def build_snapshot(
    cfg: SourceConfig,
    engine: Engine,
    *,
    connection: ConnectionFactory | None = None,
    with_samples: bool = True,
) -> SchemaSnapshot:
    tables = introspect_structure(engine, cfg)
    if with_samples:
        attach_samples(tables, cfg, engine, connection)
    return SchemaSnapshot(
        source=cfg.name,
        dialect=engine.dialect.name,
        schema_name=cfg.schema_name if engine.dialect.name != "sqlite" else None,
        tables=tables,
        fingerprint=compute_fingerprint(tables),
    )


def introspect_connector(connector: Connector, *, with_samples: bool = True) -> SchemaSnapshot:
    """Introspect through a Connector: structure via its read-only engine, samples via the sandbox."""
    return build_snapshot(
        connector.config,
        connector.engine(),
        connection=connector.readonly_connection,
        with_samples=with_samples,
    )


def current_fingerprint(cfg: SourceConfig, engine: Engine) -> str:
    """Cheap structure-only fingerprint for change detection (no data queried)."""
    return compute_fingerprint(introspect_structure(engine, cfg))
