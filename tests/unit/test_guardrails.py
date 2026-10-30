"""Guardrail rules in isolation (the red-team suite covers adversarial breadth)."""

import pytest
from pydantic import ValidationError

from tests.helpers import make_snapshot
from text2sql.guardrails import GuardrailEngine, check_sql, load_guardrail_config
from text2sql.guardrails.config import GuardrailConfig
from text2sql.guardrails.parser import physical_tables, subquery_depth

SNAP = make_snapshot({"orders": {"cols": ["id", "supplier_id"]}, "suppliers": {"cols": ["id", "name"]}})


def engine(**overrides) -> GuardrailEngine:
    return GuardrailEngine(load_guardrail_config(overrides=overrides))


def check(sql, snap=SNAP, **overrides):
    return engine(**overrides).check(sql, snap)


# -- rewriting -----------------------------------------------------------------------------------
def test_limit_is_injected_when_missing_and_reported():
    d = check("SELECT * FROM orders")
    assert d.allowed and d.sql == "SELECT * FROM orders LIMIT 1000"
    assert d.row_limit_applied == 1000 and any("G006" in w for w in d.warnings)


def test_limit_is_lowered_but_a_smaller_one_is_kept():
    assert check("SELECT * FROM orders LIMIT 50000").sql.endswith("LIMIT 1000")
    d = check("SELECT * FROM orders LIMIT 10")
    assert d.sql.endswith("LIMIT 10") and d.row_limit_applied is None and not d.warnings


@pytest.mark.parametrize(
    "tail", ["LIMIT ALL", "FETCH FIRST 50 PERCENT ROWS ONLY", "FETCH FIRST 5000 ROWS ONLY"]
)
def test_unbounded_limit_forms_are_replaced(tail):
    assert check(f"SELECT * FROM orders {tail}").sql.endswith("LIMIT 1000")


def test_limit_on_a_set_operation_applies_to_the_whole_result():
    d = check("SELECT id FROM orders UNION SELECT id FROM suppliers")
    assert d.sql.endswith("UNION SELECT id FROM suppliers LIMIT 1000")


def test_reject_mode_blocks_instead_of_rewriting():
    d = check("SELECT * FROM orders", row_limit={"mode": "reject"})
    assert d.blocked and d.rule_ids == ["G006"] and d.repairable


def test_executed_sql_is_regenerated_so_comments_never_reach_the_database():
    d = check("SELECT /* x */ id -- DROP TABLE orders\nFROM orders")
    assert d.allowed and "DROP" not in d.sql and "--" not in d.sql and "/*" not in d.sql


# -- tables --------------------------------------------------------------------------------------
def test_table_names_are_case_normalised_like_postgres():
    assert check("SELECT * FROM ORDERS").allowed  # unquoted -> lower-case
    assert check('SELECT * FROM "ORDERS"').blocked  # quoted keeps its case: a different (missing) table


def test_cte_names_are_not_tables_but_a_cte_cannot_hide_a_real_one():
    ok = check("WITH recent AS (SELECT * FROM orders) SELECT * FROM recent")
    assert ok.allowed and ok.tables == ["orders"]
    sneaky = check("WITH pg_shadow AS (SELECT * FROM pg_shadow) SELECT * FROM pg_shadow")
    assert sneaky.blocked and sneaky.rule_ids == ["G005"]


def test_unknown_table_is_repairable_and_lists_what_exists():
    d = check("SELECT * FROM users")
    assert d.blocked and d.repairable and "orders" in d.reason


def test_catalog_access_is_final_not_repairable():
    d = check("SELECT * FROM pg_catalog.pg_tables")
    assert d.blocked and not d.repairable


def test_catalog_can_be_enabled_per_source():
    d = check("SELECT * FROM information_schema.tables", tables={"allow_catalog": True})
    assert d.allowed


def test_no_schema_means_no_table_check():
    d = GuardrailEngine(GuardrailConfig()).check("SELECT * FROM anything", None)
    assert d.allowed


