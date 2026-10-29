"""ContextBuilder end to end, offline: schema fixture, real WealthLens files, lexical embeddings."""

import pytest

from tests.helpers import make_snapshot
from text2sql.generation.context import MAX_QUESTION_CHARS, ContextBuilder
from text2sql.generation.examples import load_examples
from text2sql.schema.glossary import Glossary
from text2sql.schema.provider import SchemaProvider


@pytest.fixture
def builder(wealthlens_source, wealthlens_snapshot, wealthlens_glossary, lexical):
    src = wealthlens_source
    return ContextBuilder(
        source=src,
        schema=SchemaProvider(lambda: wealthlens_snapshot, lambda: wealthlens_snapshot.fingerprint),
        glossary=wealthlens_glossary,
        examples=load_examples(src.base_dir / src.examples, src.today_sql),
        embedder=lexical,
    )


def test_clear_question_gets_a_focused_prompt(builder):
    prepared = builder.prepare("How much did I spend on groceries last month?")
    assert prepared.status == "ready" and prepared.clarification is None
    p = prepared.prompt
    assert {"transactions", "categories"} <= set(p.tables)
    assert len(p.tables) < 8  # not the whole database
    assert "TABLE transactions" in p.user and "## Question" in p.user
    assert "spend:" in p.user  # glossary definition injected because the question uses the term
    assert p.glossary_terms == ["spend"]
    assert p.table_reasons["transactions"] in {"semantic", "glossary"}
    assert p.schema_fingerprint


def test_prompt_only_shows_tables_inside_the_context(builder):
    p = builder.prepare("List all my accounts and their currencies").prompt
    shown = {line.split()[1] for line in p.user.splitlines() if line.startswith("TABLE ")}
    assert shown == set(p.tables)
    for table in set(builder.schema.get().table_names) - shown:
        assert f"TABLE {table}" not in p.user


def test_few_shot_examples_fit_the_visible_schema(builder):
    p = builder.prepare("What are my top 5 expense categories this year?").prompt
    assert 3 <= len(p.examples) <= 4
    visible = set(p.tables)
    by_question = {e.question: e for e in builder.example_selector.examples}
    for q in p.examples:
        assert set(by_question[q].tables) <= visible  # never an example over a hidden table
    for q in p.examples:
        assert q in p.user


def test_relative_dates_use_the_sources_as_of_date(builder):
    p = builder.prepare("What was my spend last month?").prompt
    assert "DATE '2026-09-30'" in p.system
    assert "CURRENT_DATE" not in p.user  # examples were rendered with the demo as-of date


def test_ambiguous_question_stops_before_any_prompt_is_built(builder):
    prepared = builder.prepare("What is my net worth?")
    assert prepared.status == "needs_clarification"
    assert prepared.prompt is None
    options = prepared.clarification.terms[0].options
    assert len(options) == 2 and all(o.example_sql for o in options)


def test_chosen_interpretation_goes_into_the_prompt(builder):
    prepared = builder.prepare("What is my net worth?", clarifications={"net_worth": 0})
    assert prepared.status == "ready"
    assert '"net worth" means: Investments only' in prepared.prompt.user
    assert "- net worth:" not in prepared.prompt.user  # the vague definition is replaced by the user's choice


def test_question_that_states_its_own_interpretation_is_not_interrupted(builder):
    assert builder.prepare("What is my net worth including bank balances?").status == "ready"


def test_sensitive_columns_never_carry_sample_values(builder):
    p = builder.prepare("Which merchants did I spend the most at?").prompt
    for line in p.user.splitlines():
        if line.strip().startswith(("merchant", "description", "name")):
            assert "(e.g." not in line


def test_input_validation(builder):
    with pytest.raises(ValueError, match="empty"):
        builder.prepare("   ")
    with pytest.raises(ValueError, match="too long"):
        builder.prepare("x" * (MAX_QUESTION_CHARS + 1))
    assert (
        builder.prepare("  How   many\naccounts  do I   have? ")
        .prompt.user.rstrip()
        .endswith("How many accounts do I have?")
    )


def test_context_regenerates_after_a_database_change(wealthlens_source, wealthlens_glossary, lexical):
    box = {
        "snap": make_snapshot({"orders": {"cols": ["id", "amount"]}, "suppliers": {"cols": ["id", "name"]}})
    }
    now = {"t": 0.0}
    provider = SchemaProvider(
        lambda: box["snap"], lambda: box["snap"].fingerprint, refresh_s=10, clock=lambda: now["t"]
    )
    b = ContextBuilder(
        source=wealthlens_source, schema=provider, glossary=Glossary.empty(), examples=[], embedder=lexical
    )
    assert "TABLE invoices" not in b.prepare("show invoices").prompt.user

    box["snap"] = make_snapshot(
        {
            "orders": {"cols": ["id", "amount"]},
            "suppliers": {"cols": ["id", "name"]},
            "invoices": {"cols": ["id", "total"]},
        }
    )
    now["t"] = 11  # past the refresh window
    prompt = b.prepare("show invoices").prompt
    assert "TABLE invoices" in prompt.user
    assert b.relevance.rebuilds == 2  # embeddings rebuilt for the new schema
