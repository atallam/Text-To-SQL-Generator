"""GuardrailEngine: parse -> rules -> rewritten, canonical SQL (or a list of violations).

Key design decision: the SQL that is executed is **re-generated from the validated AST**, never the
original text. Whatever tricks the input used (comments, odd whitespace, casing, nested comment
syntax), the database receives exactly the tree we analysed. The engine also re-parses its own output
and refuses if the round trip changes the tree.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from text2sql.guardrails import parser, rules
from text2sql.guardrails.config import GuardrailConfig
from text2sql.guardrails.rules import Violation
from text2sql.observability.logging import get_logger
from text2sql.schema.model import SchemaSnapshot

log = get_logger("guardrails")


@dataclass
class GuardrailDecision:
    original_sql: str
    allowed: bool
    sql: str | None = None  # canonical, limit-enforced SQL: the only text that may be executed
    violations: list[Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    depth: int = 0
    row_limit_applied: int | None = None

    @property
    def blocked(self) -> bool:  # the attribute the eval harness looks for
        return not self.allowed

    @property
    def reason(self) -> str:
        return "; ".join(str(v) for v in self.violations)

    @property
    def rule_ids(self) -> list[str]:
        return sorted({v.rule_id for v in self.violations})

    @property
    def repairable(self) -> bool:
        """True when every violation is something the model can plausibly fix on a retry."""
        return bool(self.violations) and all(v.repairable for v in self.violations)


class GuardrailEngine:
    def __init__(self, config: GuardrailConfig | None = None):
        self.config = config or GuardrailConfig()

    def check(self, sql: str, schema: SchemaSnapshot | None = None) -> GuardrailDecision:
        decision = self._check(sql, schema)
        digest = hashlib.sha256(sql.encode("utf-8", "replace")).hexdigest()[:12]
        if decision.allowed:
            log.info("guardrail_allow", sql_hash=digest, tables=decision.tables, warnings=decision.warnings)
        else:
            for v in decision.violations:  # one log line per violated rule, with its ID
                log.warning(
                    "guardrail_block",
                    sql_hash=digest,
                    rule_id=v.rule_id,
                    rule=v.rule,
                    reason=v.message,
                    sql_preview=" ".join(sql.split())[:160],
                )
        return decision

    # ------------------------------------------------------------------------------------------
    def _check(self, sql: str, schema: SchemaSnapshot | None) -> GuardrailDecision:
        cfg = self.config
        out = GuardrailDecision(original_sql=sql, allowed=False)

        problem = parser.precheck(sql, cfg.max_sql_chars)
        if problem:
            out.violations.append(Violation("G000", problem))
            return out

        parsed = parser.parse(sql, cfg.dialect)
        if parsed.error:
            out.violations.append(Violation("G000", parsed.error, repairable=True))
            return out

        out.violations += rules.single_statement(parsed.statements)
        tree = parsed.statements[0]
        if parser.node_count(tree) > parser.MAX_NODES:
            out.violations.append(Violation("G000", "query is too large to analyse"))
            return out

        out.violations += rules.statement_allowlist(tree)
        out.violations += rules.forbidden_constructs(tree)
        if out.violations:
            # Not a read query: stop here, do not run read-oriented rules on something unsafe.
            return out

        if cfg.functions.mode != "off":
            found = rules.function_denylist(tree, cfg)
            out.violations += found if cfg.functions.mode == "block" else []
            out.warnings += [str(v) for v in found] if cfg.functions.mode == "warn" else []
        if cfg.tables.mode != "off":
            found = rules.table_allowlist(tree, cfg, schema)
            out.violations += found if cfg.tables.mode == "block" else []
            out.warnings += [str(v) for v in found] if cfg.tables.mode == "warn" else []
        if cfg.subquery_depth.mode != "off":
            found, out.depth = rules.subquery_depth(tree, cfg)
            out.violations += found if cfg.subquery_depth.mode == "block" else []
            out.warnings += [str(v) for v in found] if cfg.subquery_depth.mode == "warn" else []

        out.tables = sorted({r.qualified for r in parser.physical_tables(tree)})
        out.functions = sorted(parser.function_names(tree))

        tree, applied, found = rules.apply_row_limit(tree, cfg)
        out.violations += found
        if applied:
            out.row_limit_applied = applied
            out.warnings.append(f"G006 row_limit: LIMIT {applied} applied")
        if out.violations:
            out.warnings = [w for w in out.warnings if not w.startswith("G006")]
            out.row_limit_applied = None
            return out

        text = parser.render(tree, cfg.dialect)
        again = parser.parse(text, cfg.dialect)
        if again.error or len(again.statements) != 1 or again.statements[0] != tree:
            out.violations.append(Violation("G000", "query does not survive a parse/regenerate round trip"))
            return out

        out.allowed, out.sql = True, text
        return out
