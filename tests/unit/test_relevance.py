"""Relevance filter: selection rules (exact, with a stub embedder) and recall on the real seed set."""

import numpy as np
import pytest

from evals.table_recall import load_seed, measure
from tests.helpers import ROOT, StubEmbedder, make_snapshot
from text2sql.config.sources import RelevanceConfig
from text2sql.schema.embeddings import LexicalEmbedder
from text2sql.schema.glossary import Glossary
from text2sql.schema.relevance import RelevanceFilter

# orders -> suppliers -> countries ;  shipments -> orders ;  audits (unrelated island)
CHAIN = make_snapshot(
    {
        "countries": {"cols": ["id", "name"]},
        "suppliers": {"cols": ["id", "country_id"], "fks": {"country_id": "countries"}},
        "orders": {"cols": ["id", "supplier_id"], "fks": {"supplier_id": "suppliers"}},
        "shipments": {"cols": ["id", "order_id"], "fks": {"order_id": "orders"}},
        "audits": {"cols": ["id", "note"]},
    }
)
KEYWORDS = ["countries", "suppliers", "orders", "shipments", "audits"]


def select(question, **cfg):
    flt = RelevanceFilter(StubEmbedder(KEYWORDS), RelevanceConfig(neighbors="none", min_tables=1, **cfg))
    return flt.select(question, CHAIN)


def test_only_tables_near_the_best_score_are_kept():
    assert select("shipments", relative_to_top=0.85).names == ["shipments"]
    # two tables tie for the top score: both stay
    assert set(select("shipments audits", relative_to_top=0.85).names) == {"shipments", "audits"}


def test_min_tables_fills_up_when_nothing_matches():
    flt = RelevanceFilter(StubEmbedder(KEYWORDS), RelevanceConfig(neighbors="none", min_tables=2))
    assert len(flt.select("nothing relevant here", CHAIN).names) == 2


def test_max_tables_caps_the_semantic_selection():
    res = select("countries suppliers orders shipments audits", relative_to_top=0.0, max_tables=3)
    assert len(res.names) == 3


def test_fk_neighbors_outgoing_adds_join_targets_only():
    cfg = RelevanceConfig(neighbors="outgoing", min_tables=1, relative_to_top=0.85)
    res = RelevanceFilter(StubEmbedder(KEYWORDS), cfg).select("orders", CHAIN)
    assert res.names == ["orders", "suppliers"]
    assert {s.name: s.reason for s in res.selections} == {"orders": "semantic", "suppliers": "fk-neighbor"}


def test_fk_neighbors_both_also_adds_tables_that_reference_the_selection():
    cfg = RelevanceConfig(neighbors="both", min_tables=1, relative_to_top=0.85)
    res = RelevanceFilter(StubEmbedder(KEYWORDS), cfg).select("orders", CHAIN)
    assert set(res.names) == {"orders", "suppliers", "shipments"}
    assert "audits" not in res.names and "countries" not in res.names


def test_glossary_term_forces_the_tables_in_its_definition():
    glossary = Glossary.model_validate(
        {"terms": {"lateness": {"definition": "Days between shipments.shipped_on and orders.due_on."}}}
    )
    cfg = RelevanceConfig(neighbors="none", min_tables=1, relative_to_top=0.85)
    res = RelevanceFilter(StubEmbedder(KEYWORDS), cfg, glossary).select("show lateness for audits", CHAIN)
    reasons = {s.name: s.reason for s in res.selections}
    assert reasons["audits"] == "semantic"
    assert reasons["shipments"] == reasons["orders"] == "glossary"

    no_boost = RelevanceConfig(neighbors="none", min_tables=1, glossary_boost=False)
    res = RelevanceFilter(StubEmbedder(KEYWORDS), no_boost, glossary).select(
        "show lateness for audits", CHAIN
    )
    assert res.names == ["audits"]


def test_schema_embeddings_are_rebuilt_only_when_the_fingerprint_changes():
    flt = RelevanceFilter(StubEmbedder(KEYWORDS), RelevanceConfig())
    flt.select("orders", CHAIN)
    flt.select("shipments", CHAIN)
    assert flt.rebuilds == 1  # cached per fingerprint

    changed = make_snapshot(
        {
            **{t.name: {"cols": [c.name for c in t.columns]} for t in CHAIN.tables},
            "invoices": {"cols": ["id"]},
        }
    )
    assert changed.fingerprint != CHAIN.fingerprint
    assert "invoices" in flt.select("invoices", changed).all_scores
    assert flt.rebuilds == 2


def test_selection_is_deterministic_on_ties():
    a = RelevanceFilter(StubEmbedder(KEYWORDS), RelevanceConfig(neighbors="none")).select("zzz", CHAIN).names
    b = RelevanceFilter(StubEmbedder(KEYWORDS), RelevanceConfig(neighbors="none")).select("zzz", CHAIN).names
    assert a == b == ["audits", "countries"]  # alphabetical tie-break


def test_lexical_embedder_is_deterministic_and_normalised():
    e = LexicalEmbedder()
    v1, v2 = e.embed_query("monthly spend by category"), e.embed_query("monthly spend by category")
    assert np.array_equal(v1, v2)
    assert abs(float(np.linalg.norm(v1)) - 1.0) < 1e-5
    assert e.embed_query("categories") @ e.embed_documents(["category"])[0] > 0.5  # plural stemming
    assert not e.embed_query("the of and").any()  # only stopwords -> zero vector, not NaN


def test_table_recall_on_the_wealthlens_seed_set_offline(
    wealthlens_source, wealthlens_snapshot, wealthlens_glossary
):
    """Regression floor for CI. The real gate (>= 95% with bge-small) is the `embeddings` test below."""
    seed = load_seed(ROOT / "evals" / "golden" / "wealthlens_seed.yaml")
    result = measure(
        seed["questions"],
        wealthlens_snapshot,
        LexicalEmbedder(),
        wealthlens_source.relevance,
        wealthlens_glossary,
    )
    assert result.question_recall >= 0.90, result.misses
    assert result.avg_tables < result.total_tables  # the filter actually filters


@pytest.mark.embeddings
def test_table_recall_meets_the_phase1_gate_with_bge_small(
    wealthlens_source, wealthlens_snapshot, wealthlens_glossary
):
    pytest.importorskip("sentence_transformers")
    from text2sql.schema.embeddings import SentenceTransformerEmbedder

    seed = load_seed(ROOT / "evals" / "golden" / "wealthlens_seed.yaml")
    result = measure(
        seed["questions"],
        wealthlens_snapshot,
        SentenceTransformerEmbedder(),
        wealthlens_source.relevance,
        wealthlens_glossary,
    )
    assert result.question_recall >= 0.95, result.misses
