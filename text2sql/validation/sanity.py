"""Result sanity checks: cheap, deterministic tests of whether the *answer* looks plausible.

A query can be valid, safe and still wrong. These checks catch the typical shapes of "wrong":

    empty_result      nothing came back
    null_heavy        a result column is mostly NULL (usually a bad join or the wrong column)
    count_bounds      a COUNT is negative or larger than the table it counts
    sum_bounds        a SUM over a whole table's column exceeds the table's own total
    date_range        a date filter lies (partly) outside the span of the data
    join_fanout       a many-to-many join feeds an aggregate, or a join multiplied the rows
    duplicate_keys    a primary key (or a whole row) appears more than once

Each check returns pass / warn / fail / skip. ``skip`` means "does not apply to this query" and is left out
of the pass rate. Table statistics come from a ``TableStats`` helper that runs a few fixed, read-only
aggregate queries through the same sandboxed executor and caches them.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlglot import exp

from text2sql.execution.executor import ExecutionError, ExecutionResult, Executor
from text2sql.guardrails import parser
from text2sql.observability.logging import get_logger
from text2sql.schema.model import SchemaSnapshot, Table
from text2sql.validation.config import SanityConfig

log = get_logger("sanity")

Status = Literal["pass", "warn", "fail", "skip"]
_SCORE = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
_DATE_TYPES = ("date", "timestamp")
_NUMERIC_TYPES = ("int", "numeric", "decimal", "double", "real", "float", "money", "serial")


class SanityCheck(BaseModel):
    name: str
    status: Status
    detail: str = ""


class SanityReport(BaseModel):
    checks: list[SanityCheck] = Field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        scored = [_SCORE[c.status] for c in self.checks if c.status != "skip"]
        return round(sum(scored) / len(scored), 4) if scored else 1.0

    @property
    def flagged(self) -> list[SanityCheck]:
        return [c for c in self.checks if c.status in ("warn", "fail")]

    @property
    def failed(self) -> bool:
        return any(c.status == "fail" for c in self.checks)


class TableStats:
    """Row counts, column sums and date spans of the source tables, fetched lazily and cached.

    Every statement is built from schema identifiers (never from user text) and goes through the
    read-only executor. Anything that fails returns ``None`` and the dependent check is skipped."""

    def __init__(self, executor: Executor, schema: SchemaSnapshot):
        self.executor, self.schema = executor, schema
        self._cache: dict[tuple, Any] = {}

    def _scalar(self, key: tuple, select: str, table: str) -> Any:
        if key in self._cache:
            return self._cache[key]
        ident = exp.to_identifier(table, quoted=True).sql(dialect="postgres")
        value = None
        try:
            res = self.executor.run(f"SELECT {select} FROM {ident}")
            value = res.rows[0][0] if res.rows else None
        except ExecutionError as exc:
            log.warning("table_stats_failed", key=key, error=exc.message[:200])
        self._cache[key] = value
        return value

    def row_count(self, table: str) -> int | None:
        v = self._scalar(("rows", table), "COUNT(*)", table)
        return int(v) if v is not None else None

    def column_sum(self, table: str, column: str) -> float | None:
        col = exp.to_identifier(column, quoted=True).sql(dialect="postgres")
        v = self._scalar(("sum", table, column), f"SUM({col})", table)
        return float(v) if v is not None else None

    def date_span(self, table: str, column: str) -> tuple[dt.date, dt.date] | None:
        key = ("span", table, column)
        if key not in self._cache:
            col = exp.to_identifier(column, quoted=True).sql(dialect="postgres")
            ident = exp.to_identifier(table, quoted=True).sql(dialect="postgres")
            span = None
            try:
                res = self.executor.run(f"SELECT MIN({col}), MAX({col}) FROM {ident}")
                lo, hi = res.rows[0] if res.rows else (None, None)
                if lo is not None and hi is not None:
                    span = (_as_date(lo), _as_date(hi))
            except (ExecutionError, ValueError, TypeError) as exc:
                log.warning("table_stats_failed", key=key, error=str(exc)[:200])
            self._cache[key] = span
        return self._cache[key]


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


# -- query structure -------------------------------------------------------------------------------
class _Scope:
    """Who is who in one query: alias -> physical table, plus helpers to resolve a column reference."""

    def __init__(self, tree: exp.Expression, schema: SchemaSnapshot):
        self.schema = schema
        self.aliases: dict[str, str] = {}
        self.tables: list[str] = []
        known = set(schema.table_names)
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            name = t.name.lower() if t.name else ""
            if name in known and name not in ctes:
                self.aliases[(t.alias_or_name or name).lower()] = name
                if name not in self.tables:
                    self.tables.append(name)

    def resolve(self, col: exp.Column) -> tuple[str, str] | None:
        """(table, column) for a column that points at a physical table, else None (CTE, derived...)."""
        name = col.name.lower()
        if col.table:
            table = self.aliases.get(col.table.lower())
            return (table, name) if table and self._has(table, name) else None
        owners = [t for t in self.tables if self._has(t, name)]
        return (owners[0], name) if len(owners) == 1 else None

    def _has(self, table: str, column: str) -> bool:
        try:
            self.schema.table(table).column(column)
            return True
        except KeyError:
            return False

    def table(self, name: str) -> Table:
        return self.schema.table(name)


def _is_numeric(col_type: str) -> bool:
    return any(t in col_type.lower() for t in _NUMERIC_TYPES)


def _is_date(col_type: str) -> bool:
    return any(t in col_type.lower() for t in _DATE_TYPES)


def _projections(tree: exp.Expression) -> list[exp.Expression]:
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    return list(select.expressions) if select else []


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    return None


# -- the checks --------------------------------------------------------------------------------------
class SanityChecker:
    def __init__(self, cfg: SanityConfig, schema: SchemaSnapshot, stats: TableStats | None = None):
        self.cfg, self.schema, self.stats = cfg, schema, stats

    def run(self, sql: str, result: ExecutionResult, dialect: str = "postgres") -> SanityReport:
        parsed = parser.parse(sql, dialect)
        tree = parsed.tree
        checks = [self._empty(result), self._null_heavy(result), self._duplicate_rows(result, tree)]
        if tree is not None:
            scope = _Scope(tree, self.schema)
            checks += [
                self._count_bounds(tree, scope, result),
                self._sum_bounds(tree, scope, result),
                self._date_range(tree, scope),
                self._join_fanout(tree, scope, result),
                self._duplicate_keys(tree, scope, result),
            ]
        return SanityReport(checks=checks)

    # empty result ---------------------------------------------------------------------------------
    def _empty(self, r: ExecutionResult) -> SanityCheck:
        if r.row_count == 0:
            return SanityCheck(
                name="empty_result",
                status="warn",
                detail="The query returned no rows; a filter may be too narrow or a join may drop everything.",  # noqa: E501
            )
        return SanityCheck(name="empty_result", status="pass")

    def _null_heavy(self, r: ExecutionResult) -> SanityCheck:
        if r.row_count < self.cfg.null_min_rows:
            return SanityCheck(name="null_heavy", status="skip", detail="too few rows to judge")
        worst: tuple[str, float] | None = None
        for i, name in enumerate(r.columns):
            ratio = sum(1 for row in r.rows if row[i] is None) / r.row_count
            if ratio > self.cfg.null_heavy_ratio and (worst is None or ratio > worst[1]):
                worst = (name, ratio)
        if worst is None:
            return SanityCheck(name="null_heavy", status="pass")
        status: Status = "fail" if worst[1] >= 0.999 else "warn"
        return SanityCheck(
            name="null_heavy",
            status=status,
            detail=f"column {worst[0]!r} is NULL in {worst[1]:.0%} of rows (often a wrong or unmatched join).",  # noqa: E501
        )

    def _duplicate_rows(self, r: ExecutionResult, tree: exp.Expression | None) -> SanityCheck:
        if r.row_count < 2 or tree is None:
            return SanityCheck(name="duplicate_rows", status="skip")
        has_agg = any(isinstance(n, exp.AggFunc) for n in tree.walk()) or tree.find(exp.Group) is not None
        distinct = tree.find(exp.Distinct) is not None
        if has_agg or distinct:
            return SanityCheck(name="duplicate_rows", status="skip", detail="aggregated or DISTINCT")
        seen = Counter(tuple(map(repr, row)) for row in r.rows)
        dupes = sum(c - 1 for c in seen.values() if c > 1)
        # raw transaction-like data may legitimately repeat; flag only when most of the result repeats
        if dupes / r.row_count > 0.5:
            return SanityCheck(
                name="duplicate_rows",
                status="warn",
                detail=f"{dupes} of {r.row_count} rows are exact duplicates (a join may be repeating rows).",
            )
        return SanityCheck(name="duplicate_rows", status="pass")

    # aggregate plausibility -------------------------------------------------------------------------
    def _count_bounds(self, tree: exp.Expression, scope: _Scope, r: ExecutionResult) -> SanityCheck:
        projs = _projections(tree)
        if any(isinstance(p, exp.Star) for p in projs) or len(projs) != len(r.columns):
            return SanityCheck(name="count_bounds", status="skip")
        has_join = tree.find(exp.Join) is not None
        seen = False
        for i, proj in enumerate(projs):
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if not isinstance(inner, exp.Count):
                continue
            seen = True
            values = [v for v in (_num(row[i]) for row in r.rows) if v is not None]
            if any(v < 0 for v in values):
                return SanityCheck(
                    name="count_bounds", status="fail", detail=f"{r.columns[i]!r} is a COUNT but is negative."
                )
            if not has_join and len(scope.tables) == 1 and self.stats and values:
                total = self.stats.row_count(scope.tables[0])
                if total is not None and max(values) > total:
                    return SanityCheck(
                        name="count_bounds",
                        status="fail",
                        detail=f"COUNT {max(values):g} exceeds the {total} rows in {scope.tables[0]}.",
                    )
        return SanityCheck(name="count_bounds", status="pass" if seen else "skip")

    def _sum_bounds(self, tree: exp.Expression, scope: _Scope, r: ExecutionResult) -> SanityCheck:
        projs = _projections(tree)
        if (
            self.stats is None
            or tree.find(exp.Join) is not None
            or len(scope.tables) != 1
            or len(projs) != len(r.columns)
            or any(isinstance(p, exp.Star) for p in projs)
        ):
            return SanityCheck(name="sum_bounds", status="skip")
        table = scope.tables[0]
        checked = False
        for i, proj in enumerate(projs):
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if not (isinstance(inner, exp.Sum) and isinstance(inner.this, exp.Column)):
                continue
            ref = scope.resolve(inner.this)
            if ref is None:
                continue
            try:
                col = scope.table(ref[0]).column(ref[1])
            except KeyError:
                continue
            total = self.stats.column_sum(ref[0], ref[1])
            if total is None:
                continue
            checked = True
            values = [v for v in (_num(row[i]) for row in r.rows) if v is not None]
            # Only meaningful for a column that is never negative in practice: compare against the total
            # of its absolute values, which is also an upper bound for any subset sum.
            if values and _is_numeric(col.type) and max(abs(v) for v in values) > abs(total) * 1.0001 + 0.01:
                if total >= 0 and all(v >= 0 for v in values):
                    return SanityCheck(
                        name="sum_bounds",
                        status="fail",
                        detail=f"SUM({ref[1]}) = {max(values):,.2f} is larger than the whole {table} column total {total:,.2f}.",  # noqa: E501
                    )
        return SanityCheck(name="sum_bounds", status="pass" if checked else "skip")

    # date range -----------------------------------------------------------------------------------------
    def _date_range(self, tree: exp.Expression, scope: _Scope) -> SanityCheck:
        if self.stats is None:
            return SanityCheck(name="date_range", status="skip")
        slack = dt.timedelta(days=self.cfg.date_slack_days)
        notes: list[tuple[Status, str]] = []
        looked = False
        for node in tree.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ):
            col, lit, flipped = _column_and_date_literal(node)
            if col is None or lit is None:
                continue
            ref = scope.resolve(col)
            if ref is None:
                continue
            try:
                ctype = scope.table(ref[0]).column(ref[1]).type
            except KeyError:
                continue
            if not _is_date(ctype):
                continue
            span = self.stats.date_span(*ref)
            if span is None:
                continue
            looked = True
            lo, hi = span
            op = type(node)
            if flipped:  # literal OP column  ->  column (mirrored OP) literal
                op = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE}.get(op, op)
            label = f"{ref[0]}.{ref[1]} {_OPS.get(op, '?')} {lit.isoformat()} (data spans {lo} to {hi})"
            # Outside the data span = no rows can match. Just outside (within the slack) is plausible when the
            # data lags behind "today", so that only warns; far outside fails. A range that merely starts
            # inside the last weeks of data is a normal "this month" question and is left alone.
            gap = 0
            if op in (exp.GT, exp.GTE) and lit > hi:
                gap = (lit - hi).days
            elif op in (exp.LT, exp.LTE) and lit < lo:
                gap = (lo - lit).days
            elif op is exp.EQ and not (lo <= lit <= hi):
                gap = (lit - hi).days if lit > hi else (lo - lit).days
            if gap > 0:
                where = "after the last date" if (lit > hi) else "before the first date"
                notes.append(
                    ("warn" if gap <= slack.days else "fail", f"filter {label} is {where} in the data")
                )
        if not looked:
            return SanityCheck(name="date_range", status="skip")
        if not notes:
            return SanityCheck(name="date_range", status="pass")
        worst = "fail" if any(s == "fail" for s, _ in notes) else "warn"
        return SanityCheck(name="date_range", status=worst, detail="; ".join(m for _, m in notes))  # type: ignore[arg-type]

    # join fan-out -------------------------------------------------------------------------------------
    def _join_fanout(self, tree: exp.Expression, scope: _Scope, r: ExecutionResult) -> SanityCheck:
        joins = list(tree.find_all(exp.Join))
        if not joins:
            return SanityCheck(name="join_fanout", status="skip", detail="no joins")
        many_to_many: list[str] = []
        for join in joins:
            on = join.args.get("on")
            if on is None:
                continue
            for eq in on.find_all(exp.EQ):
                left, right = eq.left, eq.right
                if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                    continue
                a, b = scope.resolve(left), scope.resolve(right)
                if a is None or b is None or a[0] == b[0]:
                    continue  # CTE/derived side, or a self-join: cannot judge
                if not (_is_key(scope, a) or _is_key(scope, b)):
                    many_to_many.append(f"{a[0]}.{a[1]} = {b[0]}.{b[1]}")
        aggregated = any(
            isinstance(n, (exp.Sum, exp.Avg)) or (isinstance(n, exp.Count) and not n.find(exp.Distinct))
            for n in tree.walk()
        )
        if many_to_many and aggregated:
            return SanityCheck(
                name="join_fanout",
                status="warn",
                detail=f"many-to-many join ({'; '.join(many_to_many)}) feeds an aggregate; rows may be counted more than once.",  # noqa: E501
            )
        if self.stats and not aggregated and tree.find(exp.Group) is None and not r.truncated:
            biggest = max((self.stats.row_count(t) or 0 for t in scope.tables), default=0)
            if biggest and r.row_count > biggest:
                return SanityCheck(
                    name="join_fanout",
                    status="fail",
                    detail=f"{r.row_count} rows came back but the largest table has only {biggest}: a join multiplied the rows.",  # noqa: E501
                )
        if many_to_many:
            return SanityCheck(
                name="join_fanout", status="warn", detail=f"many-to-many join ({'; '.join(many_to_many)})."
            )
        return SanityCheck(name="join_fanout", status="pass")

    # duplicate keys -------------------------------------------------------------------------------------
    def _duplicate_keys(self, tree: exp.Expression, scope: _Scope, r: ExecutionResult) -> SanityCheck:
        projs = _projections(tree)
        if r.row_count < 2 or len(projs) != len(r.columns) or tree.find(exp.Group) is not None:
            return SanityCheck(name="duplicate_keys", status="skip")
        if any(isinstance(n, exp.AggFunc) for n in tree.walk()) or tree.find(exp.Distinct) is not None:
            return SanityCheck(name="duplicate_keys", status="skip")
        checked = False
        for i, proj in enumerate(projs):
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if not isinstance(inner, exp.Column):
                continue
            ref = scope.resolve(inner)
            if ref is None or scope.table(ref[0]).primary_key != [ref[1]]:
                continue
            checked = True
            values = [row[i] for row in r.rows if row[i] is not None]
            dupes = len(values) - len(set(values))
            if dupes:
                return SanityCheck(
                    name="duplicate_keys",
                    status="fail",
                    detail=f"primary key {ref[0]}.{ref[1]} appears {dupes} extra time(s) in the result (a join repeated rows).",  # noqa: E501
                )
        return SanityCheck(name="duplicate_keys", status="pass" if checked else "skip")


_OPS = {exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=", exp.EQ: "="}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _is_key(scope: _Scope, ref: tuple[str, str]) -> bool:
    return scope.table(ref[0]).primary_key == [ref[1]]


def _column_and_date_literal(node: exp.Expression) -> tuple[exp.Column | None, dt.date | None, bool]:
    """(column, date, literal_is_on_the_left) for ``col OP 'YYYY-MM-DD'`` / ``DATE '...'`` / CAST(...)."""
    for col_side, other, flipped in ((node.left, node.right, False), (node.right, node.left, True)):
        if isinstance(col_side, exp.Cast) and isinstance(col_side.this, exp.Column):
            col_side = col_side.this
        if not isinstance(col_side, exp.Column):
            continue
        lits = [n for n in other.walk() if isinstance(n, exp.Literal) and n.is_string]
        if len(lits) != 1 or not _DATE_RE.match(lits[0].name):
            continue
        # only a plain literal / DATE 'x' / CAST('x' AS date): not arithmetic on a column
        if other.find(exp.Column) is not None:
            continue
        try:
            return col_side, dt.date.fromisoformat(lits[0].name[:10]), flipped
        except ValueError:
            continue
    return None, None, False
