"""Connector interface: the pipeline talks to this, never to a specific database."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager

from sqlalchemy.engine import Connection, Engine

from text2sql.config.sources import SourceConfig


class Connector(ABC):
    dialect: str

    def __init__(self, config: SourceConfig):
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @abstractmethod
    def engine(self) -> Engine:
        """SQLAlchemy engine bound to the READ-ONLY credentials."""

    @abstractmethod
    def readonly_connection(self, timeout_ms: int | None = None) -> AbstractContextManager[Connection]:
        """Connection inside a read-only transaction with a statement timeout; always rolled back."""

    def health_check(self) -> bool:
        from sqlalchemy import text

        with self.readonly_connection() as conn:
            return conn.execute(text("SELECT 1")).scalar() == 1

    def dispose(self) -> None:
        self.engine().dispose()


Rows = Iterator[dict]
