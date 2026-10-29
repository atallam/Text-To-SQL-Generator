"""Few-shot examples: schema-specific question -> SQL pairs, retrieved by similarity.

Examples live in sources/<name>/examples.yaml:

    examples:
      - question: How much did I spend on groceries last month?
        tables: [transactions, categories]
        sql: |
          SELECT ...

Only examples whose tables are ALL inside the filtered schema are offered, so the prompt never shows
SQL against a table the model cannot see. Later (Phase 4) user-approved corrections join this set.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml
from pydantic import BaseModel, Field

from text2sql.schema.embeddings import Embedder


class FewShotExample(BaseModel):
    question: str
    sql: str
    tables: list[str] = Field(min_length=1)


def load_examples(path: Path, today_sql: str = "CURRENT_DATE") -> list[FewShotExample]:
    """``{TODAY}`` in an example is replaced by the source's date expression (see SourceConfig.today_sql)."""
    raw = path.read_text(encoding="utf-8").replace("{TODAY}", today_sql)
    data = yaml.safe_load(raw) or {}
    return [FewShotExample.model_validate(e) for e in data.get("examples", [])]


class ExampleSelector:
    def __init__(self, examples: list[FewShotExample], embedder: Embedder):
        self.examples = examples
        self.embedder = embedder
        self._vecs = (
            embedder.embed_documents([e.question for e in examples]) if examples else np.zeros((0, 1))
        )

    def select(
        self, question: str, available_tables: set[str] | None = None, k: int = 4
    ) -> list[FewShotExample]:
        if not self.examples or k <= 0:
            return []
        sims = self._vecs @ self.embedder.embed_query(question)
        order = sorted(range(len(self.examples)), key=lambda i: (-float(sims[i]), i))
        picked: list[FewShotExample] = []
        for i in order:
            ex = self.examples[i]
            if available_tables is not None and not set(ex.tables) <= available_tables:
                continue
            picked.append(ex)
            if len(picked) == k:
                break
        return picked
