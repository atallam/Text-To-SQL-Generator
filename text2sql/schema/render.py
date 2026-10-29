"""Render a SchemaSnapshot as compact, prompt-ready text. Pure functions, snapshot-tested."""

from __future__ import annotations

from text2sql.schema.model import Column, SchemaSnapshot, Table


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def _quote_values(values: list[str]) -> str:
    return " | ".join(f"'{v}'" for v in values)


def render_column(table: Table, col: Column) -> str:
    parts = [f"  {col.name} {col.type}"]
    if col.primary_key:
        parts.append("PK")
    elif not col.nullable:
        parts.append("NOT NULL")
    for fk in table.foreign_keys:
        if col.name in fk.columns:
            idx = fk.columns.index(col.name)
            ref = fk.ref_columns[idx] if idx < len(fk.ref_columns) else "?"
            parts.append(f"-> {fk.ref_table}({ref})")
    if col.allowed_values:
        parts.append(f"[{_quote_values(col.allowed_values)}]")
    elif col.sample_values:
        parts.append(f"(e.g. {', '.join(repr(v) for v in col.sample_values)})")
    line = " ".join(parts)
    if col.comment:
        line += f"  -- {_one_line(col.comment)}"
    return line


def render_table(table: Table) -> str:
    header = f"TABLE {table.name}"
    if table.comment:
        header += f"  -- {_one_line(table.comment)}"
    lines = [header] + [render_column(table, c) for c in table.columns]
    composite = [fk for fk in table.foreign_keys if len(fk.columns) > 1]
    for fk in composite:
        lines.append(
            f"  FOREIGN KEY ({', '.join(fk.columns)}) -> {fk.ref_table}({', '.join(fk.ref_columns)})"
        )
    if len(table.primary_key) > 1:
        lines.append(f"  PRIMARY KEY ({', '.join(table.primary_key)})")
    return "\n".join(lines)


def render_schema(snapshot: SchemaSnapshot) -> str:
    return "\n\n".join(render_table(t) for t in snapshot.tables)


def render_relationships(snapshot: SchemaSnapshot) -> list[str]:
    """One 'a.col -> b.col' line per foreign key, e.g. for a JOIN cheat-sheet in the prompt."""
    lines = []
    for t in snapshot.tables:
        for fk in t.foreign_keys:
            pairs = zip(fk.columns, fk.ref_columns, strict=False)
            lines.append(", ".join(f"{t.name}.{c} = {fk.ref_table}.{r}" for c, r in pairs))
    return lines
