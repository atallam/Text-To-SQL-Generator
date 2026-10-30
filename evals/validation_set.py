"""Loader for the labeled hallucination-detection set (``evals/validation/variants.yaml``)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

PATH = Path(__file__).parent / "validation" / "variants.yaml"
CATEGORIES = ("wrong_join", "wrong_filter", "wrong_aggregation", "wrong_date_range")


class Variant(BaseModel):
    id: str
    case: str
    question: str
    reference: str
    sql: str
    label: str  # "wrong" | "correct"
    category: str | None = None
    note: str = ""


def load_variants(path: Path = PATH) -> list[Variant]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = data["cases"]
    out: list[Variant] = []
    for label, key in (("wrong", "wrong"), ("correct", "correct")):
        for item in data[key]:
            case = cases[item["case"]]
            category = item.get("category")
            if label == "wrong" and category not in CATEGORIES:
                raise ValueError(f"{item['id']}: category must be one of {CATEGORIES}")
            out.append(
                Variant(
                    id=item["id"],
                    case=item["case"],
                    question=case["question"],
                    reference=case["reference"].strip(),
                    sql=item["sql"].strip(),
                    label=label,
                    category=category,
                    note=item.get("note", ""),
                )
            )
    ids = [v.id for v in out]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate variant ids")
    return out
