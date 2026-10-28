"""Per-source config bundles (sources/<name>/source.yaml).

Adding a datasource = adding a folder with source.yaml, schema/glossary files and a read-only
role. Core pipeline modules must never hard-code table or source names.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from text2sql.config.settings import Settings, get_env, get_settings


class ConnectionConfig(BaseModel):
    readonly_url_env: str
    admin_url_env: str | None = None  # only used for seeding / bootstrap, never by the API


class SampleValuePolicy(BaseModel):
    """Which columns may have sample values shown to the LLM. Default: none."""

    default: str = "never"  # never | redact | allow
    allow: list[str] = Field(default_factory=list)  # "table.column"


class SourceConfig(BaseModel):
    name: str
    display_name: str
    dialect: str  # sqlglot / SQLAlchemy dialect name, e.g. "postgresql"
    schema_name: str = "public"
    connection: ConnectionConfig
    statement_timeout_ms: int = 10_000
    max_rows: int = 1000
    glossary: str | None = None  # file name inside the source folder
    sample_values: SampleValuePolicy = Field(default_factory=SampleValuePolicy)
    base_dir: Path | None = None  # filled by the loader

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
