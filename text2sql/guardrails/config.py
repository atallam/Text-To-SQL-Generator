"""Guardrail configuration (``text2sql/config/guardrails.yaml``, optionally overridden per source).

Every rule has an ID. Rules G001-G003 (single statement, read-only statement allow-list, forbidden
constructs) are the safety core and cannot be switched off or downgraded: a config that tries is
rejected at load time rather than silently weakening the system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from text2sql.config.sources import SourceConfig

DEFAULTS_PATH = Path(__file__).resolve().parents[1] / "config" / "guardrails.yaml"

Mode = Literal["block", "warn", "off"]


class Rule(BaseModel):
    mode: Mode = "block"
    # A repairable violation is fed back to the model for another attempt; a non-repairable one
    # (security rules) ends the request immediately.
    repairable: bool = False


class LockedRule(Rule):
    """Safety-core rule: always blocks."""

    @field_validator("mode")
    @classmethod
    def _must_block(cls, v: str) -> str:
        if v != "block":
            raise ValueError("this rule is part of the safety core and must stay mode: block")
        return v


def _default_denied_functions() -> list[str]:
    """A bare ``GuardrailConfig()`` must be as strict as the shipped yaml, never weaker."""
    return list(yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8"))["functions"]["deny"])


class FunctionRule(Rule):
    # fnmatch patterns, matched case-insensitively against the bare function name (schema prefix removed)
    deny: list[str] = Field(default_factory=_default_denied_functions)
    allow: list[str] = Field(default_factory=list)  # exceptions to `deny`


class TableRule(Rule):
    allowed_schemas: list[str] = Field(default_factory=list)  # default: the source's schema only
    allow_catalog: bool = False  # pg_catalog / information_schema / pg_* tables
    extra_tables: list[str] = Field(default_factory=list)  # tables allowed even if not introspected


class RowLimitRule(BaseModel):
    mode: Literal["rewrite", "reject", "off"] = "rewrite"
    max_rows: int = Field(1000, ge=1)


class DepthRule(Rule):
    max_depth: int = Field(3, ge=0)


class ExplainRule(Rule):
    max_total_cost: float = Field(1_000_000.0, gt=0)
    max_plan_rows: float = Field(5_000_000.0, gt=0)


class GuardrailConfig(BaseModel):
    dialect: str = "postgres"  # sqlglot dialect
    max_sql_chars: int = Field(20_000, ge=100)
    statement_timeout_ms: int = Field(10_000, ge=100)  # enforced by the connector; recorded for audit

    parse: LockedRule = Field(default_factory=LockedRule)  # G000
    single_statement: LockedRule = Field(default_factory=LockedRule)  # G001
    statement_allowlist: LockedRule = Field(default_factory=LockedRule)  # G002
    forbidden_constructs: LockedRule = Field(default_factory=LockedRule)  # G003
    functions: FunctionRule = Field(default_factory=FunctionRule)  # G004
    tables: TableRule = Field(default_factory=lambda: TableRule(repairable=True))  # G005
    row_limit: RowLimitRule = Field(default_factory=RowLimitRule)  # G006
    subquery_depth: DepthRule = Field(default_factory=lambda: DepthRule(repairable=True))  # G007
    explain: ExplainRule = Field(default_factory=lambda: ExplainRule(repairable=True))  # G008

    @model_validator(mode="after")
    def _sane(self) -> GuardrailConfig:
        for pattern in self.functions.deny + self.functions.allow:
            if not pattern.strip():
                raise ValueError("empty function pattern")
        return self


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_guardrail_config(
    source: SourceConfig | None = None, path: Path | None = None, overrides: dict[str, Any] | None = None
) -> GuardrailConfig:
    """Defaults, then the source's own ``guardrails`` file, then the source's headline limits, then
    ``overrides`` (used by tests)."""
    data = _read(path or DEFAULTS_PATH)
    if source is not None:
        if source.base_dir and source.guardrails:
            data = _deep_merge(data, _read(source.base_dir / source.guardrails))
        data.setdefault("row_limit", {})["max_rows"] = source.max_rows
        data["statement_timeout_ms"] = source.statement_timeout_ms
        data["dialect"] = _SQLGLOT_DIALECTS.get(source.dialect, source.dialect)
        schema = data.setdefault("tables", {}).setdefault("allowed_schemas", [])
        if not schema:
            data["tables"]["allowed_schemas"] = [source.schema_name]
    if overrides:
        data = _deep_merge(data, overrides)
    return GuardrailConfig.model_validate(data)


_SQLGLOT_DIALECTS = {"postgresql": "postgres", "postgres": "postgres", "sqlite": "sqlite", "mysql": "mysql"}
