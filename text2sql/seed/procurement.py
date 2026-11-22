"""Deterministic synthetic procurement data (direct materials). Pure functions: no DB access.

Models a purchase-order lifecycle: suppliers and parts, POs and lines, PO changes (pushouts),
ASN shipments, receipts, inventory and RMAs. Every event date is on or before AS_OF so the data is
internally consistent ("today" for the source is AS_OF, set in sources/procurement/source.yaml).
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

DATA_START = date(2025, 1, 1)
AS_OF = date(2026, 9, 30)

TABLE_ORDER = [
    "suppliers",
    "parts",
    "supplier_parts",
    "purchase_orders",
    "po_lines",
    "po_changes",
    "shipments",
    "shipment_lines",
    "receipts",
    "inventory",
    "rmas",
]

# id, name, country, region, tier, payment_terms_days, onboarded_on, is_active
SUPPLIERS = [
    (1, "Apex Memory Systems", "Taiwan", "APAC", "strategic", 60, date(2016, 3, 1), True),
    (2, "Bluefin Storage", "South Korea", "APAC", "strategic", 60, date(2017, 7, 1), True),
    (3, "Cobalt Display Tech", "China", "APAC", "preferred", 45, date(2018, 1, 15), True),
    (4, "Delta Power Cells", "Vietnam", "APAC", "preferred", 45, date(2019, 5, 1), True),
    (5, "Evergreen Circuits", "Malaysia", "APAC", "strategic", 60, date(2015, 9, 1), True),
    (6, "Falcon Metalworks", "Mexico", "AMER", "approved", 30, date(2020, 2, 1), True),
    (7, "Granite Cable Co", "United States", "AMER", "preferred", 45, date(2018, 11, 1), True),
    (8, "Harbor Packaging", "United States", "AMER", "approved", 30, date(2021, 4, 1), True),
    (9, "Iris Optoelectronics", "Germany", "EMEA", "preferred", 45, date(2019, 8, 1), True),
    (10, "Juniper Components", "Poland", "EMEA", "approved", 30, date(2022, 1, 1), True),
    (11, "Kestrel Semiconductors", "Japan", "APAC", "strategic", 75, date(2014, 6, 1), True),
    (12, "Lumen Precision", "India", "APAC", "approved", 30, date(2023, 3, 1), False),
]

# category -> (part name stems, unit cost range, moq choices, lead time range days)
CATEGORY_SPECS = {
    "memory": (["DDR5 16GB module", "DDR5 32GB module", "LPDDR5 8GB package", "DDR4 8GB module"], (18, 60), (500, 1000, 2000), (28, 70)),
    "storage": (["NVMe 512GB SSD", "NVMe 1TB SSD", "SATA 2TB HDD", "eMMC 128GB"], (22, 95), (250, 500, 1000), (21, 56)),
    "display": (["14in FHD panel", "15.6in 4K panel", "13.3in OLED panel", "27in QHD panel"], (45, 180), (100, 250, 500), (35, 84)),
    "battery": (["3-cell 51Wh pack", "4-cell 68Wh pack", "2-cell 38Wh pack", "Coin cell CR2032"], (8, 38), (500, 1000, 2500), (14, 49)),
    "pcb": (["Mainboard rev C", "Daughterboard IO", "Charger board", "Sensor flex PCB"], (6, 55), (200, 500, 1000), (28, 63)),
    "chassis": (["Aluminium top cover", "Magnesium base", "Hinge assembly", "Keyboard bezel"], (3, 22), (500, 1000, 2000), (14, 42)),
    "cable": (["eDP display cable", "Battery connector cable", "USB-C power cable", "Antenna cable"], (0.4, 4), (2000, 5000, 10000), (10, 28)),
    "packaging": (["Retail carton", "Foam insert", "Accessory box", "Shipping sleeve"], (0.2, 2), (5000, 10000, 20000), (7, 21)),
}

BUYERS = ["Priya Rao", "Marcus Lee", "Elena Petrova", "Tom Becker", "Aisha Khan", "Diego Silva"]
CARRIERS = ["DHL", "FedEx", "Maersk Air", "Kuehne+Nagel", "UPS"]
SITES = ["Hyderabad", "Austin", "Penang"]
PUSH_REASONS = ["supplier_capacity", "material_shortage", "logistics", "quality_hold"]


def _money(x: float, places: int = 4) -> Decimal:
    return Decimal(str(round(x, places)))


def _gen_parts(rng: random.Random) -> list[dict]:
    parts, pid = [], 1
    for category, (stems, (lo, hi), moqs, (lt_lo, lt_hi)) in CATEGORY_SPECS.items():
        for stem in stems:
            for variant in ("A", "B"):
                parts.append(
                    {
                        "id": pid,
                        "part_number": f"{category[:3].upper()}-{1000 + pid}",
                        "description": f"{stem} ({variant})",
                        "category": category,
                        "uom": "EA",
                        "standard_cost": _money(rng.uniform(lo, hi)),
                        "moq": rng.choice(moqs),
                        "lead_time_days": rng.randint(lt_lo, lt_hi),
                    }
                )
                pid += 1
    return parts


# which supplier ids can make which part categories
_CAPABLE = {
    "memory": [1, 11, 10],
    "storage": [2, 1, 11],
    "display": [3, 9],
    "battery": [4, 3],
    "pcb": [5, 11, 10],
    "chassis": [6, 12],
    "cable": [7, 5],
    "packaging": [8, 6],
}


def _gen_supplier_parts(rng: random.Random, parts: list[dict]) -> list[dict]:
    rows = []
    for p in parts:
        candidates = list(_CAPABLE[p["category"]])
        rng.shuffle(candidates)
        chosen = candidates[: rng.randint(1, min(3, len(candidates)))]
        for i, sid in enumerate(chosen):
            rows.append(
                {
                    "supplier_id": sid,
                    "part_id": p["id"],
                    "unit_price": _money(float(p["standard_cost"]) * rng.uniform(0.92, 1.12)),
                    "lead_time_days": max(5, p["lead_time_days"] + rng.randint(-7, 14)),
                    "moq": p["moq"] * rng.choice((1, 1, 2)),
                    "is_primary": i == 0,
                }
            )
    return rows


def generate(seed: int = 42) -> dict[str, list[dict]]:
    rng = random.Random(seed)

    suppliers = [
        dict(zip(("id", "name", "country", "region", "tier", "payment_terms_days", "onboarded_on", "is_active"), r))
        for r in SUPPLIERS
    ]
    parts = _gen_parts(rng)
    supplier_parts = _gen_supplier_parts(rng, parts)
    sp_by_part: dict[int, list[dict]] = {}
    for sp in supplier_parts:
        sp_by_part.setdefault(sp["part_id"], []).append(sp)
    active = {s["id"] for s in suppliers if s["is_active"]}

    # Supplier reliability drives pushouts, delays and rejects (so questions about "worst supplier" have signal).
    reliability = {s["id"]: rng.uniform(0.55, 0.97) for s in suppliers}

    pos, lines, changes, shipments, ship_lines, receipts, rmas = [], [], [], [], [], [], []
    line_id = change_id = ship_id = ship_line_id = receipt_id = rma_id = 0

    horizon_days = (AS_OF - DATA_START).days
    for po_id in range(1, 281):
        if rng.random() < 0.3:  # recent orders: a realistic open backlog at the as-of date
            order_date = AS_OF - timedelta(days=rng.randint(10, 90))
        else:
            order_date = DATA_START + timedelta(days=rng.randint(0, horizon_days - 10))
        # pick a part first, then one of its suppliers (active ones only for recent orders)
        base_part = rng.choice(parts)
        options = [sp for sp in sp_by_part[base_part["id"]] if sp["supplier_id"] in active or order_date < date(2025, 9, 1)]
        sup = rng.choice(options or sp_by_part[base_part["id"]])["supplier_id"]
        po_lines_spec = [base_part]
        # extra lines: other parts this same supplier makes
        mine = [sp for sp in supplier_parts if sp["supplier_id"] == sup]
        for sp in rng.sample(mine, k=min(len(mine), rng.randint(0, 3))):
            if sp["part_id"] != base_part["id"]:
                po_lines_spec.append(parts[sp["part_id"] - 1])

        po = {
            "id": po_id,
            "po_number": f"PO-{2025000 + po_id}",
            "supplier_id": sup,
            "buyer": rng.choice(BUYERS),
            "status": "open",
            "order_date": order_date,
            "currency": "USD",
        }
        po_line_rows = []
        for n, part in enumerate(po_lines_spec, 1):
            sp = next(x for x in sp_by_part[part["id"]] if x["supplier_id"] == sup)
            line_id += 1
            qty = sp["moq"] * rng.choice((1, 1, 2, 3, 4))
            requested = order_date + timedelta(days=sp["lead_time_days"])
            promised = requested
            rel = reliability[sup]
            line = {
                "id": line_id,
                "po_id": po_id,
                "line_no": n,
                "part_id": part["id"],
                "quantity": qty,
                "unit_price": sp["unit_price"],
                "requested_date": requested,
                "promised_date": promised,
                "status": "open",
            }

            # --- changes: supplier pushouts (more likely for unreliable suppliers), occasional pull-ins / qty / cancel
            pushed = rng.random() > rel
            for _ in range(rng.choice((1, 1, 2)) if pushed else 0):
                days = rng.choice((5, 7, 10, 14, 21, 28))
                changed_on = min(AS_OF, order_date + timedelta(days=rng.randint(5, max(6, sp["lead_time_days"] - 2))))
                old = line["promised_date"]
                line["promised_date"] = old + timedelta(days=days)
                change_id += 1
                changes.append(
                    {
                        "id": change_id, "po_line_id": line_id, "change_type": "pushout",
                        "old_date": old, "new_date": line["promised_date"], "old_quantity": None, "new_quantity": None,
                        "reason": rng.choice(PUSH_REASONS), "initiated_by": "supplier", "changed_on": changed_on,
                    }
                )
            if not pushed and rng.random() < 0.06:
                days = rng.choice((3, 5, 7))
                old = line["promised_date"]
                line["promised_date"] = max(order_date + timedelta(days=3), old - timedelta(days=days))
                change_id += 1
                changes.append(
                    {
                        "id": change_id, "po_line_id": line_id, "change_type": "pullin",
                        "old_date": old, "new_date": line["promised_date"], "old_quantity": None, "new_quantity": None,
                        "reason": "demand_change", "initiated_by": "buyer",
                        "changed_on": min(AS_OF, order_date + timedelta(days=rng.randint(3, 20))),
                    }
                )
            if rng.random() < 0.08:
                old_q = line["quantity"]
                new_q = max(sp["moq"], int(old_q * rng.choice((0.5, 0.75, 1.25, 1.5))) // 10 * 10)
                if new_q != old_q:
                    line["quantity"] = new_q
                    change_id += 1
                    changes.append(
                        {
                            "id": change_id, "po_line_id": line_id, "change_type": "quantity_change",
                            "old_date": None, "new_date": None, "old_quantity": old_q, "new_quantity": new_q,
                            "reason": "demand_change", "initiated_by": "buyer",
                            "changed_on": min(AS_OF, order_date + timedelta(days=rng.randint(2, 25))),
                        }
                    )
            if rng.random() < 0.03:
                line["status"] = "cancelled"
                change_id += 1
                changes.append(
                    {
                        "id": change_id, "po_line_id": line_id, "change_type": "cancellation",
                        "old_date": None, "new_date": None, "old_quantity": line["quantity"], "new_quantity": 0,
                        "reason": "demand_change", "initiated_by": "buyer",
                        "changed_on": min(AS_OF, order_date + timedelta(days=rng.randint(2, 30))),
                    }
                )

            # --- fulfilment (only if not cancelled): receipts and ASN shipments, never after AS_OF
            if line["status"] != "cancelled":
                slip = 0 if rng.random() < rel else rng.randint(1, 12)  # extra lateness beyond the promise
                arrival = line["promised_date"] + timedelta(days=slip + rng.randint(-2, 2))
                if arrival <= AS_OF:
                    split = rng.random() < 0.12 and line["quantity"] >= 2 * sp["moq"]
                    parts_q = [line["quantity"] // 2, line["quantity"] - line["quantity"] // 2] if split else [line["quantity"]]
                    for k, q in enumerate(parts_q):
                        recv_day = arrival + timedelta(days=7 * k)
                        if recv_day > AS_OF:
                            line["status"] = "partially_received" if k > 0 else "open"
                            break
                        receipt_id += 1
                        rejected = int(q * rng.choice((0, 0, 0, 0.005, 0.01, 0.02, 0.04)) * (1.6 - rel))
                        receipts.append(
                            {"id": receipt_id, "po_line_id": line_id, "received_on": recv_day,
                             "quantity_received": q, "quantity_rejected": rejected}
                        )
                        line["status"] = "received" if k == len(parts_q) - 1 else "partially_received"
                        if rejected and rng.random() < 0.6:
                            rma_id += 1
                            rmas.append(
                                {"id": rma_id, "po_line_id": line_id, "supplier_id": sup, "part_id": part["id"],
                                 "rma_date": min(AS_OF, recv_day + timedelta(days=rng.randint(2, 14))),
                                 "quantity": rejected, "reason": rng.choice(("defective", "defective", "wrong_part", "damaged_in_transit")),
                                 "status": rng.choice(("open", "shipped_back", "credited", "closed"))}
                            )
            po_line_rows.append((line, sp))
            lines.append(line)

        # --- one ASN shipment per PO covering lines that have shipped (received, or in transit near due date)
        shipped = []
        for line, sp in po_line_rows:
            rec = [r for r in receipts if r["po_line_id"] == line["id"]]
            if rec:
                shipped.append((line, sum(r["quantity_received"] for r in rec), min(r["received_on"] for r in rec)))
            elif line["status"] == "open" and line["promised_date"] <= AS_OF + timedelta(days=21) and line["promised_date"] > AS_OF - timedelta(days=45):
                shipped.append((line, line["quantity"], None))
        if shipped:
            ship_id += 1
            first_recv = min((s[2] for s in shipped if s[2]), default=None)
            if first_recv:
                transit = rng.randint(3, 14)
                ship_date, eta = first_recv - timedelta(days=transit), first_recv - timedelta(days=rng.randint(0, 2))
                status = "delivered"
            else:
                earliest = min(s[0]["promised_date"] for s in shipped)
                eta = earliest
                ship_date = min(AS_OF, eta - timedelta(days=rng.randint(4, 14)))
                status = "delayed" if eta < AS_OF else "in_transit"
            shipments.append(
                {"id": ship_id, "po_id": po_id, "asn_number": f"ASN-{700000 + ship_id}",
                 "carrier": rng.choice(CARRIERS), "ship_date": ship_date, "eta_date": eta, "status": status}
            )
            for line, qty, _ in shipped:
                ship_line_id += 1
                ship_lines.append({"id": ship_line_id, "shipment_id": ship_id, "po_line_id": line["id"], "quantity": max(1, qty)})

        # --- header status from its lines
        statuses = {ln["status"] for ln, _ in po_line_rows}
        if statuses == {"cancelled"}:
            po["status"] = "cancelled"
        elif statuses <= {"received", "cancelled"}:
            po["status"] = "closed"
        elif statuses & {"received", "partially_received"}:
            po["status"] = "partially_received"
        pos.append(po)

    inventory, inv_id = [], 0
    for p in parts:
        for site in SITES:
            inv_id += 1
            safety = int(p["moq"] * rng.uniform(0.2, 0.6))
            inventory.append(
                {"id": inv_id, "part_id": p["id"], "site": site,
                 "on_hand": int(safety * rng.uniform(0.3, 3.2)), "safety_stock": safety,
                 "reorder_point": int(safety * 1.5)}
            )

    return {
        "suppliers": suppliers, "parts": parts, "supplier_parts": supplier_parts, "purchase_orders": pos,
        "po_lines": lines, "po_changes": changes, "shipments": shipments, "shipment_lines": ship_lines,
        "receipts": receipts, "inventory": inventory, "rmas": rmas,
    }
