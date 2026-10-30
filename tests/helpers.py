"""Shared test helpers (importable: ``from tests.helpers import ...``)."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from text2sql.config.sources import SourceConfig
from text2sql.schema.model import Column, ForeignKey, SchemaSnapshot, Table, compute_fingerprint

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SNAPSHOTS = ROOT / "tests" / "snapshots"


def source_config(**overrides) -> SourceConfig:
    data = dict(
        name="testsrc",
        display_name="Test Source",
        dialect="postgresql",
        connection={"readonly_url_env": "UNUSED_FOR_TESTS"},
    )
    data.update(overrides)
    return SourceConfig.model_validate(data)


def make_snapshot(spec: dict[str, dict], dialect: str = "postgresql") -> SchemaSnapshot:
    """spec = {"orders": {"cols": [...], "fks": {"supplier_id": "suppliers"}, "comment": "..."}}"""
    tables = []
    for name, t in spec.items():
        fks = [
            ForeignKey(columns=[c], ref_table=ref, ref_columns=["id"]) for c, ref in t.get("fks", {}).items()
        ]
        cols = [
            Column(
                name=c, type="integer" if c == "id" or c.endswith("_id") else "text", primary_key=c == "id"
            )
            for c in t["cols"]
        ]
        tables.append(
            Table(name=name, comment=t.get("comment"), columns=cols, primary_key=["id"], foreign_keys=fks)
        )
    return SchemaSnapshot(
        source="testsrc",
        dialect=dialect,
        schema_name="public",
        tables=tables,
        fingerprint=compute_fingerprint(tables),
    )


class StubEmbedder:
    """Keyword-count embedder: gives tests exact control over similarity scores."""

    name = "stub"

    def __init__(self, keywords: list[str]):
        self.keywords = [k.lower() for k in keywords]

    def _vec(self, text: str) -> np.ndarray:
        low = text.lower()
        v = np.array([float(low.count(k)) for k in self.keywords], dtype=np.float32)
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return (
            np.vstack([self._vec(t) for t in texts])
            if texts
            else np.zeros((0, len(self.keywords)), np.float32)
        )

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


def assert_snapshot(name: str, text: str) -> None:
    """Golden-file check. Review the diff, then regenerate with UPDATE_SNAPSHOTS=1 (PowerShell:
    ``$env:UPDATE_SNAPSHOTS = "1"; pytest tests/unit/test_prompts.py``)."""
    path = SNAPSHOTS / f"{name}.txt"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return
    assert path.is_file(), f"missing snapshot {path.name}; create it with UPDATE_SNAPSHOTS=1"
    expected = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text == expected, (
        f"prompt snapshot {path.name} changed. If intended, bump the prompt version or regenerate with "
        "UPDATE_SNAPSHOTS=1 and review the diff."
    )


# -- Phase 2 helpers ---------------------------------------------------------------------------
class ScriptedLLM:
    """Stands in for LLMClient. ``script`` items are consumed one per call: a GeneratedSQL, an Exception to
    raise, or a callable ``(agent, system, user) -> GeneratedSQL`` (lets a test simulate a *compromised*
    model that reacts to what is in the prompt). The last item repeats once the script runs out."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []

    def complete_structured(self, *, agent_name, system, user, response_model, model=None, max_tokens=None):
        from text2sql.llm.client import StructuredResult, Usage

        self.calls.append({"agent": agent_name, "system": system, "user": user})
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if callable(item) and not isinstance(item, Exception):
            item = item(agent_name, system, user)
        if isinstance(item, Exception):
            raise item
        return StructuredResult(parsed=item, usage=Usage(100, 20), latency_ms=5, model="scripted", attempts=1)


def gen(sql: str = "SELECT 1", **kw):
    from text2sql.generation.generator import GeneratedSQL

    kw.setdefault("explanation", "test query")
    kw.setdefault("self_confidence", 0.9)
    return GeneratedSQL(sql=sql, **kw)


# -- Phase 3 helpers ---------------------------------------------------------------------------
def exec_result(columns, rows, *, truncated: bool = False, sql: str = "SELECT 1"):
    from text2sql.execution.executor import ExecutionResult

    return ExecutionResult(
        columns=list(columns),
        rows=[list(r) for r in rows],
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=1,
        result_hash="h",
        sql=sql,
    )


class FakeExecutor:
    """Executor stand-in: ``answers`` maps a substring of the SQL to (columns, rows). First match wins."""

    def __init__(self, answers: dict[str, tuple[list[str], list[list]]] | None = None):
        self.answers = answers or {}
        self.sqls: list[str] = []

    def run(self, sql: str):
        from text2sql.execution.executor import ExecutionError

        self.sqls.append(sql)
        for needle, (cols, rows) in self.answers.items():
            if needle in sql:
                return exec_result(cols, rows, sql=sql)
        raise ExecutionError("sql_error", f"no fake answer for: {sql}")


def typed_snapshot(spec: dict[str, dict]):
    """spec = {"t": {"cols": {"id": "integer", "d": "date"}, "pk": ["id"], "fks": {"cat_id": "c"}}}"""
    from text2sql.schema.model import Column, ForeignKey

    tables = []
    for name, t in spec.items():
        pk = t.get("pk", ["id"])
        cols = [Column(name=c, type=ty, primary_key=c in pk) for c, ty in t["cols"].items()]
        fks = [ForeignKey(columns=[c], ref_table=r, ref_columns=["id"]) for c, r in t.get("fks", {}).items()]
        tables.append(Table(name=name, columns=cols, primary_key=pk, foreign_keys=fks))
    return SchemaSnapshot(
        source="testsrc",
        dialect="postgresql",
        schema_name="public",
        tables=tables,
        fingerprint=compute_fingerprint(tables),
    )
