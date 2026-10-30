"""Validation settings: weights, caps, thresholds. Loaded from ``text2sql/config/confidence.yaml`` and
optionally overridden per source (``validation:`` in source.yaml)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from text2sql.config.sources import SourceConfig

DEFAULTS_PATH = Path(__file__).resolve().parents[1] / "config" / "confidence.yaml"
SIGNALS = ("backtranslation", "multiquery", "sanity", "schema_coverage", "syntax")


class Caps(BaseModel):
    disagreement: float = 0.5
    guardrail_warning: float = 0.6
    misalignment: float = 0.6


class Levels(BaseModel):
    high: float = 0.8
    medium: float = 0.5


class BackTranslationConfig(BaseModel):
    flag_below: float = 0.75
    dimension_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "entity": 0.20,
            "measure": 0.25,
            "filters": 0.20,
            "time_range": 0.15,
            "grouping": 0.10,
            "output_shape": 0.10,
        }
    )
    mismatch_cap: float = 0.6


class MultiQueryConfig(BaseModel):
    enabled: bool = True
    complexity_threshold: int = 3
    abs_tol: float = 0.01
    rel_tol: float = 1e-4
    compare_rows: int = 200


class SanityConfig(BaseModel):
    null_heavy_ratio: float = 0.5
    null_min_rows: int = 5
    date_slack_days: int = 31


class ValidationConfig(BaseModel):
    calibrated: bool = False
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "backtranslation": 0.30,
            "multiquery": 0.25,
            "sanity": 0.20,
            "schema_coverage": 0.15,
            "syntax": 0.10,
        }
    )
    caps: Caps = Field(default_factory=Caps)
    warning_ignore_rules: list[str] = Field(default_factory=lambda: ["G006"])
    levels: Levels = Field(default_factory=Levels)
    backtranslation: BackTranslationConfig = Field(default_factory=BackTranslationConfig)
    multiquery: MultiQueryConfig = Field(default_factory=MultiQueryConfig)
    sanity: SanityConfig = Field(default_factory=SanityConfig)

    @field_validator("weights")
    @classmethod
    def _weights(cls, v: dict[str, float]) -> dict[str, float]:
        unknown = set(v) - set(SIGNALS)
        if unknown:
            raise ValueError(f"unknown signal(s) in weights: {sorted(unknown)}; expected {list(SIGNALS)}")
        if any(w < 0 for w in v.values()) or sum(v.values()) <= 0:
            raise ValueError("weights must be non-negative and not all zero")
        return v


def _read(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_validation_config(
    source: SourceConfig | None = None, path: Path | None = None, overrides: dict[str, Any] | None = None
) -> ValidationConfig:
    data = _read(path or DEFAULTS_PATH)
    if source is not None and source.base_dir and source.validation:
        data = _merge(data, _read(source.base_dir / source.validation))
    if overrides:
        data = _merge(data, overrides)
    return ValidationConfig.model_validate(data)
