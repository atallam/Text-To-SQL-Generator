"""API tests with a fake pipeline: no database, no LLM, no network."""

from __future__ import annotations

import datetime as dt
import decimal
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import service
from api.app import create_app
from api.schemas import _safe, to_response
from api.security import RateLimiter
from api.service import PipelineRegistry
from api.settings import ApiSettings
from api.store import Store
from tests.helpers import make_snapshot, source_config
from text2sql.config.settings import Settings
from text2sql.execution.executor import ExecutionResult
from text2sql.generation.ambiguity import AmbiguousTerm, ClarificationOption, ClarificationRequest
from text2sql.generation.generator import GeneratedSQL
from text2sql.pipeline import GuardrailSummary, QueryOutcome
from text2sql.validation import ValidationReport
from text2sql.validation.confidence import ConfidenceReport, Signal

KEY, ADMIN = "test-key-1", "admin-key-1"
H = {"X-API-Key": KEY}
HA = {"X-API-Key": ADMIN}
SID = "session-aaaa-1111"


# ---- fixtures / fakes ---------------------------------------------------------------------------
def success_outcome(question="How many accounts?", audit="aud000000001") -> QueryOutcome:
    sql = "SELECT COUNT(*) AS n FROM accounts LIMIT 1000"
    return QueryOutcome(
        audit_id=audit,
        status="success",
        question=question,
        sql=sql,
        generated=GeneratedSQL(sql=sql, explanation="Counts the accounts.", tables_used=["accounts"]),
        execution=ExecutionResult(
            columns=["n", "as_of"],
            rows=[[decimal.Decimal("3"), dt.date(2026, 9, 30)]],
            row_count=1,
            truncated=False,
            elapsed_ms=4,
            result_hash="abc123",
            sql=sql,
        ),
        guardrail=GuardrailSummary(allowed=True, tables=["accounts"], row_limit_applied=1000),
        validation=ValidationReport(
            confidence=ConfidenceReport(
                score=0.91,
                level="high",
                signals=[
                    Signal(name="syntax", value=1.0, weight=0.2, detail="first attempt"),
                    Signal(name="multi_query", value=None, weight=0.3, detail="skipped"),
                ],
            )
        ),
    )


def blocked_outcome() -> QueryOutcome:
    return QueryOutcome(
        audit_id="aud000000002",
        status="blocked",
        sql="DROP TABLE accounts",
        message="Only SELECT statements are allowed.",
        guardrail=GuardrailSummary(
            allowed=False, rule_ids=["G002"], reasons=["G002 statement_allowlist: DROP"]
        ),
    )


def clarification_outcome() -> QueryOutcome:
    return QueryOutcome(
        audit_id="aud000000003",
        status="needs_clarification",
        question="What is my revenue?",
        clarification=ClarificationRequest(
            question="What is my revenue?",
            terms=[
                AmbiguousTerm(
                    term="revenue",
                    matched_text="revenue",
                    options=[
                        ClarificationOption(index=0, label="Gross", example_sql="SELECT 1"),
                        ClarificationOption(index=1, label="Net", example_sql="SELECT 2"),
                    ],
                )
            ],
        ),
    )


class FakeT2S:
    def __init__(self):
        self.asked: list[tuple] = []
        self.ran: list[str] = []
        self.fail: Exception | None = None
        self.next: QueryOutcome | None = None
        snap = make_snapshot({"accounts": {"cols": ["id", "name"]}})
        self.builder = SimpleNamespace(schema=SimpleNamespace(get=lambda: snap))
        self.engine = SimpleNamespace(
            check=lambda sql, schema: SimpleNamespace(allowed=True, sql=sql, tables=["accounts"], reason="")
        )

    def ask(self, question, clarifications=None):
        self.asked.append((question, clarifications))
        if self.fail:
            raise self.fail
        if self.next is not None:
            return self.next
        if question.lower().startswith("what is my revenue") and not clarifications:
            return clarification_outcome()
        return success_outcome(question)

    def run_sql(self, sql):
        self.ran.append(sql)
        out = success_outcome(question=None, audit="aud000000009")
        out.validation = None
        return out


@pytest.fixture
def fake():
    return FakeT2S()


