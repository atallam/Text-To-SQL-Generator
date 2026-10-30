"""EXPLAIN cost gate (G008). Runs ``EXPLAIN (FORMAT JSON)`` - which plans but never executes - inside the
read-only sandbox. Doubles as a cheap validity check: unknown columns, type errors and bad joins show up
here as planner errors, which the repair loop can feed back to the model before any data is read."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import DBAPIError

from text2sql.connectors.base import Connector
from text2sql.guardrails.config import ExplainRule
from text2sql.guardrails.rules import Violation
from text2sql.observability.logging import get_logger

log = get_logger("explain_gate")


@dataclass
class ExplainOutcome:
    ok: bool
    skipped: bool = False
    total_cost: float | None = None
    plan_rows: float | None = None
    plan: dict[str, Any] | None = None
    violations: list[Violation] | None = None
    error: str | None = None  # planner error text (repairable), when the SQL does not plan

    @property
    def blocked(self) -> bool:
        return not self.ok


def db_error_message(exc: BaseException) -> str:
    """The database's own message, without SQLAlchemy's wrapper or the echoed statement."""
    orig = getattr(exc, "orig", exc)
    diag = getattr(orig, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag else None
    hint = getattr(diag, "message_hint", None) if diag else None
    text = primary or str(orig).splitlines()[0]
    return f"{text} (hint: {hint})" if hint else text


class ExplainGate:
    def __init__(self, connector: Connector, rule: ExplainRule, timeout_ms: int | None = None):
        self.connector = connector
        self.rule = rule
        self.timeout_ms = timeout_ms

    def check(self, safe_sql: str) -> ExplainOutcome:
        if self.rule.mode == "off" or self.connector.dialect != "postgresql":
            return ExplainOutcome(ok=True, skipped=True)
        try:
            with self.connector.readonly_connection(self.timeout_ms) as conn:
                raw = conn.exec_driver_sql(
                    self.connector.driver_sql(f"EXPLAIN (FORMAT JSON) {safe_sql}")
                ).scalar()
        except DBAPIError as exc:
            message = db_error_message(exc)
            log.info("explain_error", error=message)
            return ExplainOutcome(ok=False, error=message)
        doc = json.loads(raw) if isinstance(raw, str) else raw
        plan = doc[0]["Plan"]
        cost, rows = float(plan["Total Cost"]), float(plan["Plan Rows"])
        out = ExplainOutcome(ok=True, total_cost=cost, plan_rows=rows, plan=plan)
        found: list[Violation] = []
        if cost > self.rule.max_total_cost:
            found.append(
                Violation(
                    "G008",
                    f"estimated cost {cost:,.0f} exceeds {self.rule.max_total_cost:,.0f}",
                    self.rule.repairable,
                )
            )
        if rows > self.rule.max_plan_rows:
            found.append(
                Violation(
                    "G008",
                    f"estimated rows {rows:,.0f} exceeds {self.rule.max_plan_rows:,.0f}",
                    self.rule.repairable,
                )
            )
        if found and self.rule.mode == "block":
            out.ok, out.violations = False, found
            log.warning(
                "guardrail_block",
                rule_id="G008",
                rule="explain_gate",
                reason="; ".join(v.message for v in found),
            )
        elif found:
            out.violations = found  # mode == "warn": reported, not blocking
        return out
