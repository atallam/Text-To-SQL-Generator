"""Validator orchestration with fakes: signals are gathered, failures degrade, caps and short-circuit apply."""  # noqa: E501

from types import SimpleNamespace

from tests.helpers import FakeExecutor, ScriptedLLM, exec_result, gen, typed_snapshot
from tests.unit.test_multiquery import PROMPT, PassGate
from text2sql.generation.repair_loop import Attempt, LoopResult
from text2sql.guardrails.config import load_guardrail_config
from text2sql.guardrails.engine import GuardrailEngine
from text2sql.validation import Validator
from text2sql.validation.backtranslate import AlignmentVerdict, BackTranslator, SQLDescription
from text2sql.validation.config import ValidationConfig
from text2sql.validation.multiquery import MultiQueryValidator

SCHEMA = typed_snapshot({"t": {"cols": {"id": "integer", "v": "integer"}}})
CFG = ValidationConfig()
SQL = "SELECT SUM(v) AS s FROM t"
GOOD = AlignmentVerdict(
    entity="match",
    measure="match",
    filters="match",
    time_range="match",
    grouping="match",
    output_shape="match",
)
BAD = GOOD.model_copy(update={"filters": "mismatch", "issues": ["wrong filter"]})


def loop_result(attempts=1, warnings=(), truncated=False, status="success"):
    decision = SimpleNamespace(sql=SQL, tables=["t"], warnings=list(warnings))
    return LoopResult(
        status=status,
        generated=gen(SQL, tables_used=["t"], columns_used=["t.v"]),
        decision=decision,
        execution=exec_result(["s"], [[30]], truncated=truncated),
        attempts=[Attempt(n=i + 1, stage="execution") for i in range(attempts)],
    )


def build(llm, executor=None, multiquery=None):
    executor = executor or FakeExecutor({"SUM": (["s"], [[30]])})
    mq = MultiQueryValidator(
        llm=llm,
        engine=GuardrailEngine(load_guardrail_config()),
        gate=PassGate(),
        executor=executor,
        cfg=multiquery or CFG.multiquery.model_copy(update={"enabled": False}),
    )
    return Validator(
        cfg=CFG, backtranslator=BackTranslator(llm, CFG.backtranslation), multiquery=mq, executor=executor
    )


def bt_llm(verdict=GOOD, *extra):
    return ScriptedLLM(lambda a, s, u: SQLDescription(summary="sums v"), lambda a, s, u: verdict, *extra)


def test_simple_query_uses_four_signals_and_scores_high():
    rep = build(bt_llm()).validate("how much v?", PROMPT, loop_result(), SCHEMA)
    names = {s.name: s for s in rep.confidence.signals}
    assert names["multiquery"].value is None and rep.confidence.signals_used == 4
    assert rep.confidence.level == "high" and rep.alternative is None
    assert rep.back_translation.alignment == 1.0 and rep.sanity is not None
    assert rep.input_tokens > 0


def test_judge_failure_removes_the_signal_but_not_the_answer():
    llm = ScriptedLLM(RuntimeError("judge down"))
    rep = build(llm).validate("how much v?", PROMPT, loop_result(), SCHEMA)
    assert rep.back_translation is None and any("backtranslation" in e for e in rep.errors)
    assert next(s for s in rep.confidence.signals if s.name == "backtranslation").value is None
    assert rep.confidence.score > 0


def test_misaligned_description_lowers_the_score():
    good = build(bt_llm()).validate("q", PROMPT, loop_result(), SCHEMA).confidence.score
    bad = build(bt_llm(BAD)).validate("q", PROMPT, loop_result(), SCHEMA).confidence.score
    assert bad < good - 0.1


def test_a_misaligned_description_is_capped_not_averaged_away():
    rep = build(bt_llm(BAD)).validate("q", PROMPT, loop_result(), SCHEMA)
    assert rep.back_translation.alignment < 0.75 and rep.confidence.score <= 0.6
    assert rep.confidence.level != "high"


def test_repairs_lower_the_syntax_signal():
    one = build(bt_llm()).validate("q", PROMPT, loop_result(1), SCHEMA).confidence.score
    three = build(bt_llm()).validate("q", PROMPT, loop_result(3), SCHEMA).confidence.score
    assert three < one


def test_guardrail_warning_caps_and_limit_notice_does_not():
    capped = build(bt_llm()).validate("q", PROMPT, loop_result(warnings=["G007 deep"]), SCHEMA)
    assert capped.confidence.score == 0.6
    noted = build(bt_llm()).validate(
        "q", PROMPT, loop_result(warnings=["G006 row_limit: LIMIT 1000 applied"]), SCHEMA
    )
    assert noted.confidence.score > 0.9


def test_complex_query_runs_the_second_approach_and_disagreement_caps_at_half():
    def route(agent, system, user):
        if agent == "sql_describer":
            return SQLDescription(summary="x")
        if agent == "sql_judge":
            return GOOD
        return gen("SELECT SUM(v) AS s FROM t")

    llm = ScriptedLLM(route)
    ex = FakeExecutor({"SUM(v) AS s": (["s"], [[31337]]), "COUNT(*)": (["c"], [[10]])})
    mq = CFG.multiquery.model_copy(update={"complexity_threshold": 0})
    rep = build(llm, ex, mq).validate("q", PROMPT, loop_result(), SCHEMA)
    assert rep.alternative.status == "disagree" and rep.confidence.score == 0.5
    assert rep.alternative.preview_rows == [[31337]]


def test_unavailable_second_approach_is_recorded_not_penalised():
    def route(agent, system, user):
        if agent == "sql_describer":
            return SQLDescription(summary="x")
        if agent == "sql_judge":
            return GOOD
        raise RuntimeError("no capacity")

    mq = CFG.multiquery.model_copy(update={"complexity_threshold": 0})
    rep = build(ScriptedLLM(route), multiquery=mq).validate("q", PROMPT, loop_result(), SCHEMA)
    assert rep.alternative.status == "unavailable" and any("multiquery unavailable" in e for e in rep.errors)
    assert rep.confidence.level == "high"


def test_unanswerable_short_circuits_without_any_llm_call():
    llm = ScriptedLLM()
    res = LoopResult(
        status="unanswerable",
        message="there is no supplier table",
        generated=gen("", unanswerable=True, unanswerable_reason="x"),
    )
    rep = build(llm).validate("q", PROMPT, res, SCHEMA)
    assert rep.confidence.level == "cannot_answer" and rep.confidence.score == 0.0
    assert llm.calls == [] and "supplier" in rep.confidence.reasons[0]


def test_validate_refuses_a_failed_result():
    import pytest

    with pytest.raises(ValueError):
        build(bt_llm()).validate("q", PROMPT, LoopResult(status="failed"), SCHEMA)
