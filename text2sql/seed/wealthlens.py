"""Deterministic synthetic WealthLens data. Pure functions: no DB access, fully unit-testable."""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

DATA_START = date(2024, 10, 1)
DATA_END = date(2026, 9, 30)

TABLE_ORDER = [
    "accounts",
    "categories",
    "assets",
    "asset_prices",
    "transactions",
    "holdings",
    "budgets",
    "goals",
]

ACCOUNTS = [
    (1, "Demo Savings", "Demo Bank", "bank", "INR", date(2018, 4, 1)),
    (2, "Demo Card A", "Demo Bank", "credit_card", "INR", date(2021, 6, 1)),
    (3, "Demo Card B", "Demo Bank", "credit_card", "INR", date(2023, 2, 1)),
    (4, "Demo Demat", "Demo Broker", "brokerage", "INR", date(2019, 1, 1)),
    (5, "Demo US Brokerage", "Demo Global Broker", "brokerage", "USD", date(2022, 8, 1)),
]

# id, name, kind, parent_id
CATEGORIES = [
    (1, "Salary", "income", None),
    (2, "Interest", "income", None),
    (3, "Dividends", "income", None),
    (4, "Groceries", "expense", None),
    (5, "Dining", "expense", None),
    (6, "Fashion", "expense", None),
    (7, "Transport", "expense", None),
    (8, "Utilities", "expense", None),
    (9, "Rent", "expense", None),
    (10, "Entertainment", "expense", None),
    (11, "Health", "expense", None),
    (12, "Travel", "expense", None),
    (13, "Education", "expense", None),
    (14, "Quick Commerce", "expense", 4),
    (15, "Card Payment", "transfer", None),
    (16, "Investment Transfer", "transfer", None),
]

MERCHANTS = {
    4: ["Fresh Mart", "Green Basket", "Daily Needs"],
    5: ["Cafe Aroma", "Spice Route", "Burger Hub", "Pizza Corner"],
    6: ["Style Studio", "Urban Threads", "Trend Loft"],
    7: ["City Cab", "Metro Card", "Fuel Station"],
    8: ["Power Utility", "Broadband Co", "Mobile Plan"],
    10: ["Stream Plus", "Cinema Max", "Game Store"],
    11: ["Care Clinic", "Wellness Pharmacy"],
    12: ["Sky Airlines", "Stay Inn", "Rail Booking"],
    13: ["Learn Online", "Book Depot"],
    14: ["Quick Basket", "Zip Mart"],
}

# category_id -> (monthly count range, amount low, amount high)
SPEND_PROFILE = {
    4: ((6, 10), 300, 3500),
    5: ((5, 9), 250, 2500),
    6: ((1, 4), 800, 7000),
    7: ((6, 12), 80, 900),
    8: ((3, 3), 400, 2500),
    10: ((2, 4), 150, 1500),
    11: ((0, 2), 300, 6000),
    12: ((0, 1), 3000, 40000),
    13: ((0, 1), 500, 8000),
    14: ((4, 9), 150, 1800),
}

ASSETS = [
    (1, "ALPHA", "Alpha Industries", "stock", "INR", 1800.0),
    (2, "BETA", "Beta Financial", "stock", "INR", 950.0),
    (3, "GAMMA", "Gamma Pharma", "stock", "INR", 1250.0),
    (4, "DELTA", "Delta Tech", "stock", "INR", 3100.0),
    (5, "NIFTYX", "Index Fund India", "etf", "INR", 240.0),
    (6, "FLEXI1", "Flexi Cap Fund", "mutual_fund", "INR", 78.0),
    (7, "SMALL1", "Small Cap Fund", "mutual_fund", "INR", 145.0),
    (8, "GOVB10", "Govt Bond 10Y", "bond", "INR", 1020.0),
    (9, "USMKT", "US Market ETF", "etf", "USD", 480.0),
    (10, "TECHUS", "US Tech ETF", "etf", "USD", 395.0),
    (11, "USDIV", "US Dividend ETF", "etf", "USD", 82.0),
    (12, "WORLD", "World Equity ETF", "etf", "USD", 121.0),
]

GOALS = [
    (1, "Emergency fund", 600000, 420000, date(2027, 6, 30)),
    (2, "Home down payment", 3000000, 900000, date(2029, 12, 31)),
    (3, "Child education", 2500000, 350000, date(2038, 6, 30)),
    (4, "Vacation", 250000, 95000, date(2027, 3, 31)),
]


def _d(x: float) -> Decimal:
    return Decimal(f"{x:.2f}")


