"""Application settings (env / .env). Secrets never have defaults."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: SecretStr | None = None
    llm_model_generation: str = "claude-sonnet-5-5"
    llm_model_judge: str = "claude-haiku-5-5"
    llm_max_tokens: int = 2048
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3  # transient API errors (rate limit, 5xx, network)
    llm_max_output_attempts: int = 2  # re-asks after schema-invalid output
    llm_retry_wait_s: float = 1.0  # exponential backoff multiplier

    default_source: str = "wealthlens"
    sources_dir: Path = PROJECT_ROOT / "sources"

    log_level: str = "INFO"
    log_json: bool = False

    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
