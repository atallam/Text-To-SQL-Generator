"""SQLite store for query history and user feedback.

Feedback flywheel with a human gate: a thumbs-down becomes a *candidate eval case*, a thumbs-up a
*candidate few-shot example*. Nothing reaches evals or prompts until a reviewer approves it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    audit_id    TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,            -- 'ask' | 'run_sql'
    question    TEXT,
    sql         TEXT,
    status      TEXT NOT NULL,
    confidence  REAL,
    level       TEXT,
    created_at  REAL NOT NULL,
    response    TEXT NOT NULL             -- full JSON response, rows capped
);
CREATE INDEX IF NOT EXISTS idx_history_session ON history(session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id      TEXT NOT NULL,
    source        TEXT NOT NULL,
    verdict       TEXT NOT NULL CHECK (verdict IN ('correct','incorrect')),
    candidate     TEXT NOT NULL CHECK (candidate IN ('few_shot','eval_case')),
    question      TEXT,
    sql           TEXT,                   -- the SQL that ran
    corrected_sql TEXT,                   -- what the user says it should have been
    comment       TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','approved','rejected')),
    reviewed_by   TEXT,
    created_at    REAL NOT NULL,
    reviewed_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(review_status, created_at DESC);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._mem = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = self._mem or sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                if self._mem is None:
                    conn.close()

    # -- history ------------------------------------------------------------------------------
    def add_history(
        self, *, session_id: str, source: str, kind: str, response: dict[str, Any], keep_rows: int
    ) -> None:
        slim = dict(response)
        if slim.get("rows"):
            slim["rows"] = slim["rows"][:keep_rows]
        conf = (slim.get("confidence") or {}) if isinstance(slim.get("confidence"), dict) else {}
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    slim["audit_id"],
                    session_id,
                    source,
                    kind,
                    slim.get("question"),
                    slim.get("sql"),
                    slim["status"],
                    conf.get("score"),
                    conf.get("level"),
                    time.time(),
                    json.dumps(slim, default=str),
                ),
            )

    def list_history(
        self, session_id: str, limit: int = 50, source: str | None = None
    ) -> list[dict[str, Any]]:
        q = (
            "SELECT audit_id, source, kind, question, sql, status, confidence, level, created_at "
            "FROM history WHERE session_id = ?"
        )
        args: list[Any] = [session_id]
        if source:
            q += " AND source = ?"
            args.append(source)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args)]

    def get_history(self, audit_id: str, session_id: str | None = None) -> dict[str, Any] | None:
        q, args = "SELECT source, session_id, response FROM history WHERE audit_id = ?", [audit_id]
        with self._conn() as c:
            row = c.execute(q, args).fetchone()
        if row is None or (session_id is not None and row["session_id"] != session_id):
            return None
        return {"source": row["source"], "response": json.loads(row["response"])}

    # -- feedback -----------------------------------------------------------------------------
    def add_feedback(
        self,
        *,
        audit_id: str,
        source: str,
        verdict: str,
        question: str | None,
        sql: str | None,
        corrected_sql: str | None,
        comment: str | None,
    ) -> int:
        candidate = "few_shot" if verdict == "correct" else "eval_case"
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO feedback (audit_id, source, verdict, candidate, question, sql, corrected_sql,"
                " comment, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (audit_id, source, verdict, candidate, question, sql, corrected_sql, comment, time.time()),
            )
            return int(cur.lastrowid)

    def list_feedback(self, status: str | None = "pending", limit: int = 100) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM feedback", []
        if status:
            q += " WHERE review_status = ?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args)]

    def get_feedback(self, fid: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM feedback WHERE id = ?", (fid,)).fetchone()
        return dict(row) if row else None

    def review_feedback(self, fid: int, status: str, reviewer: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE feedback SET review_status = ?, reviewed_by = ?, reviewed_at = ? "
                "WHERE id = ? AND review_status = 'pending'",
                (status, reviewer, time.time(), fid),
            )
            return cur.rowcount == 1
