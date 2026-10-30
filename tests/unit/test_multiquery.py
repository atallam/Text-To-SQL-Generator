"""Multi-query validation: canonical comparison, complexity gate, safe path for the second query."""

import datetime as dt
from decimal import Decimal

import pytest

from tests.helpers import FakeExecutor, ScriptedLLM, exec_result, gen, typed_snapshot
from text2sql.generation.context import PromptBundle
from text2sql.guardrails.config import load_guardrail_config
from text2sql.guardrails.engine import GuardrailEngine
from text2sql.validation.config import MultiQueryConfig
from text2sql.validation.multiquery import MultiQueryValidator, compare_results, complexity

CFG = MultiQueryConfig()


def cmp(a, b):
    return compare_results(a, b, CFG)


def test_identical_results_agree():
    r = exec_result(["a", "b"], [[1, "x"], [2, "y"]])
    assert cmp(r, r).kind == "exact"


def test_aliases_column_order_and_row_order_do_not_matter():
    a = exec_result(["category", "spend"], [["food", 10.0], ["rent", 99.5], ["fun", 3.0]])
    b = exec_result(["total", "cat"], [[3.0, "fun"], [10.0, "food"], [99.5, "rent"]])
    assert cmp(a, b).agree and cmp(a, b).kind == "exact"


def test_numeric_tolerance_and_types():
    a = exec_result(["v"], [[Decimal("100.004")], [5]])
    b = exec_result(["v"], [[100.0], [5.0]])
    assert cmp(a, b).agree
    c = exec_result(["v"], [[101.0], [5.0]])
    assert not cmp(a, c).agree


def test_dates_and_datetimes_normalise():
    a = exec_result(["d"], [[dt.date(2026, 9, 1)]])
    b = exec_result(["d"], [[dt.datetime(2026, 9, 1, 0, 0)]])
    assert cmp(a, b).agree


def test_different_row_counts_disagree_with_overlap():
    a = exec_result(["x"], [[1], [2], [3], [4]])
    b = exec_result(["x"], [[1], [2], [3]])
    c = cmp(a, b)
    assert not c.agree and c.kind == "shape_mismatch" and c.row_overlap == 0.75


def test_different_values_disagree():
    a = exec_result(["n"], [[10]])
    b = exec_result(["n"], [[12]])
    assert cmp(a, b).kind == "column_mismatch"


def test_same_columns_paired_into_different_rows_is_a_row_mismatch():
    a = exec_result(["k", "v"], [["a", 1], ["b", 2]])
    b = exec_result(["k", "v"], [["a", 2], ["b", 1]])
    assert cmp(a, b).kind == "row_mismatch"


def test_extra_columns_still_agree():
    a = exec_result(["spend"], [[10.0], [20.0]])
    b = exec_result(["label", "spend"], [["x", 20.0], ["y", 10.0]])
    c = cmp(a, b)
    assert c.agree and c.kind == "extra_columns"


def test_two_empty_results_agree_and_empty_vs_nonempty_does_not():
    assert cmp(exec_result(["a"], []), exec_result(["b"], [])).agree
    assert not cmp(exec_result(["a"], []), exec_result(["a"], [[1]])).agree


def test_nulls_compare_equal_to_nulls():
    assert cmp(exec_result(["a"], [[None], [1]]), exec_result(["b"], [[1], [None]])).agree


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT 1", 0),
        ("SELECT a FROM t WHERE b = 1", 0),
        ("SELECT a, SUM(b) FROM t GROUP BY a", 1),
        ("SELECT a FROM t JOIN u ON u.id = t.u_id JOIN v ON v.id = u.v_id", 2),
        ("WITH x AS (SELECT 1) SELECT * FROM x JOIN y ON y.id = x.id GROUP BY 1", 3),
        ("SELECT a, ROW_NUMBER() OVER (ORDER BY b) FROM t", 1),
    ],
)
def test_complexity_score(sql, expected):
    assert complexity(sql) == expected


