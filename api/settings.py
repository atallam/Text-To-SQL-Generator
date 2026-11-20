"""API settings (env / .env). Secrets never have defaults; auth fails closed."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from text2sql.config.settings import ENV_FILE, PROJECT_ROOT


def _split(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value)  # type: ignore[arg-type]


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # Comma-separated keys. Empty = protected endpoints answer 503 (fail closed), /healthz still works.
    api_keys: str = ""
    # Keys allowed to review feedback (approve/reject). Empty = review endpoints answer 403.
    api_admin_keys: str = ""
    # Explicit opt-out for throw-away local demos only.
    api_auth_disabled: bool = False

    api_cors_origins: str = "http://localhost:8501,http://127.0.0.1:8501"
    api_rate_limit_per_minute: int = Field(default=30, ge=1)
    api_max_body_bytes: int = Field(default=64_000, ge=1_000)

    api_db_path: Path = PROJECT_ROOT / "data" / "app.db"  # history + feedback (SQLite)
    api_history_rows_kept: int = Field(default=200, ge=1)  # result rows stored per history entry

    @field_validator("api_keys", "api_admin_keys", "api_cors_origins", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @property
    def keys(self) -> list[str]:
        return _split(self.api_keys)

    @property
    def admin_keys(self) -> list[str]:
        return _split(self.api_admin_keys)

    @property
    def cors_origins(self) -> list[str]:
        return _split(self.api_cors_origins)


@lru_cache
def get_api_settings() -> ApiSettings:
    return ApiSettings()
