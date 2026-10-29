"""Application settings (env / .env). Secrets never have defaults."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


def get_env(var: str, default: str | None = None, env_file: Path | None = None) -> str | None:
    """Read a variable from the process environment, falling back to the project .env file."""
    value = os.environ.get(var)
    if value:
        return value
    value = dotenv_values(env_file or ENV_FILE).get(var)
    return value or default


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

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

    # Embeddings for schema relevance and few-shot retrieval. "lexical" needs no extra install.
    embedding_provider: Literal["sentence-transformers", "lexical"] = "sentence-transformers"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    schema_refresh_s: float = 60.0  # how often the schema fingerprint is re-checked for DB changes

    log_level: str = "INFO"
    log_json: bool = False

    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
