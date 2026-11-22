from collections import Counter

from text2sql.seed.procurement import AS_OF, DATA_START, TABLE_ORDER, generate


def test_deterministic():
    assert generate(1)["po_lines"] == generate(1)["po_lines"]
    assert generate(1)["po_lines"] != generate(2)["po_lines"]


def test_all_tables_populated():
    data = generate()
    for t in TABLE_ORDER:
        assert data[t], t
    assert len(data["po_lines"]) > 500


def test_foreign_keys():
    d = generate()
    ids = {t: {r["id"] for r in d[t]} for t in TABLE_ORDER if t not in ("supplier_parts",)}
    assert all(r["supplier_id"] in ids["suppliers"] for r in d["purchase_orders"])
    assert all(r["po_id"] in ids["purchase_orders"] and r["part_id"] in ids["parts"] for r in d["po_lines"])
    for t in ("po_changes", "receipts", "shipment_lines", "rmas"):
        assert all(r["po_line_id"] in ids["po_lines"] for r in d[t]), t
    assert all(r["shipment_id"] in ids["shipments"] for r in d["shipment_lines"])
    assert all(r["supplier_id"] in ids["suppliers"] and r["part_id"] in ids["parts"] for r in d["rmas"])
    assert {(r["supplier_id"], r["part_id"]) for r in d["supplier_parts"]}  # composite key present
    assert len({(r["supplier_id"], r["part_id"]) for r in d["supplier_parts"]}) == len(d["supplier_parts"])


def test_no_event_after_the_as_of_date():
    d = generate()
    for table, col in [
        ("purchase_orders", "order_date"),
        ("receipts", "received_on"),
        ("po_changes", "changed_on"),
        ("rmas", "rma_date"),
        ("shipments", "ship_date"),
    ]:
        assert all(DATA_START <= r[col] <= AS_OF for r in d[table]), (table, col)


def test_lifecycle_is_consistent():
    d = generate()
    lines = {r["id"]: r for r in d["po_lines"]}
    received = {r["po_line_id"] for r in d["receipts"]}
    for ln in lines.values():
        if ln["status"] in ("received", "partially_received"):
            assert ln["id"] in received
        if ln["status"] in ("open", "cancelled"):
            assert ln["id"] not in received
    for c in d["po_changes"]:
        if c["change_type"] == "pushout":
            assert c["new_date"] > c["old_date"]
        if c["change_type"] == "pullin":
            assert c["new_date"] < c["old_date"]
    for r in d["receipts"]:
        assert 0 <= r["quantity_rejected"] <= r["quantity_received"]
    for sh in d["shipments"]:
        assert sh["eta_date"] >= sh["ship_date"] - __import__("datetime").timedelta(days=3)


def test_enough_variety_for_demo_questions():
    d = generate()
    assert Counter(r["status"] for r in d["purchase_orders"])["open"] >= 10
    assert Counter(r["change_type"] for r in d["po_changes"])["pushout"] >= 50
    assert {r["status"] for r in d["shipments"]} >= {"delivered", "in_transit"}
    assert any(r["on_hand"] < r["safety_stock"] for r in d["inventory"])
