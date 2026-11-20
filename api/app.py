"""FastAPI application factory. The ASGI entry point is ``api.main:app``."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.schemas import (
    ColumnOut,
    FeedbackRequest,
    FeedbackResponse,
    ForeignKeyOut,
    HistoryItem,
    QueryRequest,
    QueryResponse,
    ReviewRequest,
    RunSqlRequest,
    SchemaResponse,
    SourceInfo,
    TableOut,
    to_response,
)
from api.security import RateLimiter, require_admin, require_key
from api.service import PipelineRegistry, UnknownSource, append_eval_case, apply_few_shot
from api.settings import ApiSettings, get_api_settings
from api.store import Store
from text2sql.config.settings import get_settings
from text2sql.observability.logging import get_logger
from text2sql.pipeline import QueryOutcome, Text2SQL

log = get_logger("api")
VERSION = "0.4.0"

ERR = {
    401: {"description": "Missing or invalid API key."},
    429: {"description": "Rate limit exceeded; see Retry-After."},
    503: {"description": "Service not configured or a dependency is unavailable."},
}


class _BodyLimit:
    """Reject oversized request bodies (Content-Length and streamed) before they are parsed."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for k, v in scope.get("headers", []):
            if k == b"content-length" and v.isdigit() and int(v) > self.max_bytes:
                return await self._reject(scope, receive, send)
        seen = 0

        async def limited():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge
            return msg

        try:
            await self.app(scope, limited, send)
        except _TooLarge:
            await self._reject(scope, receive, send)

    async def _reject(self, scope, receive, send):
        resp = JSONResponse({"detail": f"Request body larger than {self.max_bytes} bytes."}, status_code=413)
        await resp(scope, receive, send)


class _TooLarge(Exception):
    pass


