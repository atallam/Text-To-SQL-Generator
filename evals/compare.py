"""Result-set comparison for execution-match scoring."""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any


def _norm_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (bool, int, float)):
        return round(float(v), 4) + 0.0  # 1 == 1.0 == True, and -0.0 -> 0.0
    if isinstance(v, bytes):
        return v.hex()
    return str(v).strip()


def normalize_rows(rows: Iterable[Sequence[Any]]) -> list[list[Any]]:
    return [[_norm_value(v) for v in row] for row in rows]


def _row_key(row: Sequence[Any], ignore_column_order: bool) -> tuple[str, ...]:
    vals = [repr(v) for v in row]
    return tuple(sorted(vals)) if ignore_column_order else tuple(vals)


def results_match(
    gold: Iterable[Sequence[Any]],
    pred: Iterable[Sequence[Any]],
    *,
    ordered: bool,
    ignore_column_order: bool = True,
) -> bool:
    """True when both result sets hold the same rows (order only matters if ``ordered``)."""
    g = [_row_key(r, ignore_column_order) for r in normalize_rows(gold)]
    p = [_row_key(r, ignore_column_order) for r in normalize_rows(pred)]
    if len(g) != len(p):
        return False
    return g == p if ordered else Counter(g) == Counter(p)


def _norm_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).strip().lower()


def sql_exact_match(a: str | None, b: str | None) -> bool:
    """Informational only: whitespace/case-insensitive string equality of two queries."""
    return bool(a and b and _norm_sql(a) == _norm_sql(b))
