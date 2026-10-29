"""Schema introspection. SQLite stands in for any SQLAlchemy dialect, so these run offline."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from tests.helpers import source_config
from text2sql.schema import introspect
from text2sql.schema.introspect import build_snapshot, current_fingerprint, mask_value, parse_check_enum


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", poolclass=StaticPool)
    with eng.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "tier TEXT CHECK (tier IN ('gold','silver','bronze')), country TEXT)"
        )
        c.exec_driver_sql(
            "CREATE TABLE purchase_orders (id INTEGER PRIMARY KEY, "
            "supplier_id INTEGER NOT NULL REFERENCES suppliers(id), status TEXT, amount NUMERIC(12,2))"
        )
        c.exec_driver_sql("CREATE TABLE secrets (id INTEGER PRIMARY KEY, token TEXT)")
        c.exec_driver_sql(
            "INSERT INTO suppliers (name, tier, country) VALUES "
            "('Acme-42','gold','DE'),('Bolt Co','silver','IN'),('Cog Ltd','gold','US')"
        )
        c.exec_driver_sql("INSERT INTO secrets (token) VALUES ('s3cr3t-token')")
        for i in range(13):  # 13 distinct statuses: above the default max_distinct of 12
            c.exec_driver_sql(
                f"INSERT INTO purchase_orders (supplier_id, status, amount) VALUES (1, 'st{i}', {i})"
            )
    return eng


def snapshot(engine, **cfg):
    return build_snapshot(source_config(dialect="sqlite", **cfg), engine)


def col(snap, table, column):
    return snap.table(table).column(column)


def test_structure_keys_nullability_and_types(engine):
    snap = snapshot(engine)
    assert snap.table_names == ["purchase_orders", "secrets", "suppliers"]
    assert snap.table("suppliers").primary_key == ["id"]
    assert col(snap, "suppliers", "id").primary_key
    assert col(snap, "suppliers", "name").nullable is False
    assert col(snap, "suppliers", "country").nullable is True
    assert col(snap, "purchase_orders", "amount").type == "numeric(12, 2)"
    fk = snap.table("purchase_orders").foreign_keys[0]
    assert (fk.columns, fk.ref_table, fk.ref_columns) == (["supplier_id"], "suppliers", ["id"])


def test_neighbors_and_subset_drop_dangling_foreign_keys(engine):
    snap = snapshot(engine)
    assert snap.neighbors("purchase_orders", "outgoing") == {"suppliers"}
    assert snap.neighbors("suppliers", "outgoing") == set()
    assert snap.neighbors("suppliers", "incoming") == {"purchase_orders"}
    only_orders = snap.subset(["purchase_orders"])
    assert only_orders.table_names == ["purchase_orders"]
    assert only_orders.table("purchase_orders").foreign_keys == []  # target table is not in the subset


def test_check_constraint_becomes_allowed_values_not_samples(engine):
    snap = snapshot(engine, sample_values={"default": "allow"})
    tier = col(snap, "suppliers", "tier")
    assert tier.allowed_values == ["gold", "silver", "bronze"]
    assert tier.sample_values == []  # no data query needed for a closed value list


def test_default_policy_never_samples_data(engine):
    snap = snapshot(engine)
    assert all(c.sample_values == [] for t in snap.tables for c in t.columns)


def test_allow_list_samples_only_the_named_column(engine):
    snap = snapshot(engine, sample_values={"default": "never", "allow": ["suppliers.country"]})
    assert col(snap, "suppliers", "country").sample_values == ["DE", "IN", "US"]
    assert col(snap, "suppliers", "name").sample_values == []
    assert col(snap, "secrets", "token").sample_values == []


def test_redact_keeps_shape_not_content(engine):
    snap = snapshot(engine, sample_values={"default": "redact"})
    assert col(snap, "suppliers", "name").sample_values == ["Xxxx-99", "Xxxx Xx", "Xxx Xxx"]
    assert "Acme" not in repr(snap.model_dump())


def test_high_cardinality_and_free_text_are_skipped(engine):
    snap = snapshot(engine, sample_values={"default": "allow"})
    assert col(snap, "purchase_orders", "status").sample_values == []  # 13 > max_distinct
    snap = snapshot(engine, sample_values={"default": "allow", "max_value_length": 5})
    assert col(snap, "suppliers", "name").sample_values == []  # values longer than the limit


def test_numeric_columns_are_never_sampled(engine):
    snap = snapshot(engine, sample_values={"default": "allow"})
    assert col(snap, "purchase_orders", "amount").sample_values == []


def test_excluded_tables_are_invisible(engine):
    snap = snapshot(engine, exclude_tables=["secrets"])
    assert "secrets" not in snap.table_names


def test_a_failing_sample_query_does_not_break_introspection(engine, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(introspect, "_fetch_samples", boom)
    snap = snapshot(engine, sample_values={"default": "allow"})
    assert snap.table("suppliers").column("country").sample_values == []


def test_fingerprint_tracks_structure_not_data(engine):
    cfg = source_config(dialect="sqlite", sample_values={"default": "allow"})
    before = build_snapshot(cfg, engine).fingerprint
    assert current_fingerprint(cfg, engine) == before

    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO suppliers (name, tier, country) VALUES ('New','gold','FR')")
    assert build_snapshot(cfg, engine).fingerprint == before  # new data: same structure

    with engine.begin() as c:
        c.exec_driver_sql("ALTER TABLE suppliers ADD COLUMN rating INTEGER")
    assert current_fingerprint(cfg, engine) != before  # DDL: different structure


@pytest.mark.parametrize(
    "sqltext, expected",
    [
        (
            "(account_type = ANY (ARRAY['bank'::text, 'credit_card'::text, 'brokerage'::text]))",
            ("account_type", ["bank", "credit_card", "brokerage"]),
        ),
        (
            "(txn_type = ANY ((ARRAY['debit'::character varying, 'credit'::character varying])::text[]))",
            ("txn_type", ["debit", "credit"]),
        ),
        ("tier IN ('gold', 'silver')", ("tier", ["gold", "silver"])),
        ("\"Status\" IN ('a', 'it''s')", ("Status", ["a", "it's"])),
        ("(amount >= (0)::numeric)", None),
        ("((status = 'a'::text) OR (status = 'b'::text))", None),
        ("(kind IN ('x') AND amount > 0)", None),
    ],
)
def test_parse_check_enum(sqltext, expected):
    assert parse_check_enum(sqltext) == expected


def test_mask_value_keeps_shape():
    assert mask_value("Acme-42") == "Xxxx-99"
    assert mask_value("") == ""