def _month_starts() -> list[date]:
    months, cur = [], DATA_START
    while cur <= DATA_END:
        months.append(cur)
        cur = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
    return months


def _month_end(first: date) -> date:
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def generate(seed: int = 42) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    months = _month_starts()

    accounts = [
        dict(id=i, name=n, institution=inst, account_type=t, currency=c, opened_on=o)
        for i, n, inst, t, c, o in ACCOUNTS
    ]
    categories = [dict(id=i, name=n, kind=k, parent_id=p) for i, n, k, p in CATEGORIES]
    assets = [
        dict(id=i, symbol=s, name=n, asset_type=t, currency=c) for i, s, n, t, c, _ in ASSETS
    ]

    # Month-end prices: bounded random walk
    prices = []
    for aid, *_rest, base in ASSETS:
        price = base
        for m in months:
            price = max(1.0, price * (1 + rng.gauss(0.008, 0.045)))
            prices.append(dict(asset_id=aid, price_date=_month_end(m), close=Decimal(f"{price:.4f}")))

    txns: list[dict] = []

    def add(account_id, category_id, when, amount, ttype, merchant, desc, currency="INR"):
        txns.append(
            dict(
                id=len(txns) + 1,
                account_id=account_id,
                category_id=category_id,
                txn_date=when,
                amount=_d(amount),
                currency=currency,
                txn_type=ttype,
                merchant=merchant,
                description=desc,
            )
        )

    salary = 250_000.0
    for idx, m in enumerate(months):
        if idx and idx % 12 == 0:
            salary *= 1.08
        add(1, 1, m, salary, "credit", "Employer Inc", "Monthly salary")
        add(1, 2, m + timedelta(days=14), rng.uniform(400, 1500), "credit", "Demo Bank", "Interest")
        if m.month in (3, 6, 9, 12):
            add(1, 3, m + timedelta(days=19), rng.uniform(2000, 9000), "credit", "Demo Broker", "Dividend")
        add(1, 9, m + timedelta(days=4), 45_000.0, "debit", "Landlord", "Monthly rent")
        add(1, 16, m + timedelta(days=9), 40_000.0, "debit", "Demo Broker", "SIP transfer")

        card_total = {2: 0.0, 3: 0.0}
        for cat, ((lo, hi), amin, amax) in SPEND_PROFILE.items():
            for _ in range(rng.randint(lo, hi)):
                day = rng.randint(1, _month_end(m).day)
                amount = round(rng.uniform(amin, amax), 2)
                acct = rng.choice([1, 2, 2, 3])
                add(acct, cat, m.replace(day=day), amount, "debit", rng.choice(MERCHANTS[cat]), None)
                if acct in card_total:
                    card_total[acct] += amount
        for acct, total in card_total.items():
            if total:
                add(1, 15, m + timedelta(days=24), total, "debit",
                    f"Card {acct} payment", "Credit card bill payment")

    holdings = []
    hid = 1
    for acct, asset_ids in ((4, [1, 2, 3, 4, 5, 6, 7, 8]), (5, [9, 10, 11, 12])):
        for aid in asset_ids:
            base = next(b for i, *_r, b in ASSETS if i == aid)
            holdings.append(
                dict(
                    id=hid,
                    account_id=acct,
                    asset_id=aid,
                    quantity=Decimal(f"{rng.uniform(5, 400):.4f}"),
                    avg_cost=Decimal(f"{base * rng.uniform(0.6, 1.05):.4f}"),
                    as_of=DATA_END,
                )
            )
            hid += 1

    expense_cats = [c for c, _n, k, _p in CATEGORIES if k == "expense"]
    budgets, bid = [], 1
    for cat in expense_cats:
        if cat == 9:
            level = 45_000.0
        elif cat in SPEND_PROFILE:
            (lo, hi), amin, amax = SPEND_PROFILE[cat]
            level = ((lo + hi) / 2) * ((amin + amax) / 2) * 1.1
        else:
            continue
        for m in months:
            budgets.append(dict(id=bid, category_id=cat, month=m, amount=_d(round(level, -1))))
            bid += 1

    goals = [
        dict(id=i, name=n, target_amount=_d(t), current_amount=_d(c), target_date=d)
        for i, n, t, c, d in GOALS
    ]

    return {
        "accounts": accounts,
        "categories": categories,
        "assets": assets,
        "asset_prices": prices,
        "transactions": txns,
        "holdings": holdings,
        "budgets": budgets,
        "goals": goals,
    }
