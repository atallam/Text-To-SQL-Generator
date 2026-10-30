"""Question -> ready-to-send prompt (or a clarification request).

This is Phase 1's public entry point. It makes NO LLM call: Phase 2's generator takes the returned
``PromptBundle`` and sends it. Keeping them apart means everything here is deterministic and testable
offline.

    builder = ContextBuilder.from_source("wealthlens")
    prepared = builder.prepare("How much did I spend on groceries last month?")
    if prepared.status == "needs_clarification":
        ...show prepared.clarification, re-submit with clarifications={"net_worth": 0}
    else:
        ...send prepared.prompt.system / prepared.prompt.user to the model
"""

from __future__ import annotations

import re
import time
from typing import Literal

from pydantic import BaseModel, Field

from text2sql.config.settings import Settings, get_settings
from text2sql.config.sources import SourceConfig, load_source
from text2sql.connectors.base import Connector
from text2sql.connectors.registry import get_connector
from text2sql.generation.ambiguity import ClarificationRequest, detect_ambiguity
from text2sql.generation.examples import ExampleSelector, FewShotExample, load_examples
from text2sql.observability.logging import get_logger
from text2sql.prompts import LATEST_VERSIONS, SQL_GENERATION, render
from text2sql.schema.embeddings import Embedder, make_embedder
from text2sql.schema.glossary import Glossary
from text2sql.schema.introspect import current_fingerprint, introspect_connector
from text2sql.schema.provider import SchemaProvider
from text2sql.schema.relevance import RelevanceFilter
from text2sql.schema.render import render_relationships, render_schema

log = get_logger("context")

MAX_QUESTION_CHARS = 2000
_TAG_RE = re.compile(r"</?\s*question\s*>", re.IGNORECASE)
_DIALECT_NAMES = {"postgresql": "PostgreSQL", "sqlite": "SQLite", "mysql": "MySQL", "duckdb": "DuckDB"}


class PromptBundle(BaseModel):
    name: str
    version: str
    system: str
    user: str
    tables: list[str]
    table_reasons: dict[str, str]
    examples: list[str]
    glossary_terms: list[str]
    schema_fingerprint: str


class PreparedQuestion(BaseModel):
    status: Literal["ready", "needs_clarification"]
    prompt: PromptBundle | None = None
    clarification: ClarificationRequest | None = None
    timings_ms: dict[str, int] = Field(default_factory=dict)


class ContextBuilder:
    def __init__(
        self,
        *,
        source: SourceConfig,
        schema: SchemaProvider,
        glossary: Glossary,
        examples: list[FewShotExample],
        embedder: Embedder,
        prompt_version: str | None = None,
        max_examples: int = 4,
    ):
        self.source = source
        self.schema = schema
        self.glossary = glossary
        self.relevance = RelevanceFilter(embedder, source.relevance, glossary)
        self.example_selector = ExampleSelector(examples, embedder)
        self.prompt_version = prompt_version or LATEST_VERSIONS[SQL_GENERATION]
        self.max_examples = max_examples

    # -- construction ---------------------------------------------------------------------
    @classmethod
    def from_source(
        cls,
        source: str | SourceConfig,
        *,
        connector: Connector | None = None,
        embedder: Embedder | None = None,
        settings: Settings | None = None,
        **kwargs,
    ) -> ContextBuilder:
        settings = settings or get_settings()
        cfg = load_source(source, settings) if isinstance(source, str) else source
        connector = connector or get_connector(cfg)
        base = cfg.base_dir
        glossary = (
            Glossary.load(base / cfg.glossary, cfg.today_sql) if base and cfg.glossary else Glossary.empty()
        )
        examples = load_examples(base / cfg.examples, cfg.today_sql) if base and cfg.examples else []
        provider = SchemaProvider(
            build=lambda: introspect_connector(connector),
            fingerprint=lambda: current_fingerprint(cfg, connector.engine()),
            refresh_s=settings.schema_refresh_s,
        )
        return cls(
            source=cfg,
            schema=provider,
            glossary=glossary,
            examples=examples,
            embedder=embedder or make_embedder(settings),
            **kwargs,
        )

    # -- main entry point -----------------------------------------------------------------
    def prepare(self, question: str, clarifications: dict[str, int | str] | None = None) -> PreparedQuestion:
        question = " ".join(question.split())
        if not question:
            raise ValueError("question is empty")
        if len(question) > MAX_QUESTION_CHARS:
            raise ValueError(f"question is too long ({len(question)} > {MAX_QUESTION_CHARS} characters)")

        t0 = time.perf_counter()
        request, resolved = detect_ambiguity(question, self.glossary, clarifications)
        if request is not None:
            log.info("clarification_needed", terms=[t.term for t in request.terms])
            return PreparedQuestion(
                status="needs_clarification",
                clarification=request,
                timings_ms={"total": int((time.perf_counter() - t0) * 1000)},
            )

        snapshot = self.schema.get()
        t1 = time.perf_counter()
        selection = self.relevance.select(question, snapshot)
        focused = snapshot.subset(selection.names)
        t2 = time.perf_counter()
        shots = self.example_selector.select(question, set(focused.table_names), self.max_examples)

        matched = self.glossary.matches(question)
        definitions = [
            {"name": t.name.replace("_", " "), "definition": t.definition, "notes": t.notes}
            for t in matched
            if t.definition and t.name not in resolved
        ]
        context = {
            "dialect": _DIALECT_NAMES.get(snapshot.dialect, snapshot.dialect),
            "source_name": self.source.display_name,
            "conventions": self.glossary.conventions,
            "schema_text": render_schema(focused),
            "relationships": render_relationships(focused),
            "definitions": definitions,
            "resolved": [
                {"term": name.replace("_", " "), "label": interp.label} for name, interp in resolved.items()
            ],
            "examples": [{"question": e.question, "sql": e.sql} for e in shots],
            # v2 wraps the question in <question> tags; the question must not be able to close them.
            "question": _TAG_RE.sub("[tag removed]", question),
        }
        context["today"] = self.source.today_sql
        system_ctx = {k: context[k] for k in ("dialect", "source_name", "conventions", "today")}
        bundle = PromptBundle(
            name=SQL_GENERATION,
            version=self.prompt_version,
            system=render(SQL_GENERATION, self.prompt_version, "system", **system_ctx),
            user=render(SQL_GENERATION, self.prompt_version, "user", **context),
            tables=focused.table_names,
            table_reasons={s.name: s.reason for s in selection.selections},
            examples=[e.question for e in shots],
            glossary_terms=[t.name for t in matched],
            schema_fingerprint=snapshot.fingerprint,
        )
        timings = {
            "schema": int((t1 - t0) * 1000),
            "relevance": int((t2 - t1) * 1000),
            "total": int((time.perf_counter() - t0) * 1000),
        }
        log.info(
            "context_built",
            tables=bundle.tables,
            examples=len(shots),
            total_tables=len(snapshot.tables),
            prompt_version=bundle.version,
            **{f"ms_{k}": v for k, v in timings.items()},
        )
        return PreparedQuestion(status="ready", prompt=bundle, timings_ms=timings)
