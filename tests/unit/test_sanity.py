"""Result sanity checks."""

import datetime as dt

import pytest

from tests.helpers import FakeExecutor, exec_result, typed_snapshot
from text2sql.validation.config import SanityConfig
from text2sql.validation.sanity import SanityChecker, TableStats

SCHEMA = typed_snapshot(
    {
        "categories": {"cols": {"id": "integer", "name": "text"}},
        "transactions": {
            "cols": {
                "id": "integer",
                "category_id": "integer",
                "amount": "numeric(14,2)",
                "txn_date": "date",
                "memo": "text",
            },
            "fks": {"category_id": "categories"},
        },
        "asset_prices": {"cols": {"id": "integer", "asset_id": "integer", "close": "numeric"}},
        "holdings": {"cols": {"id": "integer", "asset_id": "integer", "quantity": "numeric"}},
    }
)
D = dt.date


def stats():
    ex = FakeExecutor(
        {
            'COUNT(*) FROM "transactions"': (["c"], [[100]]),
            'COUNT(*) FROM "holdings"': (["c"], [[10]]),
            'COUNT(*) FROM "asset_prices"': (["c"], [[500]]),
            'SUM("amount") FROM "transactions"': (["s"], [[5000.0]]),
            'MIN("txn_date")': (["a", "b"], [[D(2025, 1, 1), D(2026, 9, 30)]]),
        }
    )
    return TableStats(ex, SCHEMA)


def run(sql, columns, rows, *, with_stats=True, truncated=False):
    checker = SanityChecker(SanityConfig(), SCHEMA, stats() if with_stats else None)
    report = checker.run(sql, exec_result(columns, rows, truncated=truncated), "postgres")
    return {c.name: c for c in report.checks}, report


def test_clean_aggregate_passes_everything_applicable():
    checks, report = run("SELECT SUM(amount) AS s FROM transactions WHERE amount > 0", ["s"], [[1200.5]])
    assert report.pass_rate == 1.0 and not report.flagged
    assert checks["sum_bounds"].status == "pass"


def test_empty_result_warns():
    checks, report = run("SELECT id FROM transactions WHERE amount > 1e9", ["id"], [])
    assert checks["empty_result"].status == "warn" and report.pass_rate < 1.0


def test_null_heavy_column():
    rows = [[i, None] for i in range(8)] + [[9, "x"]]
    checks, _ = run("SELECT id, memo FROM transactions", ["id", "memo"], rows)
    assert checks["null_heavy"].status == "warn" and "memo" in checks["null_heavy"].detail


def test_all_null_column_fails_and_tiny_results_are_skipped():
    checks, _ = run("SELECT id, memo FROM transactions", ["id", "memo"], [[i, None] for i in range(6)])
    assert checks["null_heavy"].status == "fail"
    checks, _ = run("SELECT id, memo FROM transactions", ["id", "memo"], [[1, None], [2, None]])
    assert checks["null_heavy"].status == "skip"


def test_count_larger_than_table_fails():
    checks, _ = run("SELECT COUNT(*) AS n FROM transactions", ["n"], [[250]])
    assert checks["count_bounds"].status == "fail" and "100" in checks["count_bounds"].detail


def test_negative_count_fails_and_normal_count_passes():
    assert run("SELECT COUNT(*) AS n FROM transactions", ["n"], [[-3]])[0]["count_bounds"].status == "fail"
    assert run("SELECT COUNT(*) AS n FROM transactions", ["n"], [[40]])[0]["count_bounds"].status == "pass"


def test_sum_above_whole_table_total_fails():
    checks, _ = run("SELECT SUM(amount) AS s FROM transactions", ["s"], [[99999.0]])
    assert checks["sum_bounds"].status == "fail"


def test_sum_check_skipped_when_a_join_is_present():
    sql = "SELECT SUM(t.amount) AS s FROM transactions t JOIN categories c ON c.id = t.category_id"
    assert run(sql, ["s"], [[99999.0]])[0]["sum_bounds"].status == "skip"


@pytest.mark.parametrize(
    "where,expected",
    [
        ("txn_date >= '2026-01-01'", "pass"),
        ("txn_date >= '2027-03-01'", "fail"),  # starts after the last date in the data
        ("txn_date < '2024-01-01'", "fail"),  # ends before the first date
        ("txn_date >= '2026-09-25'", "pass"),  # a normal "this month" range near the end of the data
        ("txn_date >= '2026-10-05'", "warn"),  # just past the data: it may simply lag behind today
        ("txn_date = '2031-01-01'", "fail"),
        ("txn_date >= DATE '2026-01-01' AND txn_date < DATE '2026-02-01'", "pass"),
    ],
)
def test_date_range_against_the_data_span(where, expected):
    checks, _ = run(f"SELECT id FROM transactions WHERE {where}", ["id"], [[1]])
    assert checks["date_range"].status == expected


