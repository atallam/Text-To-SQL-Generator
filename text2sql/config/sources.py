"""Per-source config bundles (sources/<name>/source.yaml).

Adding a datasource = adding a folder with source.yaml, schema/glossary files and a read-only
role. Core pipeline modules must never hard-code table or source names.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from text2sql.config.settings import Settings, get_env, get_settings


class ConnectionConfig(BaseModel):
    readonly_url_env: str
    admin_url_env: str | None = None  # only used for seeding / bootstrap, never by the API


class SampleValuePolicy(BaseModel):
    """Which columns may have sample values shown to the LLM. Default: none."""

    default: Literal["never", "redact", "allow"] = "never"
    allow: list[str] = Field(default_factory=list)  # "table.column": always shown as-is
    max_distinct: int = 12  # columns with more distinct values are treated as high-cardinality: no samples
    max_value_length: int = 40  # longer values look like free text: no samples

    def mode_for(self, table: str, column: str) -> str:
        """Effective policy for one column. An explicit allow-list entry beats the default."""
        return "allow" if f"{table}.{column}" in self.allow else self.default


class RelevanceConfig(BaseModel):
    """Table-relevance filter knobs (see text2sql.schema.relevance)."""

    threshold: float = 0.0  # absolute cosine floor
    relative_to_top: float = 0.85  # keep tables scoring >= this fraction of the best table
    min_tables: int = 2
    max_tables: int = 8
    column_weight: float = 0.95  # a column match counts this much toward its table's score
    neighbors: Literal["none", "outgoing", "both"] = "outgoing"  # FK neighbors always included
    glossary_boost: bool = True  # tables named in a matched glossary definition are always included


class SourceConfig(BaseModel):
    name: str
    display_name: str
    dialect: str  # sqlglot / SQLAlchemy dialect name, e.g. "postgresql"
    schema_name: str = "public"
    connection: ConnectionConfig
    statement_timeout_ms: int = 10_000
    max_rows: int = 1000
    glossary: str | None = None  # file name inside the source folder
    examples: str | None = None  # few-shot question -> SQL pairs, file name inside the source folder
    exclude_tables: list[str] = Field(default_factory=list)  # never introspected or shown to the LLM
    # "Today" for relative dates ("last month"). None = the real CURRENT_DATE. Demo data with a fixed
    # end date sets this so questions keep returning rows as real time moves on.
    as_of_date: date | None = None
    sample_values: SampleValuePolicy = Field(default_factory=SampleValuePolicy)
    relevance: RelevanceConfig = Field(default_factory=RelevanceConfig)
    base_dir: Path | None = None  # filled by the loader

    @property
    def today_sql(self) -> str:
        return f"DATE '{self.as_of_date.isoformat()}'" if self.as_of_date else "CURRENT_DATE"

    def readonly_url(self) -> str:
        return _require_env(self.connection.readonly_url_env)

    def admin_url(self) -> str:
        if not self.connection.admin_url_env:
            raise RuntimeError(f"Source {self.name!r} defines no admin_url_env")
        return _require_env(self.connection.admin_url_env)


def _require_env(var: str) -> str:
    value = get_env(var)
    if not value:
        raise RuntimeError(
            f"{var} is not set. Add it to the .env file in the project root (see .env.example) "
            "or set it in your shell."
        )
    return value


def load_source(name: str, settings: Settings | None = None) -> SourceConfig:
    settings = settings or get_settings()
    path = settings.sources_dir / name / "source.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Unknown source {name!r}: {path} not found")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = SourceConfig.model_validate(data)
    return cfg.model_copy(update={"base_dir": path.parent})


def list_sources(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    return sorted(p.parent.name for p in settings.sources_dir.glob("*/source.yaml"))
