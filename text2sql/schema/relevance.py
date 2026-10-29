"""Relevance filter: which tables does this question need?

Large schemas waste context and confuse the model, so the prompt only carries the tables a question
is likely to touch. Selection, in order:

1. Semantic: embed the question and every table/column description; a table scores as the best of
   its own description and (weighted) its best column. Keep tables scoring at least
   ``relative_to_top`` of the best table (and above ``threshold``), between ``min_tables`` and
   ``max_tables``.
2. Glossary: tables named in the definition of a glossary term that the question mentions are always
   included (deterministic, high precision).
3. Foreign-key neighbors of everything selected are added, so JOIN partners are never missing.

Embeddings of the schema are cached per schema fingerprint and rebuilt automatically when the
database structure changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from text2sql.config.sources import RelevanceConfig
from text2sql.schema.embeddings import Embedder
from text2sql.schema.glossary import Glossary
from text2sql.schema.model import SchemaSnapshot, Table

Reason = Literal["semantic", "glossary", "fk-neighbor"]


@dataclass(frozen=True)
class TableSelection:
    name: str
    score: float
    reason: Reason


@dataclass
class RelevanceResult:
    selections: list[TableSelection] = field(default_factory=list)
    all_scores: dict[str, float] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.selections]


def _words(identifier: str) -> str:
    return identifier.replace("_", " ")


def table_document(table: Table, related_terms: list[str]) -> str:
    parts = [_words(table.name) + "."]
    if table.comment:
        parts.append(table.comment.rstrip(".") + ".")
    parts.append("Columns: " + ", ".join(_words(c.name) for c in table.columns) + ".")
    if related_terms:
        parts.append("Related terms: " + ", ".join(related_terms) + ".")
    return " ".join(parts)


def column_document(table: Table, col) -> str:
    text = f"{_words(table.name)} {_words(col.name)}"
    if col.comment:
        text += f": {col.comment.rstrip('.')}"
    values = col.allowed_values or []
    if values:
        text += ". Values: " + ", ".join(values)
    return text


@dataclass
class _Index:
    fingerprint: str
    table_names: list[str]
    table_vecs: np.ndarray
    col_vecs: np.ndarray
    col_owner: np.ndarray  # table index for every column vector


class RelevanceFilter:
    def __init__(
        self, embedder: Embedder, config: RelevanceConfig | None = None, glossary: Glossary | None = None
    ):
        self.embedder = embedder
        self.config = config or RelevanceConfig()
        self.glossary = glossary or Glossary.empty()
        self._index: _Index | None = None
        self.rebuilds = 0  # observable for tests / metrics

    # -- index ----------------------------------------------------------------------------
    def _related_terms(self, table: Table, names: list[str]) -> list[str]:
        out = []
        for term in self.glossary.terms.values():
            if table.name in self.glossary.tables_for(term, names):
                out.append(term.name.replace("_", " "))
        return out

    def _ensure_index(self, snapshot: SchemaSnapshot) -> _Index:
        if self._index is not None and self._index.fingerprint == snapshot.fingerprint:
            return self._index
        names = snapshot.table_names
        table_docs = [table_document(t, self._related_terms(t, names)) for t in snapshot.tables]
        col_docs, owners = [], []
        for i, t in enumerate(snapshot.tables):
            for c in t.columns:
                col_docs.append(column_document(t, c))
                owners.append(i)
        self._index = _Index(
            fingerprint=snapshot.fingerprint,
            table_names=names,
            table_vecs=self.embedder.embed_documents(table_docs),
            col_vecs=self.embedder.embed_documents(col_docs),
            col_owner=np.asarray(owners, dtype=int),
        )
        self.rebuilds += 1
        return self._index

    # -- scoring --------------------------------------------------------------------------
    def score_tables(self, question: str, snapshot: SchemaSnapshot) -> dict[str, float]:
        idx = self._ensure_index(snapshot)
        q = self.embedder.embed_query(question)
        table_sims = idx.table_vecs @ q
        col_sims = idx.col_vecs @ q if len(idx.col_vecs) else np.zeros(0)
        scores: dict[str, float] = {}
        for i, name in enumerate(idx.table_names):
            best_col = col_sims[idx.col_owner == i].max() if (idx.col_owner == i).any() else 0.0
            scores[name] = float(max(table_sims[i], self.config.column_weight * best_col))
        return scores

    # -- selection ------------------------------------------------------------------------
    def select(self, question: str, snapshot: SchemaSnapshot) -> RelevanceResult:
        cfg = self.config
        scores = self.score_tables(question, snapshot)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        top = ranked[0][1] if ranked else 0.0
        cutoff = max(cfg.threshold, cfg.relative_to_top * top)

        chosen: list[TableSelection] = []
        for rank, (name, score) in enumerate(ranked):
            if len(chosen) >= cfg.max_tables:
                break
            if rank < cfg.min_tables or (score >= cutoff and score > 0):
                chosen.append(TableSelection(name, score, "semantic"))
        have = {s.name for s in chosen}

        if cfg.glossary_boost:
            for term in self.glossary.matches(question):
                for name in self.glossary.tables_for(term, snapshot.table_names):
                    if name not in have:
                        chosen.append(TableSelection(name, scores.get(name, 0.0), "glossary"))
                        have.add(name)

        if cfg.neighbors != "none":
            direction = "outgoing" if cfg.neighbors == "outgoing" else "both"
            extra: set[str] = set()
            for sel in list(chosen):
                extra |= snapshot.neighbors(sel.name, direction)
            for name in sorted(extra - have):
                chosen.append(TableSelection(name, scores.get(name, 0.0), "fk-neighbor"))
                have.add(name)

        return RelevanceResult(selections=chosen, all_scores=scores)
