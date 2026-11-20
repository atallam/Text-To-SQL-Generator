"""Pipeline registry (one Text2SQL per source, built lazily) and the human-approved feedback writers."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from api.schemas import SourceInfo
from text2sql.config.settings import PROJECT_ROOT, Settings, get_settings
from text2sql.config.sources import SourceConfig, list_sources, load_source
from text2sql.connectors.base import Connector
from text2sql.connectors.registry import get_connector
from text2sql.generation.examples import load_examples
from text2sql.observability.logging import get_logger
from text2sql.pipeline import Text2SQL

log = get_logger("api.service")

APPROVED_EXAMPLES = "examples.approved.yaml"  # loaded by ContextBuilder next to examples.yaml
FEEDBACK_DIR = PROJECT_ROOT / "evals" / "feedback"


class UnknownSource(KeyError):
    pass


class PipelineRegistry:
    def __init__(
        self,
        settings: Settings | None = None,
        factory: Callable[[str], Text2SQL] | None = None,
        source_loader: Callable[[str], SourceConfig] | None = None,
        source_lister: Callable[[], list[str]] | None = None,
    ):
        self.settings = settings or get_settings()
        self._factory = factory
        self._load = source_loader or (lambda n: load_source(n, self.settings))
        self._list = source_lister or (lambda: list_sources(self.settings))
        self._pipelines: dict[str, Text2SQL] = {}
        self._connectors: dict[str, Connector] = {}
        self._lock = threading.Lock()

    # -- sources ---------------------------------------------------------------------------
    def names(self) -> list[str]:
        return self._list()

    @property
    def default(self) -> str:
        names = self.names()
        d = self.settings.default_source
        return d if d in names else (names[0] if names else d)

    def resolve(self, name: str | None) -> str:
        name = name or self.default
        if name not in self.names():  # also blocks path tricks: only known folder names pass
            raise UnknownSource(name)
        return name

    def info(self) -> list[SourceInfo]:
        out = []
        for n in self.names():
            try:
                cfg = self._load(n)
            except Exception as exc:  # noqa: BLE001  one broken source must not hide the others
                log.warning("source_load_failed", source=n, error=repr(exc)[:200])
                continue
            out.append(
                SourceInfo(
                    name=n,
                    display_name=cfg.display_name,
                    dialect=cfg.dialect,
                    default=n == self.default,
                    examples=self._example_questions(cfg),
                )
            )
        return out

    @staticmethod
    def _example_questions(cfg: SourceConfig, limit: int = 6) -> list[str]:
        if not (cfg.base_dir and cfg.examples):
            return []
        try:
            return [e.question for e in load_examples(cfg.base_dir / cfg.examples, cfg.today_sql)][:limit]
        except Exception:  # noqa: BLE001
            return []

    # -- pipelines -------------------------------------------------------------------------
    def get(self, name: str) -> Text2SQL:
        with self._lock:
            if name not in self._pipelines:
                if self._factory:
                    self._pipelines[name] = self._factory(name)
                else:
                    cfg = self._load(name)
                    conn = self._connectors.setdefault(name, get_connector(cfg))
                    self._pipelines[name] = Text2SQL.from_source(cfg, connector=conn, settings=self.settings)
                log.info("pipeline_ready", source=name)
            return self._pipelines[name]

    def reload(self, name: str) -> None:
        """Drop the cached pipeline so the next request rebuilds it (e.g. after approving few-shots)."""
        with self._lock:
            self._pipelines.pop(name, None)

    def db_ok(self, name: str) -> bool | None:
        """True/False for a real connector, None when there is nothing to probe (custom factory)."""
        if self._factory:
            return None
        try:
            with self._lock:
                conn = self._connectors.setdefault(name, get_connector(self._load(name)))
            return bool(conn.health_check())
        except Exception as exc:  # noqa: BLE001
            log.warning("db_health_failed", source=name, error=repr(exc)[:200])
            return False

    def sources_dir(self) -> Path:
        return Path(self.settings.sources_dir)


# ---- applying an APPROVED piece of feedback ---------------------------------------------------------
def apply_few_shot(registry: PipelineRegistry, source: str, question: str, sql: str) -> list[str]:
    """Append an approved question -> SQL pair after re-checking it through the guardrails."""
    t2s = registry.get(source)
    decision = t2s.engine.check(sql, t2s.builder.schema.get())
    if not decision.allowed:
        raise ValueError(f"The SQL no longer passes the guardrails: {decision.reason}")
    tables = sorted(decision.tables)
    if not tables:
        raise ValueError("Could not determine which tables the SQL uses.")
    path = registry.sources_dir() / source / APPROVED_EXAMPLES
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = data.setdefault("examples", [])
    if not any(i.get("question") == question for i in items):
        items.append({"question": question, "tables": tables, "sql": decision.sql})
        header = "# Human-approved examples from user feedback. Reviewed before being added.\n"
        path.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    registry.reload(source)
    return tables


def append_eval_case(source: str, row: dict[str, Any], directory: Path | None = None) -> Path:
    """Approved thumbs-down -> a candidate regression case for the eval suite (JSONL, one per line)."""
    directory = directory or FEEDBACK_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{source}.jsonl"
    case = {
        "question": row.get("question"),
        "bad_sql": row.get("sql"),
        "expected_sql": row.get("corrected_sql"),  # may be empty: a human must still supply the truth
        "comment": row.get("comment"),
        "source": source,
        "feedback_id": row.get("id"),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(case, ensure_ascii=False) + "\n")
    return path