# -- the second query goes through the same safety path --------------------------------------------------
SCHEMA = typed_snapshot({"t": {"cols": {"id": "integer", "v": "integer"}}})
PROMPT = PromptBundle(
    name="sql_generation",
    version="v2",
    system="SYSTEM",
    user="## Question\n<question>\nhow much v?\n</question>",
    tables=["t"],
    table_reasons={},
    examples=[],
    glossary_terms=[],
    schema_fingerprint=SCHEMA.fingerprint,
)


class PassGate:
    def check(self, sql):
        from text2sql.guardrails.explain_gate import ExplainOutcome

        return ExplainOutcome(ok=True, total_cost=1.0, plan_rows=1.0)


def validator(llm, executor, **cfg):
    return MultiQueryValidator(
        llm=llm,
        engine=GuardrailEngine(load_guardrail_config()),
        gate=PassGate(),
        executor=executor,
        cfg=MultiQueryConfig(**cfg),
    )


PRIMARY = exec_result(["total"], [[30]])


def test_applies_only_above_the_complexity_threshold():
    v = validator(ScriptedLLM(), FakeExecutor(), complexity_threshold=2)
    assert not v.applies("SELECT SUM(v) FROM t")
    assert v.applies("SELECT id, SUM(v) FROM t GROUP BY id HAVING SUM(v) > 1")
    assert not validator(ScriptedLLM(), FakeExecutor(), enabled=False).applies(
        "SELECT id, SUM(v) FROM t GROUP BY id HAVING SUM(v) > 1"
    )


def test_agreement_and_prompt_contents():
    llm = ScriptedLLM(gen("SELECT SUM(v) AS s FROM t"))
    ex = FakeExecutor({"SUM": (["s"], [[30]])})
    run = validator(llm, ex).run(PROMPT, "SELECT SUM(v) FROM t GROUP BY id", PRIMARY, SCHEMA)
    assert run.status == "agree" and run.comparison.kind == "exact"
    call = llm.calls[0]
    assert call["agent"] == "sql_alternative" and call["system"] == "SYSTEM"
    assert "Approach A" in call["user"] and "SELECT SUM(v) FROM t GROUP BY id" in call["user"]
    assert "how much v?" in call["user"]


def test_disagreement_keeps_the_second_result_for_display():
    llm = ScriptedLLM(gen("SELECT SUM(v) AS s FROM t"))
    ex = FakeExecutor({"SUM": (["s"], [[99]])})
    run = validator(llm, ex).run(PROMPT, "SELECT 1", PRIMARY, SCHEMA)
    assert run.status == "disagree" and run.preview_rows == [[99]] and run.columns == ["s"]


def test_unsafe_second_query_is_refused_and_never_executed():
    llm = ScriptedLLM(gen("DELETE FROM t"))
    ex = FakeExecutor()
    run = validator(llm, ex).run(PROMPT, "SELECT 1", PRIMARY, SCHEMA)
    assert run.status == "unavailable" and "guardrails" in run.reason and ex.sqls == []


def test_second_query_failures_are_unavailable_not_disagreement():
    run = validator(ScriptedLLM(RuntimeError("api down")), FakeExecutor()).run(
        PROMPT, "SELECT 1", PRIMARY, SCHEMA
    )
    assert run.status == "unavailable" and "api down" in run.reason
    run = validator(ScriptedLLM(gen("SELECT SUM(v) FROM t")), FakeExecutor()).run(
        PROMPT, "SELECT 1", PRIMARY, SCHEMA
    )
    assert run.status == "unavailable" and "failed to run" in run.reason
    unans = gen("", unanswerable=True, unanswerable_reason="no data")
    run = validator(ScriptedLLM(unans), FakeExecutor()).run(PROMPT, "SELECT 1", PRIMARY, SCHEMA)
    assert run.status == "unavailable"
