"""The bounded repair loop with a scripted model and fake gate/executor: no network, no database."""

import pytest
from pydantic import ValidationError

from tests.helpers import ScriptedLLM, gen, make_snapshot
from text2sql.execution.executor import ExecutionError, ExecutionResult
from text2sql.generation.context import PromptBundle
from text2sql.generation.generator import GeneratedSQL, SQLGenerator
from text2sql.generation.repair_loop import RepairLoop
from text2sql.guardrails import GuardrailEngine, load_guardrail_config
from text2sql.guardrails.explain_gate import ExplainOutcome
from text2sql.guardrails.rules import Violation
from text2sql.llm.client import LLMOutputError
from text2sql.schema.provider import SchemaProvider

SNAP = make_snapshot({"orders": {"cols": ["id", "amount"]}, "suppliers": {"cols": ["id", "name"]}})
PROMPT = PromptBundle(
    name="sql_generation",
    version="v2",
    system="SYSTEM",
    user="## Question\nhow many orders?",
    tables=["orders"],
    table_reasons={},
    examples=[],
    glossary_terms=[],
    schema_fingerprint=SNAP.fingerprint,
)


class FakeGate:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def check(self, sql):
        self.calls.append(sql)
        return (
            self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
            if self.outcomes
            else ExplainOutcome(ok=True)
        )


class FakeExecutor:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def run(self, sql):
        self.calls.append(sql)
        item = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def result(sql="x"):
    return ExecutionResult(
        columns=["n"], rows=[[1]], row_count=1, truncated=False, elapsed_ms=3, result_hash="h", sql=sql
    )


def make_loop(llm, gate=None, executor=None, max_iterations=3):
    return RepairLoop(
        generator=SQLGenerator(llm),
        engine=GuardrailEngine(load_guardrail_config()),
        gate=gate or FakeGate(),
        executor=executor or FakeExecutor(result()),
        schema=SchemaProvider(lambda: SNAP, lambda: SNAP.fingerprint),
        max_iterations=max_iterations,
    ), (gate, executor)


def test_happy_path_runs_the_canonical_sql_once():
    ex = FakeExecutor(result())
    loop, _ = make_loop(ScriptedLLM(gen("SELECT count(*) FROM orders -- note")), executor=ex)
    res = loop.run(PROMPT)
    assert res.status == "success" and len(res.attempts) == 1 and res.attempts[0].ok
    assert ex.calls == ["SELECT COUNT(*) FROM orders LIMIT 1000"]  # regenerated, comment gone, LIMIT added
    assert res.attempts[0].input_tokens == 100 and res.attempts[0].self_confidence == 0.9


def test_unanswerable_short_circuits_before_any_database_work():
    gate, ex = FakeGate(), FakeExecutor(result())
    llm = ScriptedLLM(gen("", unanswerable=True, unanswerable_reason="No weather data exists."))
    res = make_loop(llm, gate, ex)[0].run(PROMPT)
    assert res.status == "unanswerable" and "weather" in res.message
    assert gate.calls == [] and ex.calls == []


def test_unknown_table_is_repaired_using_the_guardrail_message():
    llm = ScriptedLLM(gen("SELECT count(*) FROM order"), gen("SELECT count(*) FROM orders"))
    res = make_loop(llm)[0].run(PROMPT)
    assert res.status == "success" and [a.n for a in res.attempts] == [1, 2]
    assert res.attempts[0].stage == "guardrail" and res.attempts[0].rule_ids == ["G005"]
    repair_prompt = llm.calls[1]["user"]
    assert llm.calls[1]["agent"] == "sql_repair"
    assert (
        "SELECT count(*) FROM order" in repair_prompt
        and "G005" in repair_prompt
        and "how many orders" in repair_prompt
    )


def test_planner_error_is_fed_back_and_fixed():
    gate = FakeGate(ExplainOutcome(ok=False, error='column "total" does not exist'), ExplainOutcome(ok=True))
    llm = ScriptedLLM(gen("SELECT total FROM orders"), gen("SELECT amount FROM orders"))
    res = make_loop(llm, gate)[0].run(PROMPT)
    assert res.status == "success"
    assert res.attempts[0].stage == "explain" and 'column "total" does not exist' in llm.calls[1]["user"]


