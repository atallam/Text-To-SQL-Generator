"""The bounded generate -> guard -> plan -> execute -> repair loop, as a LangGraph state graph.

    generate --> guard --> plan --> execute --> END (success)
       ^           |         |         |
       +-----------+---------+---------+   repairable failure and attempts left -> back to generate
                   (non-repairable failure -> END "blocked"; attempts exhausted -> END "failed")

Properties worth knowing:
* ``max_iterations`` is a hard cap, enforced in the failure handler and again by LangGraph's recursion limit.
* Security violations (G000-G004 and catalog access) are final: the model is not asked to "try again", so
  an injected instruction cannot probe the guardrails by iterating.
* Only SQL that the guardrails re-generated from its own AST (``decision.sql``) ever reaches the planner
  or the database.
* Every attempt is recorded (SQL, stage reached, rule IDs, tokens) for the audit trail.
"""

from __future__ import annotations

import time
from typing import Any, Literal, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from text2sql.execution.executor import ExecutionError, ExecutionResult, Executor
from text2sql.generation.context import PromptBundle
from text2sql.generation.generator import FailedAttempt, GeneratedSQL, SQLGenerator
from text2sql.guardrails.engine import GuardrailDecision, GuardrailEngine
from text2sql.guardrails.explain_gate import ExplainGate, ExplainOutcome
from text2sql.llm.client import LLMOutputError
from text2sql.observability.logging import get_logger
from text2sql.schema.provider import SchemaProvider

log = get_logger("repair_loop")

Status = Literal["success", "blocked", "unanswerable", "failed"]
Stage = Literal["generation", "guardrail", "explain", "execution"]
_TERMINAL = {"success", "blocked", "unanswerable", "failed"}


class Attempt(BaseModel):
    """One pass through the loop, for the audit log."""

    n: int
    sql: str = ""
    stage: Stage  # the furthest stage reached
    ok: bool = False
    rule_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    self_confidence: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    llm_ms: int = 0
    total_ms: int = 0


class LoopResult(BaseModel, arbitrary_types_allowed=True):
    status: Status
    message: str = ""
    generated: GeneratedSQL | None = None
    decision: Any = None  # GuardrailDecision of the last guarded attempt
    explain: Any = None  # ExplainOutcome of the last planned attempt
    execution: ExecutionResult | None = None
    attempts: list[Attempt] = Field(default_factory=list)


class LoopState(TypedDict, total=False):
    prompt: PromptBundle
    iteration: int
    status: str  # "running" | "retry" | terminal
    message: str
    generated: GeneratedSQL | None
    decision: GuardrailDecision | None
    explain: ExplainOutcome | None
    execution: ExecutionResult | None
    failures: list[FailedAttempt]
    attempts: list[Attempt]
    started: float


