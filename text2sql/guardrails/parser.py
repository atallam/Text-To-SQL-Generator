"""SQL parsing and structural analysis (sqlglot AST). No rules live here, only facts about the query.

Why an AST and not regex: keywords hide in comments, string literals, quoted identifiers, dollar-quoted
bodies and odd casing. The parser has already resolved all of that, so rules look at node types.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError, TokenError
from sqlglot.optimizer.scope import traverse_scope

# sqlglot logs "unsupported syntax, falling back to Command" at WARNING; that is expected input here.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# Anything that is not a read: if one of these appears ANYWHERE in the tree (including inside a CTE or
# subquery), the statement is refused. Names are looked up defensively so a sqlglot upgrade that
# renames a class cannot crash the import.
_FORBIDDEN_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "AlterTable",
    "AlterColumn",
    "TruncateTable",
    "Command",
    "Copy",
    "Set",
    "SetItem",
    "Use",
    "Grant",
    "Revoke",
    "Pragma",
    "Transaction",
    "Commit",
    "Rollback",
    "Analyze",
    "Attach",
    "Detach",
    "Kill",
    "LoadData",
    "Show",
    "Describe",
    "Declare",
    "Comment",
    "Refresh",
    "Cache",
    "Uncache",
    "Export",
    "Install",
    "Execute",
    "Call",
    "Into",
    "Lock",
    "Rename",
    "RenameTable",
    "Vacuum",
)
FORBIDDEN_NODE_TYPES: tuple[type[exp.Expression], ...] = tuple(
    cls for name in _FORBIDDEN_NAMES if isinstance(cls := getattr(exp, name, None), type)
) + tuple(cls for name in ("DML", "DDL") if isinstance(cls := getattr(exp, name, None), type))

MAX_NODES = 5_000  # a legitimate analytics query is far smaller; bounds work done by later checks


@dataclass
class ParsedSQL:
    statements: list[exp.Expression] = field(default_factory=list)
    error: str | None = None

    @property
    def tree(self) -> exp.Expression | None:
        return self.statements[0] if self.statements else None


def precheck(sql: str, max_chars: int) -> str | None:
    """Reject input that should never reach the parser. Returns a reason, or None when fine."""
    if not sql or not sql.strip():
        return "empty SQL"
    if len(sql) > max_chars:
        return f"SQL is {len(sql)} characters long (limit {max_chars})"
    for ch in sql:
        if ch == "\x00" or (ord(ch) < 32 and ch not in "\t\n\r"):
            return f"control character U+{ord(ch):04X} in SQL"
    return None


def parse(sql: str, dialect: str) -> ParsedSQL:
    """Parse into one expression per statement (empty statements dropped). Fails closed."""
    try:
        parsed = sqlglot.parse(sql, dialect=dialect, error_level=sqlglot.ErrorLevel.RAISE)
    except (ParseError, TokenError) as exc:
        return ParsedSQL(error=f"could not parse: {str(exc).splitlines()[0][:200]}")
    except SqlglotError as exc:  # pragma: no cover - defensive
        return ParsedSQL(error=f"could not parse: {exc}"[:200])
    except RecursionError:
        return ParsedSQL(error="SQL is nested too deeply to analyse")
    statements = [s for s in parsed if s is not None]
    if not statements:
        return ParsedSQL(error="no SQL statement found")
    return ParsedSQL(statements=statements)


def render(tree: exp.Expression, dialect: str) -> str:
    """Canonical SQL text. Comments are dropped, so what we run is exactly what we analysed."""
    return tree.sql(dialect=dialect, comments=False)


def node_count(tree: exp.Expression) -> int:
    return sum(1 for _ in tree.walk())


# -- structural facts ----------------------------------------------------------------------------
def forbidden_nodes(tree: exp.Expression) -> list[str]:
    """Names of non-read constructs found anywhere in the tree (sorted, unique)."""
    found = {type(n).__name__ for n in tree.walk() if isinstance(n, FORBIDDEN_NODE_TYPES)}
    return sorted(found)


def is_read_query(tree: exp.Expression) -> bool:
    """True for SELECT / set operations / parenthesised queries (a WITH is part of the query node)."""
    return isinstance(tree, exp.Query) and not isinstance(tree, exp.DML)


def function_names(tree: exp.Expression) -> set[str]:
    """Lower-case bare names of every function call, including schema-qualified and in FROM."""
    names: set[str] = set()
    for node in tree.walk():
        if isinstance(node, exp.Anonymous):
            names.add(str(node.name).lower())
        elif isinstance(node, exp.Func):
            names.add(node.sql_name().lower())
    return names


@dataclass(frozen=True)
class TableRef:
    name: str  # normalised: unquoted -> lower-case, quoted -> exact
    schema: str | None
    catalog: str | None

    @property
    def qualified(self) -> str:
        return ".".join(p for p in (self.catalog, self.schema, self.name) if p)


def _norm(identifier: exp.Expression | None) -> str | None:
    if identifier is None or not getattr(identifier, "name", ""):
        return None
    name = identifier.name
    quoted = bool(identifier.args.get("quoted")) if isinstance(identifier, exp.Identifier) else False
    return name if quoted else name.lower()


def physical_tables(tree: exp.Expression) -> list[TableRef]:
    """Tables that actually exist in the database, excluding references to a CTE.

    A CTE reference is only recognised through sqlglot's scope analysis, so ``WITH pg_shadow AS
    (SELECT * FROM pg_shadow) ...`` still reports the inner ``pg_shadow`` as a physical table: in
    PostgreSQL a non-recursive CTE cannot see itself, so that inner name is the real catalog table.
    """
    all_tables = [t for t in tree.find_all(exp.Table) if isinstance(t.this, exp.Identifier)]
    cte_refs: set[int] = set()
    try:
        for scope in traverse_scope(tree):
            for table in scope.tables:
                if table.db or table.catalog:
                    continue
                source = scope.sources.get(table.alias_or_name)
                if isinstance(source, type(scope)) and source is not scope:
                    cte_refs.add(id(table))
    except Exception:  # noqa: BLE001  analysis failed -> treat every name as a real table (fail closed)
        cte_refs = set()
    return [
        TableRef(
            name=_norm(t.this) or "", schema=_norm(t.args.get("db")), catalog=_norm(t.args.get("catalog"))
        )
        for t in all_tables
        if id(t) not in cte_refs
    ]


def subquery_depth(tree: exp.Expression) -> int:
    """Deepest nesting of SELECTs inside SELECTs (root = 0). CTE bodies count as one level, set-operation
    branches add none."""
    deepest = 0
    for node in tree.walk():
        if isinstance(node, exp.Select):
            depth, parent = 0, node.parent
            while parent is not None:
                if isinstance(parent, exp.Select):
                    depth += 1
                parent = parent.parent
            deepest = max(deepest, depth)
    return deepest