def test_date_range_ignores_relative_expressions_and_unknown_stats():
    sql = "SELECT id FROM transactions WHERE txn_date >= date_trunc('month', CURRENT_DATE)"
    assert run(sql, ["id"], [[1]])[0]["date_range"].status == "skip"
    sql = "SELECT id FROM transactions WHERE txn_date >= '2030-01-01'"
    assert run(sql, ["id"], [[1]], with_stats=False)[0]["date_range"].status == "skip"


def test_reversed_comparison_is_understood():
    checks, _ = run("SELECT id FROM transactions WHERE '2030-01-01' <= txn_date", ["id"], [[1]])
    assert checks["date_range"].status == "fail"


def test_many_to_many_join_into_aggregate_warns():
    sql = (
        "SELECT SUM(h.quantity * p.close) AS v FROM holdings h JOIN asset_prices p ON p.asset_id = h.asset_id"
    )
    checks, _ = run(sql, ["v"], [[1.0]])
    assert checks["join_fanout"].status == "warn" and "many-to-many" in checks["join_fanout"].detail


def test_key_join_is_fine_and_cte_side_is_not_judged():
    ok = "SELECT SUM(t.amount) AS s FROM transactions t JOIN categories c ON c.id = t.category_id"
    assert run(ok, ["s"], [[1.0]])[0]["join_fanout"].status == "pass"
    cte = (
        "WITH latest AS (SELECT DISTINCT ON (asset_id) asset_id, close FROM asset_prices ORDER BY asset_id, id DESC) "  # noqa: E501
        "SELECT SUM(h.quantity * l.close) AS v FROM holdings h JOIN latest l ON l.asset_id = h.asset_id"
    )
    assert run(cte, ["v"], [[1.0]])[0]["join_fanout"].status == "pass"


def test_join_that_multiplies_rows_fails():
    sql = "SELECT h.id, p.close FROM holdings h JOIN asset_prices p ON p.asset_id = h.asset_id"
    rows = [[i, 1.0] for i in range(900)]  # more rows than the biggest table (500)
    checks, _ = run(sql, ["id", "close"], rows)
    assert checks["join_fanout"].status == "fail"


def test_no_join_means_skip():
    assert run("SELECT id FROM transactions", ["id"], [[1]])[0]["join_fanout"].status == "skip"


def test_duplicate_primary_key_in_result_fails():
    sql = "SELECT c.id, t.amount FROM categories c JOIN transactions t ON t.category_id = c.id"
    checks, _ = run(sql, ["id", "amount"], [[1, 5], [1, 6], [2, 7]])
    assert checks["duplicate_keys"].status == "fail"
    checks, _ = run(sql, ["id", "amount"], [[1, 5], [2, 6]])
    assert checks["duplicate_keys"].status == "pass"


def test_duplicate_keys_skipped_for_aggregates():
    sql = "SELECT c.id, SUM(t.amount) FROM categories c JOIN transactions t ON t.category_id = c.id GROUP BY c.id"  # noqa: E501
    assert run(sql, ["id", "sum"], [[1, 5], [1, 6]])[0]["duplicate_keys"].status == "skip"


def test_duplicate_rows_flagged_only_when_most_rows_repeat():
    sql = "SELECT memo FROM transactions"
    assert run(sql, ["memo"], [["a"]] * 6)[0]["duplicate_rows"].status == "warn"
    assert run(sql, ["memo"], [["a"], ["b"], ["c"], ["a"]])[0]["duplicate_rows"].status == "pass"


def test_pass_rate_counts_warn_as_half_and_skips_are_excluded():
    _, report = run("SELECT id FROM transactions WHERE amount > 1e9", ["id"], [])
    scored = [c for c in report.checks if c.status != "skip"]
    expected = sum({"pass": 1, "warn": 0.5, "fail": 0}[c.status] for c in scored) / len(scored)
    assert report.pass_rate == pytest.approx(expected, abs=1e-3)


def test_stats_failure_degrades_to_skip_not_error():
    checker = SanityChecker(SanityConfig(), SCHEMA, TableStats(FakeExecutor({}), SCHEMA))
    report = checker.run(
        "SELECT COUNT(*) AS n FROM transactions WHERE txn_date >= '2030-01-01'",
        exec_result(["n"], [[3]]),
        "postgres",
    )
    by = {c.name: c.status for c in report.checks}
    assert by["date_range"] == "skip" and by["count_bounds"] == "pass"


def test_stats_are_cached():
    ex = FakeExecutor({'COUNT(*) FROM "transactions"': (["c"], [[100]])})
    s = TableStats(ex, SCHEMA)
    assert s.row_count("transactions") == s.row_count("transactions") == 100
    assert len(ex.sqls) == 1
