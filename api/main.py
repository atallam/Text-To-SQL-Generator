"""ASGI entry point.

Run (PowerShell):  uvicorn api.main:app --reload --port 8000
Docs:              http://localhost:8000/docs
"""

from __future__ import annotations

from fastapi import FastAPI

from api.app import create_app, log
from api.settings import get_api_settings
from text2sql.config.settings import get_settings
from text2sql.observability.logging import configure_logging


def _build() -> FastAPI:
    configure_logging(get_settings().log_level, get_settings().log_json)
    app = create_app()
    cfg = get_api_settings()
    if cfg.api_auth_disabled:
        log.warning("auth_disabled", note="API_AUTH_DISABLED=true; never use this outside a local demo")
    elif not cfg.keys:
        log.error("no_api_keys", note="Set API_KEYS in .env; protected endpoints will answer 503")
    return app


app = _build()