def test_execution_timeout_is_repairable_but_permission_denied_is_final():
    ex = FakeExecutor(ExecutionError("timeout", "canceling statement due to statement timeout"), result())
    res = make_loop(ScriptedLLM(gen("SELECT * FROM orders")), executor=ex)[0].run(PROMPT)
    assert res.status == "success" and len(res.attempts) == 2

    ex = FakeExecutor(ExecutionError("permission", "permission denied for table orders"), result())
    res = make_loop(ScriptedLLM(gen("SELECT * FROM orders")), executor=ex)[0].run(PROMPT)
    assert res.status == "blocked" and len(res.attempts) == 1 and len(ex.calls) == 1


def test_the_loop_is_bounded_and_reports_failure():
    llm = ScriptedLLM(gen("SELECT * FROM nope"))  # same hallucination forever
    res = make_loop(llm, max_iterations=3)[0].run(PROMPT)
    assert res.status == "failed" and len(res.attempts) == 3 and len(llm.calls) == 3
    assert "3 attempts" in res.message


def test_max_iterations_one_means_no_repair():
    llm = ScriptedLLM(gen("SELECT * FROM nope"), gen("SELECT * FROM orders"))
    res = make_loop(llm, max_iterations=1)[0].run(PROMPT)
    assert res.status == "failed" and len(llm.calls) == 1


@pytest.mark.parametrize(
    "sql, rule",
    [
        ("DROP TABLE orders", "G002"),
        ("SELECT 1; DELETE FROM orders", "G001"),
        ("SELECT pg_sleep(60)", "G004"),
        ("SELECT * FROM pg_catalog.pg_tables", "G005"),
    ],
)
def test_security_violations_end_the_request_and_are_never_retried(sql, rule):
    gate, ex = FakeGate(), FakeExecutor(result())
    llm = ScriptedLLM(gen(sql), gen("SELECT count(*) FROM orders"))  # a second, good answer is available...
    res = make_loop(llm, gate, ex)[0].run(PROMPT)
    assert res.status == "blocked" and rule in res.attempts[0].rule_ids
    assert len(llm.calls) == 1  # ...but the model is not given another chance
    assert gate.calls == [] and ex.calls == []  # nothing reached the planner or the database


def test_cost_gate_violation_is_repairable():
    too_expensive = ExplainOutcome(
        ok=False, violations=[Violation("G008", "estimated cost 9e9 exceeds 1e6", True)]
    )
    gate = FakeGate(too_expensive, ExplainOutcome(ok=True))
    res = make_loop(ScriptedLLM(gen("SELECT * FROM orders a, orders b")), gate)[0].run(PROMPT)
    assert res.status == "success" and res.attempts[0].rule_ids == ["G008"]


def test_llm_failures_end_the_loop_cleanly():
    res = make_loop(ScriptedLLM(LLMOutputError("no valid output")))[0].run(PROMPT)
    assert res.status == "failed" and "valid answer" in res.message
    res = make_loop(ScriptedLLM(ConnectionError("down")))[0].run(PROMPT)
    assert res.status == "failed" and "unavailable" in res.message


def test_stale_state_from_an_earlier_attempt_does_not_leak_into_the_result():
    llm = ScriptedLLM(gen("SELECT * FROM nope"), LLMOutputError("boom"))
    res = make_loop(llm)[0].run(PROMPT)
    assert res.status == "failed" and res.decision is None


def test_invalid_max_iterations_is_rejected():
    with pytest.raises(ValueError):
        make_loop(ScriptedLLM(gen()), max_iterations=0)


# -- the structured output model -----------------------------------------------------------------
def test_generated_sql_model_rules():
    assert GeneratedSQL(sql="SELECT 1", explanation="x").unanswerable is False
    with pytest.raises(ValidationError):
        GeneratedSQL(sql="  ", explanation="x")  # empty SQL needs unanswerable=true
    with pytest.raises(ValidationError):
        GeneratedSQL(unanswerable=True, explanation="x")  # a reason is required
    with pytest.raises(ValidationError):
        GeneratedSQL(sql="SELECT 1", explanation="x", self_confidence=1.5)
    refused = GeneratedSQL(
        sql="DROP TABLE x", unanswerable=True, unanswerable_reason="not a read", explanation="x"
    )
    assert refused.sql == ""  # an unanswerable answer never carries SQL
