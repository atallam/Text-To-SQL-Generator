"""Prompt snapshot tests (Phase 1 exit gate). A wording change must be a conscious, reviewed diff.

Update after an intended change:  $env:UPDATE_SNAPSHOTS = "1"; pytest tests/unit/test_prompts.py
"""

from datetime import date

import jinja2
import pytest

from tests.helpers import StubEmbedder, assert_snapshot, source_config
from text2sql.generation.context import ContextBuilder
from text2sql.generation.examples import FewShotExample, load_examples
from text2sql.prompts import LATEST_VERSIONS, SQL_GENERATION, _env, render
from text2sql.schema.glossary import Glossary
from text2sql.schema.model import Column, ForeignKey, SchemaSnapshot, Table, compute_fingerprint
from text2sql.schema.provider import SchemaProvider
from text2sql.schema.relevance import RelevanceResult, TableSelection


def procurement_snapshot() -> SchemaSnapshot:
    suppliers = Table(
        name="suppliers",
        comment="One row per approved supplier.",
        primary_key=["id"],
        columns=[
            Column(name="id", type="integer", nullable=False, primary_key=True),
            Column(name="name", type="text", nullable=False),
            Column(name="tier", type="text", nullable=False, allowed_values=["gold", "silver", "bronze"]),
            Column(
                name="country", type="char(2)", sample_values=["DE", "IN", "US"], comment="ISO country code"
            ),
        ],
    )
    orders = Table(
        name="purchase_orders",
        comment="Purchase orders placed with suppliers.",
        primary_key=["id"],
        columns=[
            Column(name="id", type="integer", nullable=False, primary_key=True),
            Column(name="supplier_id", type="integer", nullable=False),
            Column(name="amount", type="numeric(12, 2)", nullable=False, comment="Order value in USD"),
            Column(name="promised_on", type="date"),
        ],
        foreign_keys=[ForeignKey(columns=["supplier_id"], ref_table="suppliers", ref_columns=["id"])],
    )
    tables = [orders, suppliers]
    return SchemaSnapshot(
        source="acme",
        dialect="postgresql",
        schema_name="public",
        tables=tables,
        fingerprint=compute_fingerprint(tables),
    )


class FixedRelevance:
    """Pins table selection so the snapshot does not depend on embedding scores."""

    def __init__(self, names):
        self.names = names

    def select(self, question, snapshot):
        return RelevanceResult(selections=[TableSelection(n, 1.0, "semantic") for n in self.names])


def build(snapshot, glossary, examples, cfg, embedder, tables):
    builder = ContextBuilder(
        source=cfg,
        schema=SchemaProvider(lambda: snapshot, lambda: snapshot.fingerprint),
        glossary=glossary,
        examples=examples,
        embedder=embedder,
    )
    builder.relevance = FixedRelevance(tables)
    return builder


def test_procurement_prompt_snapshot():
    glossary = Glossary.model_validate(
        {
            "terms": {
                "late": {
                    "definition": "delivered_on is after purchase_orders.promised_on.",
                    "notes": "Ignore orders without a promised date.",
                },
                "revenue": {
                    "ambiguous": True,
                    "interpretations": [
                        {"label": "Gross revenue", "example_sql": "SELECT 1"},
                        {"label": "Net revenue", "example_sql": "SELECT 2", "cues": ["net revenue"]},
                    ],
                },
            },
            "conventions": ["Amounts are in USD.", "A supplier is gold, silver or bronze tier."],
        }
    )
    examples = [
        FewShotExample(
            question="How many gold suppliers are there?",
            tables=["suppliers"],
            sql="SELECT COUNT(*) FROM suppliers WHERE tier = 'gold'",
        ),
        FewShotExample(
            question="Total order value per supplier",
            tables=["purchase_orders", "suppliers"],
            sql=(
                "SELECT s.name, SUM(o.amount)\nFROM purchase_orders o\n"
                "JOIN suppliers s ON s.id = o.supplier_id\nGROUP BY s.name"
            ),
        ),
    ]
    cfg = source_config(name="acme", display_name="Acme Procurement", as_of_date=date(2026, 9, 30))
    builder = build(
        procurement_snapshot(),
        glossary,
        examples,
        cfg,
        StubEmbedder(["supplier", "order", "gold"]),
        ["purchase_orders", "suppliers"],
    )
    prepared = builder.prepare("Which late orders are there per supplier, using net revenue?")
    assert prepared.status == "ready"
    assert prepared.prompt.version == LATEST_VERSIONS[SQL_GENERATION]
    assert_snapshot("sql_generation_v1_procurement_system", prepared.prompt.system)
    assert_snapshot("sql_generation_v1_procurement_user", prepared.prompt.user)


def test_wealthlens_prompt_snapshot(wealthlens_source, wealthlens_snapshot, wealthlens_glossary, lexical):
    src = wealthlens_source
    examples = load_examples(src.base_dir / src.examples, src.today_sql)
    builder = build(
        wealthlens_snapshot,
        wealthlens_glossary,
        examples,
        src,
        lexical,
        ["transactions", "categories", "accounts"],
    )
    prepared = builder.prepare("How much did I spend on groceries last month?")
    assert prepared.status == "ready"
    assert_snapshot("sql_generation_v1_wealthlens_system", prepared.prompt.system)
    assert_snapshot("sql_generation_v1_wealthlens_user", prepared.prompt.user)


def test_every_latest_version_has_both_template_parts():
    for name, version in LATEST_VERSIONS.items():
        for part in ("system", "user"):
            assert _env().get_template(f"{name}.{version}.{part}.j2") is not None


def test_missing_template_variable_is_an_error_not_a_blank():
    with pytest.raises(jinja2.UndefinedError):
        render(SQL_GENERATION, "v1", "user", schema_text="x")  # question etc. missing


def test_prompts_never_contain_unrendered_template_syntax(
    wealthlens_source, wealthlens_snapshot, wealthlens_glossary, lexical
):
    builder = ContextBuilder(
        source=wealthlens_source,
        schema=SchemaProvider(lambda: wealthlens_snapshot, lambda: wealthlens_snapshot.fingerprint),
        glossary=wealthlens_glossary,
        examples=[],
        embedder=lexical,
    )
    prompt = builder.prepare("List all my accounts").prompt
    for text in (prompt.system, prompt.user):
        assert "{{" not in text and "{%" not in text and "{TODAY}" not in text