@pytest.fixture
def make_client(tmp_path, fake):
    def build(**api_kwargs):
        cfg = ApiSettings(
            _env_file=None,
            api_keys=api_kwargs.pop("api_keys", KEY),
            api_admin_keys=api_kwargs.pop("api_admin_keys", ADMIN),
            api_db_path=tmp_path / "app.db",
            api_rate_limit_per_minute=api_kwargs.pop("api_rate_limit_per_minute", 1000),
            **api_kwargs,
        )
        registry = PipelineRegistry(
            settings=Settings(_env_file=None, sources_dir=tmp_path / "sources"),
            factory=lambda name: fake,
            source_loader=lambda name: source_config(name=name, display_name=name.title()),
            source_lister=lambda: ["demo", "other"],
        )
        return TestClient(create_app(registry=registry, store=Store(cfg.api_db_path), api_settings=cfg))

    return build


@pytest.fixture
def client(make_client):
    return make_client()


# ---- auth, limits, health -----------------------------------------------------------------------
def test_healthz_needs_no_key_and_reports_state(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["default_source"] == "demo" and body["auth"] == "configured"


def test_missing_or_wrong_key_is_401(client):
    assert client.get("/v1/sources").status_code == 401
    assert client.get("/v1/sources", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/v1/sources", headers=H).status_code == 200


def test_no_keys_configured_fails_closed(make_client):
    c = make_client(api_keys="")
    r = c.get("/v1/sources", headers=H)
    assert r.status_code == 503 and "API_KEYS" in r.json()["detail"]
    assert c.get("/healthz").json()["auth"] == "no_keys"


def test_auth_can_be_disabled_explicitly(make_client):
    c = make_client(api_keys="", api_auth_disabled=True)
    assert c.get("/v1/sources").status_code == 200


def test_rate_limit_returns_429_with_retry_after(make_client):
    c = make_client(api_rate_limit_per_minute=2)
    assert c.get("/v1/sources", headers=H).status_code == 200
    assert c.get("/v1/sources", headers=H).status_code == 200
    r = c.get("/v1/sources", headers=H)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_rate_limiter_window_slides():
    now = [0.0]
    rl = RateLimiter(clock=lambda: now[0])
    rl.check("a", 1)
    with pytest.raises(Exception):
        rl.check("a", 1)
    rl.check("b", 1)  # separate callers do not share a budget
    now[0] = 61.0
    rl.check("a", 1)


def test_oversized_body_is_413(make_client):
    c = make_client(api_max_body_bytes=1000)
    r = c.post("/v1/query", headers=H, json={"question": "x" * 1900})
    assert r.status_code == 413


# ---- query --------------------------------------------------------------------------------------
def test_query_success_has_confidence_rows_and_json_safe_cells(client, fake):
    r = client.post("/v1/query", headers=H, json={"question": "How many accounts?", "session_id": SID})
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "success" and b["source"] == "demo" and b["session_id"] == SID
    assert b["rows"] == [[3.0, "2026-09-30"]]
    c = b["confidence"]
    assert c["level"] == "high" and c["score"] == 0.91
    assert c["signals_used"] == 1 and c["signals_total"] == 2
    assert [s["measured"] for s in c["signals"]] == [True, False]
    assert b["explanation"] == "Counts the accounts."


def test_every_answer_gets_a_session_id_when_none_given(client):
    b = client.post("/v1/query", headers=H, json={"question": "How many accounts?"}).json()
    assert len(b["session_id"]) >= 8


def test_clarification_round_trip(client, fake):
    first = client.post("/v1/query", headers=H, json={"question": "What is my revenue?"}).json()
    assert first["status"] == "needs_clarification"
    opts = first["clarification"]["terms"][0]["options"]
    assert [o["label"] for o in opts] == ["Gross", "Net"]
    second = client.post(
        "/v1/query", headers=H, json={"question": "What is my revenue?", "clarifications": {"revenue": 1}}
    ).json()
    assert second["status"] == "success"
    assert fake.asked[-1][1] == {"revenue": 1}


def test_blocked_query_is_explained_in_plain_language(client, fake):
    fake.next = blocked_outcome()
    b = client.post("/v1/query", headers=H, json={"question": "drop it"}).json()
    assert b["status"] == "blocked" and b["rows"] == []
    expl = b["guardrail"]["explanations"][0]
    assert expl["rule_id"] == "G002" and "SELECT" in expl["plain"]
    assert b["confidence"] is None and b["confidence_note"]


def test_run_sql_goes_through_pipeline_and_is_not_scored(client, fake):
    r = client.post("/v1/query/run-sql", headers=H, json={"sql": "SELECT 1", "session_id": SID})
    assert r.status_code == 200 and fake.ran == ["SELECT 1"]
    b = r.json()
    assert b["kind"] == "run_sql" and b["confidence"] is None and "not scored" in b["confidence_note"]


def test_unknown_source_is_404_and_lists_choices(client):
    r = client.post("/v1/query", headers=H, json={"question": "hi", "source": "../etc"})
    assert r.status_code == 404 and "demo" in r.json()["detail"]


def test_bad_input_is_422(client):
    assert client.post("/v1/query", headers=H, json={"question": ""}).status_code == 422
    assert (
        client.post("/v1/query", headers=H, json={"question": "ok", "session_id": "bad id!"}).status_code
        == 422
    )


def test_pipeline_value_error_is_422(client, fake):
    fake.fail = ValueError("question is empty")
    assert client.post("/v1/query", headers=H, json={"question": "hi"}).status_code == 422


def test_internal_failure_is_502_and_leaks_nothing(client, fake):
    fake.fail = RuntimeError("postgresql://user:SECRET@host/db exploded")
    r = client.post("/v1/query", headers=H, json={"question": "hi"})
    assert r.status_code == 502
    assert "SECRET" not in r.text and "Nothing was run" in r.json()["detail"]


# ---- sources / schema / history -----------------------------------------------------------------
def test_sources_and_schema(client):
    srcs = client.get("/v1/sources", headers=H).json()
    assert [s["name"] for s in srcs] == ["demo", "other"] and srcs[0]["default"] is True
    sch = client.get("/v1/schema", headers=H).json()
    assert sch["tables"][0]["name"] == "accounts"
    assert {c["name"] for c in sch["tables"][0]["columns"]} == {"id", "name"}
    assert client.get("/v1/schema?source=nope", headers=H).status_code == 404


def test_history_is_per_session(client):
    client.post("/v1/query", headers=H, json={"question": "How many accounts?", "session_id": SID})
    client.post("/v1/query/run-sql", headers=H, json={"sql": "SELECT 1", "session_id": SID})
    client.post("/v1/query", headers=H, json={"question": "other session", "session_id": "session-bbbb-2222"})
    items = client.get(f"/v1/history?session_id={SID}", headers=H).json()
    assert len(items) == 2 and {i["kind"] for i in items} == {"ask", "run_sql"}
    one = client.get(f"/v1/history/{items[0]['audit_id']}?session_id={SID}", headers=H)
    assert one.status_code == 200 and one.json()["audit_id"] == items[0]["audit_id"]
    assert (
        client.get(f"/v1/history/{items[0]['audit_id']}?session_id=session-bbbb-2222", headers=H).status_code
        == 404
    )


# ---- feedback and the human gate ----------------------------------------------------------------
def _ask(client, session=SID):
    return client.post(
        "/v1/query", headers=H, json={"question": "How many accounts?", "session_id": session}
    ).json()


def test_feedback_creates_pending_candidates(client):
    a = _ask(client)
    up = client.post(
        "/v1/feedback", headers=H, json={"audit_id": a["audit_id"], "verdict": "correct", "session_id": SID}
    )
    down = client.post(
        "/v1/feedback",
        headers=H,
        json={"audit_id": a["audit_id"], "verdict": "incorrect", "comment": "wrong", "session_id": SID},
    )
    assert up.json()["candidate"] == "few_shot" and down.json()["candidate"] == "eval_case"
    assert up.json()["review_status"] == "pending"


def test_feedback_for_unknown_or_foreign_answer_is_404(client):
    a = _ask(client)
    assert (
        client.post(
            "/v1/feedback", headers=H, json={"audit_id": "nope12345", "verdict": "correct"}
        ).status_code
        == 404
    )
    r = client.post(
        "/v1/feedback",
        headers=H,
        json={"audit_id": a["audit_id"], "verdict": "correct", "session_id": "session-bbbb-2222"},
    )
    assert r.status_code == 404


def test_review_needs_admin_key(client, make_client):
    a = _ask(client)
    fid = client.post(
        "/v1/feedback", headers=H, json={"audit_id": a["audit_id"], "verdict": "incorrect", "session_id": SID}
    ).json()["id"]
    assert client.get("/v1/feedback", headers=H).status_code == 403
    assert (
        client.post(f"/v1/feedback/{fid}/review", headers=H, json={"decision": "approve"}).status_code == 403
    )
    no_admin = make_client(api_admin_keys="")
    assert no_admin.get("/v1/feedback", headers=H).status_code == 403
    queue = client.get("/v1/feedback", headers=HA).json()
    assert [q["id"] for q in queue] == [fid] and queue[0]["review_status"] == "pending"


def test_approving_thumbs_down_writes_eval_case_only_after_review(client, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "FEEDBACK_DIR", tmp_path / "fb")
    a = _ask(client)
    fid = client.post(
        "/v1/feedback",
        headers=H,
        json={
            "audit_id": a["audit_id"],
            "verdict": "incorrect",
            "corrected_sql": "SELECT 2",
            "session_id": SID,
        },
    ).json()["id"]
    assert not (tmp_path / "fb").exists()  # nothing happens on submit
    r = client.post(f"/v1/feedback/{fid}/review", headers=HA, json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["review_status"] == "approved"
    line = json.loads((tmp_path / "fb" / "demo.jsonl").read_text().splitlines()[0])
    assert line["question"] == "How many accounts?" and line["expected_sql"] == "SELECT 2"
    # a decision is final
    assert (
        client.post(f"/v1/feedback/{fid}/review", headers=HA, json={"decision": "approve"}).status_code == 404
    )


def test_approving_thumbs_up_adds_guardrail_checked_few_shot(client, tmp_path):
    a = _ask(client)
    fid = client.post(
        "/v1/feedback", headers=H, json={"audit_id": a["audit_id"], "verdict": "correct", "session_id": SID}
    ).json()["id"]
    r = client.post(f"/v1/feedback/{fid}/review", headers=HA, json={"decision": "approve"})
    assert r.status_code == 200 and "accounts" in r.json()["detail"]
    from text2sql.generation.examples import load_examples

    ex = load_examples(tmp_path / "sources" / "demo" / service.APPROVED_EXAMPLES)
    assert ex[0].question == "How many accounts?" and ex[0].tables == ["accounts"]


def test_rejecting_changes_nothing_on_disk(client, tmp_path):
    a = _ask(client)
    fid = client.post(
        "/v1/feedback", headers=H, json={"audit_id": a["audit_id"], "verdict": "correct", "session_id": SID}
    ).json()["id"]
    r = client.post(f"/v1/feedback/{fid}/review", headers=HA, json={"decision": "reject"})
    assert r.json()["review_status"] == "rejected"
    assert not (tmp_path / "sources").exists()


def test_few_shot_that_fails_guardrails_is_not_added(client, fake, tmp_path):
    a = _ask(client)
    fid = client.post(
        "/v1/feedback", headers=H, json={"audit_id": a["audit_id"], "verdict": "correct", "session_id": SID}
    ).json()["id"]
    fake.engine = SimpleNamespace(
        check=lambda sql, schema: SimpleNamespace(
            allowed=False, sql=sql, tables=[], reason="G002 not a SELECT"
        )
    )
    r = client.post(f"/v1/feedback/{fid}/review", headers=HA, json={"decision": "approve"})
    assert r.status_code == 422 and "guardrails" in r.json()["detail"]
    assert not (tmp_path / "sources").exists()


# ---- conversion helpers -------------------------------------------------------------------------
def test_safe_cell_conversion():
    assert _safe(decimal.Decimal("1.5")) == 1.5
    assert _safe(dt.datetime(2026, 1, 2, 3, 4)) == "2026-01-02T03:04:00"
    assert _safe(float("nan")) is None and _safe(float("inf")) is None
    assert _safe(b"x") == "b'x'" and _safe(None) is None and _safe(True) is True


def test_to_response_marks_missing_confidence_reasons():
    out = success_outcome()
    out.validation = None
    r = to_response(out, source="demo", session_id=SID, kind="ask")
    assert r.confidence is None and "not available" in r.confidence_note


def test_store_history_caps_rows(tmp_path):
    s = Store(tmp_path / "x.db")
    resp = to_response(success_outcome(), source="demo", session_id=SID, kind="ask").model_dump(mode="json")
    resp["rows"] = [[i] for i in range(500)]
    s.add_history(session_id=SID, source="demo", kind="ask", response=resp, keep_rows=50)
    got = s.get_history(resp["audit_id"], SID)["response"]
    assert len(got["rows"]) == 50
