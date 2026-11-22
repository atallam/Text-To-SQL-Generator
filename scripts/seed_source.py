"""Create schema, seed synthetic data and the read-only role for any source with a ``seed_module``.

Usage (PowerShell):  python -m scripts.seed_source procurement
Needs the admin URL variable named in source.yaml (connection.admin_url_env) and the read-only password
(connection.readonly_password_env, default <SOURCE>_READONLY_PASSWORD). The database is created if missing.
The SELECT-only role comes from connection.readonly_role (default t2s_readonly).
"""
from __future__ import annotations

import argparse

from text2sql.config.settings import get_env, get_settings
from text2sql.config.sources import load_source
from text2sql.db.bootstrap import apply_schema, ensure_database, ensure_readonly_role, seed_tables
from text2sql.observability.logging import configure_logging, get_logger


def seed_source(name: str) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    log = get_logger("seed")
    src = load_source(name, settings)
    if not src.seed_module:
        raise SystemExit(f"Source {name!r} has no seed_module in source.yaml (nothing to seed).")
    admin_url = src.admin_url()

    if ensure_database(admin_url):
        log.info("database_created", source=name)
    apply_schema(admin_url, src.base_dir / src.schema_file)
    counts = seed_tables(admin_url, module=src.seed_module)
    log.info("seeded", source=name, **counts)

    role = src.connection.readonly_role
    password_var = src.connection.readonly_password_env or f"{name.upper()}_READONLY_PASSWORD"
    password = get_env(password_var)
    if not password:
        raise SystemExit(f"Set {password_var} in .env")
    ensure_readonly_role(admin_url, role, password, src.schema_name, src.statement_timeout_ms)
    log.info("readonly_role_ready", source=name, role=role)


def main() -> None:
    p = argparse.ArgumentParser(description="Seed a source's database.")
    p.add_argument("source", help="folder name under sources/, e.g. wealthlens or procurement")
    seed_source(p.parse_args().source)


if __name__ == "__main__":
    main()