def create_app(
    *,
    registry: PipelineRegistry | None = None,
    store: Store | None = None,
    api_settings: ApiSettings | None = None,
) -> FastAPI:
    cfg = api_settings or get_api_settings()
    registry = registry or PipelineRegistry()
    store = store or Store(cfg.api_db_path)

    app = FastAPI(
        title="Text-To-SQL API",
        version=VERSION,
        summary="Ask questions in plain English; get guarded, validated SQL answers with a confidence score.",
        description=(
            "Every answer is produced by read-only, guardrail-checked SQL and comes with a confidence "
            "score and the evidence behind it. Authenticate with the `X-API-Key` header."
        ),
        openapi_tags=[
            {"name": "query", "description": "Ask questions or re-run edited SQL."},
            {"name": "schema", "description": "Sources and their schemas."},
            {"name": "history", "description": "Past queries for a session."},
            {"name": "feedback", "description": "Thumbs up/down and the human review queue."},
            {"name": "ops", "description": "Health checks."},
        ],
    )
    app.state.registry, app.state.store, app.state.rate_limiter = registry, store, RateLimiter()
    if api_settings is not None:
        app.dependency_overrides[get_api_settings] = lambda: cfg

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type"],
    )
    app.add_middleware(_BodyLimit, max_bytes=cfg.api_max_body_bytes)

    def source_or_404(name: str | None) -> str:
        try:
            return registry.resolve(name)
        except UnknownSource:
            raise HTTPException(
                404, f"Unknown source {name!r}. Available: {', '.join(registry.names())}"
            ) from None

    def pipeline(source: str) -> Text2SQL:
        try:
            return registry.get(source)
        except (RuntimeError, FileNotFoundError) as exc:
            log.error("pipeline_unavailable", source=source, error=repr(exc)[:300])
            raise HTTPException(503, f"Source {source!r} is not ready: {exc}") from None

    def call(fn: Callable[[], QueryOutcome]) -> QueryOutcome:
        try:
            return fn()
        except ValueError as exc:  # empty / too-long question
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:  # noqa: BLE001  never leak internals; the audit log has the detail
            log.error("pipeline_error", error=repr(exc)[:400])
            raise HTTPException(
                502,
                "The model or database is temporarily unavailable. Nothing was run. Please try again shortly.",
            ) from None

    def finish(
        out: QueryOutcome, *, source: str, session_id: str, kind: Literal["ask", "run_sql"]
    ) -> QueryResponse:
        resp = to_response(out, source=source, session_id=session_id, kind=kind)
        try:
            store.add_history(
                session_id=session_id,
                source=source,
                kind=kind,
                response=resp.model_dump(mode="json"),
                keep_rows=cfg.api_history_rows_kept,
            )
        except Exception as exc:  # noqa: BLE001  history is best effort; the answer still goes out
            log.error("history_write_failed", error=repr(exc)[:200])
        return resp

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_key)], responses=ERR)

    # -- query ------------------------------------------------------------------------------
    @v1.post(
        "/query", response_model=QueryResponse, tags=["query"], summary="Ask a question in plain English"
    )
    def query(req: QueryRequest) -> QueryResponse:
        """Returns SQL, results, confidence (with the signals behind it) and guardrail notes. If the
        question is ambiguous the response has `status: needs_clarification`; resubmit the same question
        with `clarifications` set to the chosen option for each term."""
        source = source_or_404(req.source)
        t2s = pipeline(source)
        out = call(lambda: t2s.ask(req.question, req.clarifications or None))
        return finish(out, source=source, session_id=req.session_id or uuid.uuid4().hex, kind="ask")

    @v1.post(
        "/query/run-sql",
        response_model=QueryResponse,
        tags=["query"],
        summary="Re-run SQL you edited, through the same guardrails",
    )
    def run_sql(req: RunSqlRequest) -> QueryResponse:
        """No model is involved. The SQL goes through the same safety layers (parser allow-list, planner
        cost gate, read-only transaction, SELECT-only role). The result is not confidence-scored."""
        source = source_or_404(req.source)
        t2s = pipeline(source)
        out = call(lambda: t2s.run_sql(req.sql))
        return finish(out, source=source, session_id=req.session_id or uuid.uuid4().hex, kind="run_sql")

    # -- schema / sources ---------------------------------------------------------------------
    @v1.get("/sources", response_model=list[SourceInfo], tags=["schema"], summary="List data sources")
    def sources() -> list[SourceInfo]:
        return registry.info()

    @v1.get("/schema", response_model=SchemaResponse, tags=["schema"], summary="Schema of a source")
    def schema(source: str | None = Query(default=None)) -> SchemaResponse:
        name = source_or_404(source)
        snap = pipeline(name).builder.schema.get()
        return SchemaResponse(
            source=name,
            dialect=snap.dialect,
            fingerprint=snap.fingerprint,
            tables=[
                TableOut(
                    name=t.name,
                    comment=t.comment,
                    columns=[
                        ColumnOut(
                            name=c.name,
                            type=c.type,
                            nullable=c.nullable,
                            primary_key=c.primary_key,
                            comment=c.comment,
                            allowed_values=c.allowed_values,
                        )
                        for c in t.columns
                    ],
                    foreign_keys=[ForeignKeyOut(**fk.model_dump()) for fk in t.foreign_keys],
                )
                for t in snap.tables
            ],
        )

    # -- history ------------------------------------------------------------------------------
    @v1.get(
        "/history", response_model=list[HistoryItem], tags=["history"], summary="Past queries of a session"
    )
    def history(
        session_id: str = Query(pattern=r"^[A-Za-z0-9_-]{8,64}$"),
        source: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        return store.list_history(session_id, limit, source)

    @v1.get("/history/{audit_id}", response_model=QueryResponse, tags=["history"], summary="One past result")
    def history_item(
        audit_id: str, session_id: str = Query(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    ) -> dict[str, Any]:
        found = store.get_history(audit_id, session_id)
        if found is None:
            raise HTTPException(404, "No such query in this session.")
        return found["response"]

    # -- feedback -----------------------------------------------------------------------------
    @v1.post(
        "/feedback",
        response_model=FeedbackResponse,
        tags=["feedback"],
        summary="Mark an answer right or wrong",
    )
    def feedback(req: FeedbackRequest) -> FeedbackResponse:
        """Thumbs up becomes a *candidate few-shot example*; thumbs down a *candidate eval case*. Both wait
        for human review, so feedback can never change answers by itself."""
        found = store.get_history(req.audit_id, req.session_id)
        if found is None:
            raise HTTPException(404, "Unknown query. Feedback must refer to an answer from your session.")
        resp = found["response"]
        fid = store.add_feedback(
            audit_id=req.audit_id,
            source=found["source"],
            verdict=req.verdict,
            question=resp.get("question"),
            sql=resp.get("sql"),
            corrected_sql=req.corrected_sql,
            comment=req.comment,
        )
        return FeedbackResponse(
            id=fid,
            candidate="few_shot" if req.verdict == "correct" else "eval_case",
            message="Thanks. Saved for review; it will not change any answers until a reviewer approves it.",
        )

    @v1.get("/feedback", tags=["feedback"], summary="Review queue (admin)")
    def feedback_queue(
        status: Literal["pending", "approved", "rejected"] = "pending",
        limit: int = Query(default=100, ge=1, le=500),
        _admin: str = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return store.list_feedback(status, limit)

    @v1.post(
        "/feedback/{feedback_id}/review", tags=["feedback"], summary="Approve or reject feedback (admin)"
    )
    def feedback_review(
        feedback_id: int, req: ReviewRequest, admin: str = Depends(require_admin)
    ) -> dict[str, Any]:
        row = store.get_feedback(feedback_id)
        if row is None or row["review_status"] != "pending":
            raise HTTPException(404, "No pending feedback with that id.")
        detail = ""
        if req.decision == "approve":
            try:
                if row["candidate"] == "few_shot":
                    if not (row["question"] and row["sql"]):
                        raise ValueError("Nothing to add: the original question or SQL is missing.")
                    tables = apply_few_shot(registry, row["source"], row["question"], row["sql"])
                    detail = f"Added as a few-shot example (tables: {', '.join(tables)})."
                else:
                    path = append_eval_case(row["source"], row)
                    detail = f"Added to {path.name} as a candidate eval case."
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        store.review_feedback(feedback_id, "approved" if req.decision == "approve" else "rejected", admin)
        return {"id": feedback_id, "review_status": req.decision + "d", "detail": detail}

    # -- ops ----------------------------------------------------------------------------------
    @app.get("/healthz", tags=["ops"], summary="Liveness and dependency check (no auth)")
    def healthz() -> JSONResponse:
        name = registry.default
        db = registry.db_ok(name)
        llm = bool(get_settings().anthropic_api_key) if not registry._factory else True  # noqa: SLF001
        ok = db is not False
        body = {
            "status": "ok" if ok else "degraded",
            "version": VERSION,
            "default_source": name,
            "database": {True: "ok", False: "unreachable", None: "not_checked"}[db],
            "llm_configured": llm,
            "auth": "disabled" if cfg.api_auth_disabled else ("configured" if cfg.keys else "no_keys"),
        }
        return JSONResponse(body, status_code=200 if ok else 503)

    app.include_router(v1)
    return app
