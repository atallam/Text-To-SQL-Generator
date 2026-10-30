"""The guardrail rules. Each is a small function: facts in, violations out. IDs are stable (logged, audited,
referenced from guardrails.yaml and the red-team fixtures)."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

from sqlglot import exp

from text2sql.guardrails import parser
from text2sql.guardrails.config import GuardrailConfig
from text2sql.schema.model import SchemaSnapshot

RULE_NAMES = {
    "G000": "parse",
    "G001": "single_statement",
    "G002": "statement_allowlist",
    "G003": "forbidden_constructs",
    "G004": "function_denylist",
    "G005": "table_allowlist",
    "G006": "row_limit",
    "G007": "subquery_depth",
    "G008": "explain_gate",
}

CATALOG_SCHEMAS = {"pg_catalog", "information_schema"}


@dataclass(frozen=True)
class Violation:
    rule_id: str
    message: str
    repairable: bool = False

    @property
    def rule(self) -> str:
        return RULE_NAMES.get(self.rule_id, self.rule_id)

    def __str__(self) -> str:
        return f"{self.rule_id} {self.rule}: {self.message}"


def single_statement(statements: list) -> list[Violation]:
    if len(statements) > 1:
        return [Violation("G001", f"{len(statements)} statements found; exactly one is allowed")]
    return []


def statement_allowlist(tree: exp.Expression) -> list[Violation]:
    if parser.is_read_query(tree):
        return []
    return [
        Violation("G002", f"only SELECT / WITH ... SELECT is allowed (got {type(tree).__name__.upper()})")
    ]


def forbidden_constructs(tree: exp.Expression) -> list[Violation]:
    found = parser.forbidden_nodes(tree)
    if not found:
        return []
    labels = {
        "Into": "SELECT INTO",
        "Lock": "row locking (FOR UPDATE/SHARE)",
        "Command": "an unsupported/administrative command",
    }
    pretty = ", ".join(labels.get(n, n.upper()) for n in found)
    return [Violation("G003", f"not allowed in a read-only query: {pretty}")]


def function_denylist(tree: exp.Expression, cfg: GuardrailConfig) -> list[Violation]:
    rule = cfg.functions
    allow = [p.lower() for p in rule.allow]
    deny = [p.lower() for p in rule.deny]
    blocked = sorted(
        name
        for name in parser.function_names(tree)
        if any(fnmatchcase(name, p) for p in deny) and not any(fnmatchcase(name, p) for p in allow)
    )
    if not blocked:
        return []
    return [Violation("G004", f"function not allowed: {', '.join(blocked)}", rule.repairable)]


def table_allowlist(
    tree: exp.Expression, cfg: GuardrailConfig, schema: SchemaSnapshot | None
) -> list[Violation]:
    """Only registered tables in registered schemas. Skipped when no schema is available (eval harness)."""
    rule = cfg.tables
    if schema is None:
        return []
    known = {n for n in schema.table_names} | set(rule.extra_tables)
    schemas = set(rule.allowed_schemas) or {schema.schema_name}
    out: list[Violation] = []
    seen: set[str] = set()
    for ref in parser.physical_tables(tree):
        if ref.qualified in seen:
            continue
        seen.add(ref.qualified)
        if ref.catalog:
            out.append(Violation("G005", f"cross-database reference {ref.qualified!r} is not allowed"))
        elif (
            ref.schema in CATALOG_SCHEMAS
            or (ref.schema or "").startswith("pg_")
            or (ref.schema is None and ref.name.startswith("pg_"))
        ):
            if not rule.allow_catalog:
                out.append(Violation("G005", f"system catalog access ({ref.qualified!r}) is not allowed"))
        elif ref.schema is not None and ref.schema not in schemas:
            out.append(Violation("G005", f"schema {ref.schema!r} is not allowed", rule.repairable))
        elif ref.name not in known:
            out.append(
                Violation(
                    "G005",
                    f"table {ref.qualified!r} does not exist in the schema; available: {', '.join(sorted(known))}",  # noqa: E501
                    rule.repairable,
                )
            )
    return out


def subquery_depth(tree: exp.Expression, cfg: GuardrailConfig) -> tuple[list[Violation], int]:
    depth = parser.subquery_depth(tree)
    if depth > cfg.subquery_depth.max_depth:
        msg = f"subquery nesting depth {depth} exceeds the limit of {cfg.subquery_depth.max_depth}; use CTEs or joins"  # noqa: E501
        return [Violation("G007", msg, cfg.subquery_depth.repairable)], depth
    return [], depth


def apply_row_limit(
    tree: exp.Expression, cfg: GuardrailConfig
) -> tuple[exp.Expression, int | None, list[Violation]]:
    """Inject or lower the LIMIT. Returns (tree, applied_limit_or_None, violations).

    ``rewrite`` mode fixes the query silently (reported as a warning by the engine); ``reject`` blocks it.
    """
    rule = cfg.row_limit
    if rule.mode == "off":
        return tree, None, []
    current = tree.args.get("limit")
    fine = False
    if isinstance(current, exp.Limit):
        count = current.expression
        fine = (
            isinstance(count, exp.Literal)
            and not count.is_string
            and count.is_int
            and int(count.name) <= rule.max_rows
        )
    elif isinstance(current, exp.Fetch):
        count = current.args.get("count")
        opts = current.args.get("limit_options")
        percent = bool(opts and opts.args.get("percent"))
        ties = bool(opts and opts.args.get("with_ties"))
        fine = (
            isinstance(count, exp.Literal)
            and count.is_int
            and int(count.name) <= rule.max_rows
            and not percent
            and not ties
        )
    if fine:
        return tree, None, []
    if rule.mode == "reject":
        why = "has no LIMIT" if current is None else f"has a LIMIT above {rule.max_rows} rows"
        return tree, None, [Violation("G006", f"query {why} (max {rule.max_rows}).", True)]
    tree.set("limit", exp.Limit(expression=exp.Literal.number(rule.max_rows)))
    return tree, rule.max_rows, []
