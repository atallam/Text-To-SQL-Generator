"""Glossary matching and the ambiguity handler (Phase 1 acceptance: revenue / late / top supplier)."""

import pytest
import yaml
from pydantic import ValidationError

from text2sql.generation.ambiguity import detect_ambiguity, resolve_choice
from text2sql.schema.glossary import Glossary

PROCUREMENT = Glossary.model_validate(
    yaml.safe_load(
        """
terms:
  revenue:
    definition: Money received from sales.
    ambiguous: true
    interpretations:
      - label: Gross revenue (before discounts and returns)
        example_sql: SELECT SUM(gross_amount) FROM sales
      - label: Net revenue (after discounts and returns)
        example_sql: SELECT SUM(net_amount) FROM sales
        cues: [net revenue, after discounts]
  late:
    ambiguous: true
    aliases: [delayed, overdue]
    interpretations:
      - label: Delivered after the promised date
        example_sql: SELECT * FROM deliveries WHERE delivered_on > promised_on
      - label: Delivered after the originally requested date
        example_sql: SELECT * FROM deliveries WHERE delivered_on > requested_on
  top_supplier:
    ambiguous: true
    aliases: [best supplier]
    interpretations:
      - label: Highest spend
        example_sql: SELECT supplier_id FROM po GROUP BY 1 ORDER BY SUM(amount) DESC LIMIT 1
      - label: Best on-time delivery rate
        example_sql: SELECT supplier_id FROM deliveries GROUP BY 1 ORDER BY AVG(on_time) DESC LIMIT 1
      - label: Most orders
        example_sql: SELECT supplier_id FROM po GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1
  lead_time:
    definition: Days between po_date and delivered_on.
"""
    )
)


@pytest.mark.parametrize(
    "question, term",
    [
        ("What was revenue last quarter?", "revenue"),
        ("Which suppliers were late in March?", "late"),
        ("How many orders are overdue?", "late"),  # alias
        ("Who is our top supplier?", "top_supplier"),
    ],
)
def test_ambiguous_question_returns_clarification_not_sql(question, term):
    request, resolved = detect_ambiguity(question, PROCUREMENT)
    assert request is not None and resolved == {}
    assert [t.term for t in request.terms] == [term]
    options = request.terms[0].options
    assert len(options) >= 2
    assert all(o.example_sql.strip().upper().startswith("SELECT") for o in options)
    assert [o.index for o in options] == list(range(len(options)))
    assert options[0].label in request.message  # human-readable rendering for the UI


def test_several_ambiguous_terms_are_reported_in_order_of_appearance():
    request, _ = detect_ambiguity("Revenue from our top supplier when deliveries were late", PROCUREMENT)
    assert [t.term for t in request.terms] == ["revenue", "top_supplier", "late"]


def test_unambiguous_questions_pass_through():
    assert detect_ambiguity("What is the lead time for supplier 7?", PROCUREMENT)[0] is None
    assert detect_ambiguity("How many suppliers do we have?", PROCUREMENT)[0] is None


def test_user_choice_resolves_by_index_or_label():
    request, resolved = detect_ambiguity("What was revenue?", PROCUREMENT, {"revenue": 1})
    assert request is None
    assert resolved["revenue"].label.startswith("Net revenue")
    _, by_label = detect_ambiguity(
        "What was revenue?", PROCUREMENT, {"revenue": "gross revenue (before discounts and returns)"}
    )
    assert by_label["revenue"].label.startswith("Gross")


def test_invalid_choice_is_an_error_not_a_guess():
    term = PROCUREMENT.terms["revenue"]
    with pytest.raises(ValueError, match="out of range"):
        resolve_choice(term, 5)
    with pytest.raises(ValueError, match="no interpretation"):
        resolve_choice(term, "made up")


def test_a_cue_in_the_question_resolves_the_term():
    request, resolved = detect_ambiguity("What was net revenue last year?", PROCUREMENT)
    assert request is None
    assert resolved["revenue"].label.startswith("Net revenue")


def test_conflicting_cues_stay_ambiguous():
    glossary = Glossary.model_validate(
        {
            "terms": {
                "size": {
                    "ambiguous": True,
                    "interpretations": [
                        {"label": "Small", "example_sql": "SELECT 1", "cues": ["tiny"]},
                        {"label": "Large", "example_sql": "SELECT 2", "cues": ["huge"]},
                    ],
                }
            }
        }
    )
    assert detect_ambiguity("tiny or huge size", glossary)[0] is not None


def test_ambiguous_terms_must_be_well_formed():
    with pytest.raises(ValidationError, match="at least 2"):
        Glossary.model_validate(
            {
                "terms": {
                    "x": {
                        "ambiguous": True,
                        "interpretations": [{"label": "only one", "example_sql": "SELECT 1"}],
                    }
                }
            }
        )
    with pytest.raises(ValidationError, match="without example_sql"):
        Glossary.model_validate({"terms": {"x": {"ambiguous": True, "interpretations": ["a", "b"]}}})


def test_plural_and_possessive_forms_match():
    names = {
        t.name for t in PROCUREMENT.matches("Which top suppliers had late deliveries and what are revenues?")
    }
    assert names == {"top_supplier", "late", "revenue"}


def test_longer_term_wins_over_the_shorter_one_inside_it(wealthlens_glossary):
    assert [t.name for t in wealthlens_glossary.matches("What was my savings rate in 2025?")] == [
        "savings_rate"
    ]
    assert [t.name for t in wealthlens_glossary.matches("What is my net savings?")] == ["net_savings"]
    assert [t.name for t in wealthlens_glossary.matches("How much have I saved?")] == ["savings"]


def test_tables_follow_references_between_terms(wealthlens_glossary, wealthlens_snapshot):
    names = wealthlens_snapshot.table_names
    # savings_rate = net_savings / income, and neither definition names a table directly.
    assert set(wealthlens_glossary.tables_for(wealthlens_glossary.terms["savings_rate"], names)) == {
        "transactions",
        "categories",
    }
    assert set(wealthlens_glossary.tables_for(wealthlens_glossary.terms["portfolio_value"], names)) >= {
        "holdings",
        "asset_prices",
    }


def test_today_placeholder_is_substituted_on_load(tmp_path):
    f = tmp_path / "g.yaml"
    f.write_text(
        "terms:\n  recent:\n    ambiguous: true\n    interpretations:\n"
        '      - {label: a, example_sql: "SELECT {TODAY}"}\n'
        '      - {label: b, example_sql: "SELECT {TODAY} - 1"}\n',
        encoding="utf-8",
    )
    g = Glossary.load(f, "DATE '2026-09-30'")
    assert g.terms["recent"].interpretations[0].example_sql == "SELECT DATE '2026-09-30'"


def test_wealthlens_glossary_is_valid_and_has_ambiguous_terms(wealthlens_glossary):
    ambiguous = {t.name for t in wealthlens_glossary.terms.values() if t.ambiguous}
    assert {"net_worth", "savings", "returns", "recent"} <= ambiguous
