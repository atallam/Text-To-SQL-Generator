"""SchemaProvider: schema context regenerates after a database change, without hammering the DB."""

from tests.helpers import make_snapshot
from text2sql.schema.provider import SchemaProvider


class Harness:
    def __init__(self):
        self.now = 0.0
        self.snapshot = make_snapshot({"orders": {"cols": ["id"]}})
        self.fingerprint_calls = 0
        self.fingerprint_error: Exception | None = None
        self.provider = SchemaProvider(
            build=lambda: self.snapshot,
            fingerprint=self._fingerprint,
            refresh_s=60,
            clock=lambda: self.now,
        )

    def _fingerprint(self):
        self.fingerprint_calls += 1
        if self.fingerprint_error:
            raise self.fingerprint_error
        return self.snapshot.fingerprint

    def alter(self):
        self.snapshot = make_snapshot({"orders": {"cols": ["id", "status"]}})


def test_first_get_builds_and_later_gets_use_the_cache():
    h = Harness()
    first = h.provider.get()
    h.now = 30
    assert h.provider.get() is first
    assert (h.provider.rebuilds, h.fingerprint_calls) == (1, 0)  # inside the refresh window: no DB work


def test_unchanged_schema_is_checked_cheaply_but_not_rebuilt():
    h = Harness()
    h.provider.get()
    h.now = 61
    h.provider.get()
    assert (h.provider.rebuilds, h.fingerprint_calls) == (1, 1)


def test_ddl_change_is_picked_up_after_the_refresh_window():
    h = Harness()
    old = h.provider.get()
    h.alter()
    h.now = 10
    assert h.provider.get().fingerprint == old.fingerprint  # still inside the window
    h.now = 61
    new = h.provider.get()
    assert new.fingerprint != old.fingerprint
    assert "status" in new.table("orders").columns[-1].name
    assert h.provider.rebuilds == 2


def test_database_hiccup_keeps_serving_the_last_good_schema():
    h = Harness()
    old = h.provider.get()
    h.fingerprint_error = RuntimeError("connection lost")
    h.now = 61
    assert h.provider.get() is old


def test_manual_refresh_rebuilds_immediately():
    h = Harness()
    h.provider.get()
    h.alter()
    assert "status" in [c.name for c in h.provider.refresh().table("orders").columns]