def test_physical_tables_reports_qualified_names():
    import sqlglot

    refs = physical_tables(
        sqlglot.parse_one("SELECT * FROM public.orders o JOIN suppliers s ON 1=1", dialect="postgres")
    )
    assert sorted(r.qualified for r in refs) == ["public.orders", "suppliers"]


# -- functions -----------------------------------------------------------------------------------
def test_function_deny_list_matches_patterns_and_respects_allow_exceptions():
    assert check("SELECT lo_unlink(1)").rule_ids == ["G004"]  # lo_* pattern
    cfg = {"functions": {"deny": ["pg_*"], "allow": ["pg_typeof"]}}
    assert check("SELECT pg_typeof(1)", **cfg).allowed
    assert check("SELECT pg_sleep(1)", **cfg).blocked


def test_ordinary_functions_are_fine():
    assert check("SELECT count(*), sum(id), date_trunc('month', now()), coalesce(id, 0) FROM orders").allowed


# -- depth ---------------------------------------------------------------------------------------
def test_depth_counts_nested_selects_but_not_set_operation_branches():
    import sqlglot

    def depth(sql):
        return subquery_depth(sqlglot.parse_one(sql, dialect="postgres"))

    assert depth("SELECT 1") == 0
    assert depth("SELECT 1 UNION SELECT 2 UNION SELECT 3") == 0
    assert depth("SELECT * FROM (SELECT * FROM (SELECT 1) a) b") == 2
    assert depth("WITH a AS (SELECT 1), b AS (SELECT * FROM a) SELECT * FROM b") == 1


def test_depth_limit_is_configurable():
    sql = "SELECT * FROM (SELECT * FROM (SELECT * FROM orders) a) b"
    assert check(sql).allowed
    assert check(sql, subquery_depth={"max_depth": 1}).rule_ids == ["G007"]


# -- config --------------------------------------------------------------------------------------
def test_safety_core_rules_cannot_be_weakened():
    for rule in ("single_statement", "statement_allowlist", "forbidden_constructs", "parse"):
        with pytest.raises(ValidationError):
            GuardrailConfig.model_validate({rule: {"mode": "off"}})
        with pytest.raises(ValidationError):
            GuardrailConfig.model_validate({rule: {"mode": "warn"}})


def test_a_warn_mode_rule_reports_without_blocking():
    d = check("SELECT * FROM users", tables={"mode": "warn"})
    assert d.allowed and any("G005" in w for w in d.warnings)


def test_oversized_and_control_character_input_is_refused_before_parsing():
    assert check("SELECT 1 /*" + "x" * 30_000 + "*/").rule_ids == ["G000"]
    assert check("SELECT 1\x07").rule_ids == ["G000"]
    assert check("   ").rule_ids == ["G000"]


def test_deeply_nested_input_fails_closed_instead_of_crashing():
    sql = "SELECT " + "(" * 3000 + "1" + ")" * 3000
    assert check(sql).blocked


def test_source_config_overrides_defaults(wealthlens_source):
    cfg = load_guardrail_config(wealthlens_source)
    assert cfg.row_limit.max_rows == wealthlens_source.max_rows
    assert cfg.tables.allowed_schemas == ["public"] and cfg.dialect == "postgres"


def test_per_source_guardrail_file_is_deep_merged(tmp_path):
    from text2sql.config.sources import SourceConfig

    (tmp_path / "g.yaml").write_text("subquery_depth:\n  max_depth: 1\nfunctions:\n  deny: ['x_*']\n")
    src = SourceConfig.model_validate(
        dict(
            name="t",
            display_name="t",
            dialect="postgresql",
            connection=dict(readonly_url_env="X"),
            guardrails="g.yaml",
            base_dir=tmp_path,
        )
    )
    cfg = load_guardrail_config(src)
    assert cfg.subquery_depth.max_depth == 1 and cfg.functions.deny == ["x_*"]
    assert cfg.explain.max_total_cost == 1_000_000  # untouched defaults survive


def test_eval_hook_blocks_without_a_schema():
    assert check_sql("DROP TABLE x").blocked and check_sql("SELECT 1").allowed
