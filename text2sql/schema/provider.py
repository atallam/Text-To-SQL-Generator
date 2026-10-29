"""Keeps the schema context fresh without re-reading the database on every question.

``get()`` returns the cached snapshot. At most every ``refresh_s`` seconds it recomputes the cheap,
structure-only fingerprint; if it changed (DDL happened) the full snapshot is rebuilt. Anything
keyed on ``snapshot.fingerprint`` (e.g. the relevance index) then refreshes by itself.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from text2sql.observability.logging import get_logger
from text2sql.schema.model import SchemaSnapshot

log = get_logger("schema")


class SchemaProvider:
    def __init__(
        self,
        build: Callable[[], SchemaSnapshot],
        fingerprint: Callable[[], str],
        refresh_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._build = build
        self._fingerprint = fingerprint
        self._refresh_s = refresh_s
        self._clock = clock
        self._snapshot: SchemaSnapshot | None = None
        self._checked_at = 0.0
        self._lock = threading.Lock()
        self.rebuilds = 0  # observable for tests / metrics

    def get(self) -> SchemaSnapshot:
        with self._lock:
            now = self._clock()
            if self._snapshot is None:
                self._rebuild(now)
            elif now - self._checked_at >= self._refresh_s:
                self._checked_at = now
                try:
                    changed = self._fingerprint() != self._snapshot.fingerprint
                except Exception as exc:  # keep serving the last good schema if the DB hiccups
                    log.warning("schema_fingerprint_failed", error=repr(exc)[:200])
                    changed = False
                if changed:
                    self._rebuild(now)
            assert self._snapshot is not None
            return self._snapshot

    def refresh(self) -> SchemaSnapshot:
        """Force a rebuild now (e.g. right after a migration)."""
        with self._lock:
            self._rebuild(self._clock())
            assert self._snapshot is not None
            return self._snapshot

    def _rebuild(self, now: float) -> None:
        previous = self._snapshot.fingerprint if self._snapshot else None
        self._snapshot = self._build()
        self._checked_at = now
        self.rebuilds += 1
        log.info(
            "schema_loaded",
            fingerprint=self._snapshot.fingerprint,
            previous=previous,
            tables=len(self._snapshot.tables),
        )
