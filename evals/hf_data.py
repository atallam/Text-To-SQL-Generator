"""Load text-to-SQL test data from the Hugging Face Hub.

Default dataset: ``gretelai/synthetic_text_to_sql`` (Apache-2.0, ~105k rows). Every row carries
its own CREATE TABLE + INSERT script (``sql_context``), a question (``sql_prompt``) and verified
SQL (``sql``), so we can score by executing queries instead of comparing SQL text. Rows are tagged
with a business domain, which lets us focus the test set on supply-chain-adjacent domains.

Public datasets need no login. ``hf auth login`` (or an HF_TOKEN env var) only raises rate limits.
"""
from __future__ import annotations

from typing import Any

DATASET_ID = "gretelai/synthetic_text_to_sql"

COLUMNS = ["id", "domain", "sql_complexity", "sql_task_type", "sql_prompt", "sql_context", "sql"]

# Closest domains in the dataset to supply chain work. Override with --domains.
SUPPLY_CHAIN_DOMAINS: tuple[str, ...] = (
    "logistics",
    "transportation",
    "public transportation",
    "maritime",
    "ocean shipping",
    "manufacturing",
    "automotive",
    "retail",
    "fashion retail",
)


def load_rows(dataset_id: str = DATASET_ID, split: str = "test") -> list[dict[str, Any]]:
    """Download (cached after the first run) and return the rows as plain dicts."""
    try:
        from datasets import load_dataset
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit('Missing the "datasets" package. Run: pip install -e ".[evals]"') from exc

    ds = load_dataset(dataset_id, split=split)
    ds = ds.select_columns([c for c in COLUMNS if c in ds.column_names])
    return [dict(row) for row in ds]