class RepairLoop:
    def __init__(
        self,
        *,
        generator: SQLGenerator,
        engine: GuardrailEngine,
        gate: ExplainGate,
        executor: Executor,
        schema: SchemaProvider,
        max_iterations: int = 3,
    ):
        if not 1 <= max_iterations <= 10:
            raise ValueError("max_iterations must be between 1 and 10")
        self.generator, self.engine, self.gate = generator, engine, gate
        self.executor, self.schema, self.max_iterations = executor, schema, max_iterations
        self.graph = self._build()

    # -- graph ----------------------------------------------------------------------------------
    def _build(self):
        g = StateGraph(LoopState)
        g.add_node("generate", self._generate)
        g.add_node("guard", self._guard)
        g.add_node("plan", self._plan)
        g.add_node("execute", self._execute)
        g.set_entry_point("generate")

        def edge(next_node: str):
            def route(state: LoopState) -> str:
                status = state.get("status", "running")
                if status == "retry":
                    return "generate"
                return END if status in _TERMINAL else next_node

            return route

        g.add_conditional_edges(
            "generate", edge("guard"), {"guard": "guard", "generate": "generate", END: END}
        )
        g.add_conditional_edges("guard", edge("plan"), {"plan": "plan", "generate": "generate", END: END})
        g.add_conditional_edges(
            "plan", edge("execute"), {"execute": "execute", "generate": "generate", END: END}
        )
        g.add_conditional_edges("execute", edge(END), {"generate": "generate", END: END})
        return g.compile()

    def run(self, prompt: PromptBundle) -> LoopResult:
        init: LoopState = {
            "prompt": prompt,
            "iteration": 0,
            "status": "running",
            "message": "",
            "generated": None,
            "decision": None,
            "explain": None,
            "execution": None,
            "failures": [],
            "attempts": [],
        }
        final = self.graph.invoke(init, config={"recursion_limit": self.max_iterations * 4 + 6})
        return LoopResult(
            status=final["status"] if final["status"] in _TERMINAL else "failed",
            message=final.get("message", ""),
            generated=final.get("generated"),
            decision=final.get("decision"),
            explain=final.get("explain"),
            execution=final.get("execution"),
            attempts=final["attempts"],
        )

    # -- helpers --------------------------------------------------------------------------------
    @staticmethod
    def _last(state: LoopState) -> Attempt:
        return state["attempts"][-1]

    def _fail(
        self,
        state: LoopState,
        stage: Stage,
        error: str,
        *,
        repairable: bool,
        rule_ids: list[str] | None = None,
    ) -> LoopState:
        """Record a failure and decide: retry, or end as blocked / failed."""
        attempt = self._last(state)
        attempt.stage, attempt.error, attempt.rule_ids = stage, error, rule_ids or []
        failures = list(state["failures"])
        generated = state.get("generated")
        failures.append(
            FailedAttempt(n=attempt.n, sql=generated.sql if generated else "", stage=stage, error=error)
        )
        if not repairable:
            log.warning("loop_blocked", stage=stage, rule_ids=attempt.rule_ids, error=error)
            return {"failures": failures, "status": "blocked", "message": error}
        if state["iteration"] >= self.max_iterations:
            log.warning("loop_exhausted", attempts=state["iteration"], last_error=error)
            msg = f"No valid query after {state['iteration']} attempts. Last problem: {error}"
            return {"failures": failures, "status": "failed", "message": msg}
        log.info("loop_retry", attempt=attempt.n, stage=stage, error=error[:200])
        return {"failures": failures, "status": "retry", "message": error}

    # -- nodes ----------------------------------------------------------------------------------
    def _generate(self, state: LoopState) -> LoopState:
        n = state["iteration"] + 1
        attempt = Attempt(n=n, stage="generation")
        attempts = [*state["attempts"], attempt]
        fresh = {"iteration": n, "attempts": attempts, "decision": None, "explain": None, "execution": None}
        t0 = time.perf_counter()
        try:
            res = self.generator.generate(state["prompt"], state["failures"] or None)
        except LLMOutputError as exc:
            attempt.error, attempt.total_ms = str(exc)[:500], int((time.perf_counter() - t0) * 1000)
            return {**fresh, "status": "failed", "message": f"The model did not return a valid answer: {exc}"}
        except Exception as exc:  # noqa: BLE001  API/network failure after the client's own retries
            attempt.error, attempt.total_ms = repr(exc)[:500], int((time.perf_counter() - t0) * 1000)
            log.error("generation_error", error=repr(exc))
            return {**fresh, "status": "failed", "message": "The language model is unavailable."}
        gen = res.parsed
        attempt.sql, attempt.self_confidence = gen.sql, gen.self_confidence
        attempt.input_tokens, attempt.output_tokens = res.usage.input_tokens, res.usage.output_tokens
        attempt.llm_ms = attempt.total_ms = int((time.perf_counter() - t0) * 1000)
        if gen.unanswerable:
            attempt.ok, attempt.error = True, gen.unanswerable_reason
            return {
                **fresh,
                "generated": gen,
                "status": "unanswerable",
                "message": gen.unanswerable_reason or "",
            }
        return {**fresh, "generated": gen, "status": "running"}

    def _guard(self, state: LoopState) -> LoopState:
        attempt = self._last(state)
        attempt.stage = "guardrail"
        decision = self.engine.check(state["generated"].sql, self.schema.get())
        if decision.allowed:
            return {"decision": decision}
        update = self._fail(
            state, "guardrail", decision.reason, repairable=decision.repairable, rule_ids=decision.rule_ids
        )
        return {**update, "decision": decision}

    def _plan(self, state: LoopState) -> LoopState:
        attempt = self._last(state)
        attempt.stage = "explain"
        outcome = self.gate.check(state["decision"].sql)
        if outcome.ok:
            return {"explain": outcome}
        if outcome.error:  # does not plan: unknown column, type mismatch, ... -> the model can fix it
            update = self._fail(state, "explain", outcome.error, repairable=True)
        else:
            reasons = "; ".join(v.message for v in outcome.violations or [])
            ids = sorted({v.rule_id for v in outcome.violations or []})
            update = self._fail(
                state,
                "explain",
                reasons,
                repairable=all(v.repairable for v in outcome.violations or []),
                rule_ids=ids,
            )
        return {**update, "explain": outcome}

    def _execute(self, state: LoopState) -> LoopState:
        attempt = self._last(state)
        attempt.stage = "execution"
        try:
            result = self.executor.run(state["decision"].sql)
        except ExecutionError as exc:
            msg = exc.message
            if exc.kind == "timeout":
                msg = f"The query was cancelled at the time limit ({msg}). Make it cheaper: filter earlier, aggregate, avoid cross joins."  # noqa: E501
            return self._fail(state, "execution", msg, repairable=exc.repairable)
        attempt.ok = True
        attempt.total_ms += result.elapsed_ms
        return {"execution": result, "status": "success", "message": ""}
