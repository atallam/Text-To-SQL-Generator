"""Create schema, seed synthetic data and the read-only role for the WealthLens source.

Usage (PowerShell):  python -m scripts.seed_wealthlens
Needs WEALTHLENS_ADMIN_URL and WEALTHLENS_READONLY_PASSWORD in the environment / .env.
"""
from __future__ import annotations

import os

from text2sql.config.settings import get_settings
from text2sql.config.sources import load_source
from text2sql.db.bootstrap import apply_schema, ensure_readonly_role, seed_tables
from text2sql.observability.logging import configure_logging, get_logger


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    log = get_logger("seed")
    src = load_source("wealthlens", settings)
    admin_url = src.admin_url()

    apply_schema(admin_url, src.base_dir / "schema.sql")
    counts = seed_tables(admin_url)
    log.info("seeded", **counts)

    role = os.environ.get("WEALTHLENS_READONLY_USER", "t2s_readonly")
    password = os.environ.get("WEALTHLENS_READONLY_PASSWORD")
    if not password:
        raise SystemExit("Set WEALTHLENS_READONLY_PASSWORD")
    ensure_readonly_role(admin_url, role, password, src.schema_name, src.statement_timeout_ms)
    log.info("readonly_role_ready", role=role)


if __name__ == "__main__":
    main()
