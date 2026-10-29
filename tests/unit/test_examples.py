"""Few-shot examples: well-formed, honest about the tables they use, and retrieved sensibly."""

import re

from text2sql.generation.examples import ExampleSelector, FewShotExample, load_examples


def uses(sql: str, table: str) -> bool:
    return bool(re.search(rf"(?<![\w]){re.escape(table)}(?![\w])", sql, re.IGNORECASE))


def wealthlens_examples(src):
    return load_examples(src.base_dir / src.examples, src.today_sql)


def test_enough_examples_and_placeholder_resolved(wealthlens_source):
    examples = wealthlens_examples(wealthlens_source)
    assert len(examples) >= 5  # the prompt shows 3-5 of them
    assert all("{TODAY}" not in e.sql for e in examples)
    assert any("DATE '2026-09-30'" in e.sql for e in examples)


def test_declared_tables_match_the_sql_exactly(wealthlens_source, wealthlens_snapshot):
    """An under-declared example could be shown while one of its tables is hidden from the model."""
    for e in wealthlens_examples(wealthlens_source):
        used = {t for t in wealthlens_snapshot.table_names if uses(e.sql, t)}
        assert set(e.tables) == used, e.question


def test_most_similar_example_ranks_first(wealthlens_source, lexical):
    sel = ExampleSelector(wealthlens_examples(wealthlens_source), lexical)
    top = sel.select("How much did I spend on groceries in the last month?", k=1)[0]
    assert top.question == "How much did I spend on groceries last month?"


def test_selection_respects_visible_tables_and_k(wealthlens_source, lexical):
    sel = ExampleSelector(wealthlens_examples(wealthlens_source), lexical)
    picked = sel.select("savings goals progress", available_tables={"goals"}, k=4)
    assert picked and all(set(e.tables) == {"goals"} for e in picked)
    assert len(sel.select("anything", k=3)) == 3
    assert sel.select("anything", available_tables=set(), k=3) == []


def test_empty_example_set_is_fine(lexical):
    assert ExampleSelector([], lexical).select("anything") == []


def test_examples_need_at_least_one_table():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FewShotExample(question="q", sql="SELECT 1", tables=[])
