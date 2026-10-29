"""Structured, database-agnostic description of a source schema."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field


class ForeignKey(BaseModel):
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


class Column(BaseModel):
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    comment: str | None = None
    # Exhaustive value list taken from a CHECK constraint. This is schema, not data, so it is
    # always safe to show to the LLM regardless of the sample-value policy.
    allowed_values: list[str] | None = None
    # Sampled data values. Only filled when the source's sample_values policy allows it.
    sample_values: list[str] = Field(default_factory=list)


class Table(BaseModel):
    name: str
    comment: str | None = None
    columns: list[Column]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)

    def column(self, name: str) -> Column:
        for col in self.columns:
            if col.name == name:
                return col
        raise KeyError(f"{self.name}.{name}")


def compute_fingerprint(tables: list[Table]) -> str:
    """Hash of the schema STRUCTURE only. Sample values are excluded so ordinary data changes
    do not trigger a rebuild; any DDL change (table, column, type, key, comment) does."""
    payload = []
    for t in sorted(tables, key=lambda x: x.name):
        payload.append(
            {
                "t": t.name,
                "c": t.comment,
                "pk": t.primary_key,
                "fk": sorted(
                    (fk.model_dump() for fk in t.foreign_keys), key=lambda d: json.dumps(d, sort_keys=True)
                ),
                "cols": [
                    [c.name, c.type, c.nullable, c.primary_key, c.comment, c.allowed_values]
                    for c in sorted(t.columns, key=lambda x: x.name)
                ],
            }
        )
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class SchemaSnapshot(BaseModel):
    source: str
    dialect: str
    schema_name: str | None = None
    tables: list[Table]
    fingerprint: str

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    def table(self, name: str) -> Table:
        for t in self.tables:
            if t.name == name:
                return t
        raise KeyError(name)

    def neighbors(self, name: str, direction: Literal["outgoing", "incoming", "both"] = "both") -> set[str]:
        """Tables linked to ``name`` by a foreign key. Outgoing = tables it references."""
        known = set(self.table_names)
        found: set[str] = set()
        if direction in ("outgoing", "both"):
            found |= {fk.ref_table for fk in self.table(name).foreign_keys if fk.ref_table in known}
        if direction in ("incoming", "both"):
            found |= {
                t.name
                for t in self.tables
                for fk in t.foreign_keys
                if fk.ref_table == name and t.name != name
            }
        found.discard(name)
        return found

    def subset(self, names: list[str]) -> SchemaSnapshot:
        """A copy holding only ``names`` (in the given order). Foreign keys that point outside the
        subset are dropped so the prompt never mentions a table the model cannot see."""
        keep = [n for n in names if n in set(self.table_names)]
        keep_set = set(keep)
        tables = []
        for n in keep:
            t = self.table(n)
            fks = [fk for fk in t.foreign_keys if fk.ref_table in keep_set]
            tables.append(t.model_copy(update={"foreign_keys": fks}))
        return self.model_copy(update={"tables": tables})
