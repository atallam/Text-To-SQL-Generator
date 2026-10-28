from __future__ import annotations

from text2sql.config.sources import SourceConfig, load_source
from text2sql.connectors.base import Connector
from text2sql.connectors.postgres import PostgresConnector

_DIALECTS: dict[str, type[Connector]] = {"postgresql": PostgresConnector}


def register_dialect(dialect: str, cls: type[Connector]) -> None:
    _DIALECTS[dialect] = cls


def get_connector(source: str | SourceConfig) -> Connector:
    cfg = load_source(source) if isinstance(source, str) else source
    try:
        return _DIALECTS[cfg.dialect](cfg)
    except KeyError:
        raise ValueError(f"No connector for dialect {cfg.dialect!r} (source {cfg.name!r})") from None
