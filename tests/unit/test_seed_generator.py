from datetime import date

from text2sql.seed.wealthlens import DATA_END, DATA_START, TABLE_ORDER, generate


def test_deterministic():
    assert generate(1)["transactions"] == generate(1)["transactions"]
    assert generate(1)["transactions"] != generate(2)["transactions"]


def test_all_tables_populated():
    data = generate()
    for t in TABLE_ORDER:
        assert data[t], t
    assert len(data["transactions"]) > 1000


def test_foreign_keys_and_ranges():
    d = generate()
    acc = {r["id"] for r in d["accounts"]}
    cat = {r["id"] for r in d["categories"]}
    ast = {r["id"] for r in d["assets"]}
    for t in d["transactions"]:
        assert t["account_id"] in acc and t["category_id"] in cat
        assert t["amount"] >= 0 and t["txn_type"] in ("debit", "credit")
        assert DATA_START <= t["txn_date"] <= date(2026, 10, 31)
        assert t["txn_date"] <= DATA_END
    assert {r["asset_id"] for r in d["asset_prices"]} <= ast
    assert all(h["account_id"] in acc and h["asset_id"] in ast for h in d["holdings"])
    assert all(b["category_id"] in cat for b in d["budgets"])
    ids = [t["id"] for t in d["transactions"]]
    assert ids == sorted(set(ids))
