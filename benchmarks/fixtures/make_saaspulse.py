"""Deterministic generator for the SaaSPulse benchmark fixture.

SaaSPulse is a synthetic, never-published B2B-SaaS analytics database. It is the
contamination-proof core of the QueryPilot native benchmark: because the schema
and data never existed on the public web, no model was trained on it, so
"the model memorized Spider" is not a possible objection to results measured here.

The generator is fully deterministic: a single fixed RNG seed drives every
choice, all dates are derived from a fixed epoch (never ``datetime.now()``), rows
are inserted in a fixed order, and the database is ``VACUUM``-ed at the end. Two
runs therefore produce byte-identical ``saaspulse.db`` files.

Regenerate from the repo root:

    python benchmarks/fixtures/make_saaspulse.py

Verify byte-stability:

    python benchmarks/fixtures/make_saaspulse.py --out /tmp/a.db
    python benchmarks/fixtures/make_saaspulse.py --out /tmp/b.db
    shasum -a 256 /tmp/a.db /tmp/b.db   # the two hashes must match
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

# --- Determinism knobs -------------------------------------------------------
# Bump SEED only if you intend to regenerate the fixture *and* re-validate every
# gold query against the new data. The gold SQL is written to be data-agnostic
# (it never hard-codes generated ids/values), so a reseed should not break the
# suite, but always re-run ``benchmarks/validate_golds.py`` afterwards.
SEED = 20260711
EPOCH = date(2023, 1, 1)
# A fixed "as of" date so the dataset has a stable notion of "now" without ever
# calling datetime.now(). Everything happens on or before this date.
AS_OF = date(2025, 6, 30)

# --- Volume knobs (tuned so the file lands in the 2-4 MB target) -------------
N_ACCOUNTS = 420
USAGE_EVENTS_TARGET = 60000

COUNTRIES = [
    ("US", 40),
    ("GB", 14),
    ("DE", 10),
    ("FR", 8),
    ("CA", 8),
    ("AU", 6),
    ("IN", 6),
    ("BR", 4),
    ("JP", 4),
]
INDUSTRIES = [
    "Software",
    "Retail",
    "Finance",
    "Healthcare",
    "Education",
    "Manufacturing",
    "Media",
    "Logistics",
]
SEGMENTS = [("smb", 55), ("midmarket", 30), ("enterprise", 15)]
USER_ROLES = [("admin", 18), ("member", 62), ("viewer", 20)]
PAYMENT_METHODS = [("card", 62), ("ach", 26), ("wire", 12)]
TICKET_PRIORITIES = [("low", 30), ("normal", 42), ("high", 20), ("urgent", 8)]
TICKET_CATEGORIES = [
    ("how_to", 32),
    ("bug", 24),
    ("billing", 18),
    ("feature_request", 16),
    ("outage", 10),
]
EVENT_TYPES = [
    ("api_call", 46),
    ("login", 22),
    ("report_view", 16),
    ("export", 10),
    ("integration_sync", 6),
]
CURRENCY_BY_COUNTRY = {
    "US": "USD",
    "CA": "USD",
    "BR": "USD",
    "IN": "USD",
    "JP": "USD",
    "AU": "USD",
    "GB": "GBP",
    "DE": "EUR",
    "FR": "EUR",
}

# Company-name building blocks (deterministic, purely cosmetic).
NAME_PREFIXES = [
    "North",
    "Blue",
    "Silver",
    "Bright",
    "Iron",
    "Cedar",
    "Summit",
    "Harbor",
    "Vertex",
    "Nimbus",
    "Quartz",
    "Orbit",
    "Delta",
    "Pioneer",
    "Meridian",
    "Aster",
]
NAME_ROOTS = [
    "Grid",
    "Ledger",
    "Works",
    "Labs",
    "Metrics",
    "Cloud",
    "Systems",
    "Analytics",
    "Data",
    "Flow",
    "Logic",
    "Scale",
    "Signal",
    "Forge",
    "Stack",
    "Core",
]
NAME_SUFFIXES = ["Inc", "LLC", "Group", "Co", "Holdings", "Partners"]

# Hand-authored plan catalog. Two $0 plans (Legacy Free, Trial) and one retired
# plan (Legacy Free) provide realistic edge rows. Enterprise has an unlimited
# (NULL) seat limit.
PLANS = [
    # plan_id, name, monthly_price_cents, billing_interval, seat_limit, is_active
    (1, "Starter", 2900, "monthly", 5, 1),
    (2, "Growth", 9900, "monthly", 25, 1),
    (3, "Business", 29900, "monthly", 100, 1),
    (4, "Enterprise", 99900, "annual", None, 1),
    (5, "Legacy Free", 0, "monthly", 3, 0),
    (6, "Trial", 0, "monthly", 10, 1),
]

SCHEMA = """
DROP TABLE IF EXISTS usage_events;
DROP TABLE IF EXISTS support_tickets;
DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS subscriptions;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS accounts;
DROP TABLE IF EXISTS plans;

CREATE TABLE plans (
    plan_id INTEGER PRIMARY KEY,
    plan_name TEXT NOT NULL,
    monthly_price_cents INTEGER NOT NULL,
    billing_interval TEXT NOT NULL,
    seat_limit INTEGER,
    is_active INTEGER NOT NULL
);

CREATE TABLE accounts (
    account_id INTEGER PRIMARY KEY,
    account_name TEXT NOT NULL,
    country TEXT NOT NULL,
    industry TEXT NOT NULL,
    segment TEXT NOT NULL,
    signup_date TEXT NOT NULL,
    churned_date TEXT,
    is_active INTEGER NOT NULL
);

CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    email TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    is_active INTEGER NOT NULL
);

CREATE TABLE subscriptions (
    subscription_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    plan_id INTEGER NOT NULL REFERENCES plans(plan_id),
    status TEXT NOT NULL,
    seats INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    mrr_cents INTEGER NOT NULL
);

CREATE TABLE invoices (
    invoice_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(subscription_id),
    issued_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    currency TEXT NOT NULL
);

CREATE TABLE payments (
    payment_id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(invoice_id),
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    paid_date TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    method TEXT NOT NULL
);

CREATE TABLE support_tickets (
    ticket_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    priority TEXT NOT NULL,
    category TEXT NOT NULL,
    satisfaction_score INTEGER
);

CREATE TABLE usage_events (
    event_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    event_date TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_count INTEGER NOT NULL
);

CREATE INDEX idx_users_account ON users(account_id);
CREATE INDEX idx_subscriptions_account ON subscriptions(account_id);
CREATE INDEX idx_invoices_account ON invoices(account_id);
CREATE INDEX idx_invoices_subscription ON invoices(subscription_id);
CREATE INDEX idx_payments_invoice ON payments(invoice_id);
CREATE INDEX idx_tickets_account ON support_tickets(account_id);
CREATE INDEX idx_usage_account ON usage_events(account_id);
"""


def _weighted(rng: random.Random, choices: list[tuple[str, int]]) -> str:
    population = [value for value, _ in choices]
    weights = [weight for _, weight in choices]
    return rng.choices(population, weights=weights, k=1)[0]


def _iso_date(day_offset: int) -> str:
    return (EPOCH + timedelta(days=day_offset)).isoformat()


def _iso_datetime(day_offset: int, rng: random.Random) -> str:
    moment = datetime(EPOCH.year, EPOCH.month, EPOCH.day) + timedelta(
        days=day_offset,
        hours=rng.randint(0, 23),
        minutes=rng.randint(0, 59),
        seconds=rng.randint(0, 59),
    )
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _month_starts(start: date, end: date) -> list[date]:
    """First-of-month dates from ``start``'s month through ``end``'s month."""
    months: list[date] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(date(year, month, 1))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def _as_of_offset() -> int:
    return (AS_OF - EPOCH).days


def build_rows() -> dict[str, list[tuple]]:
    rng = random.Random(SEED)
    as_of = _as_of_offset()

    accounts: list[tuple] = []
    # Track fields we need downstream, keyed by list position (deterministic).
    account_meta: list[dict] = []
    for account_id in range(1, N_ACCOUNTS + 1):
        name = (
            f"{NAME_PREFIXES[rng.randrange(len(NAME_PREFIXES))]} "
            f"{NAME_ROOTS[rng.randrange(len(NAME_ROOTS))]} "
            f"{NAME_SUFFIXES[rng.randrange(len(NAME_SUFFIXES))]}"
        )
        country = _weighted(rng, COUNTRIES)
        industry = INDUSTRIES[rng.randrange(len(INDUSTRIES))]
        segment = _weighted(rng, SEGMENTS)
        signup_offset = rng.randint(0, as_of - 60)
        churned = rng.random() < 0.18
        churned_offset: int | None = None
        churned_date: str | None = None
        if churned:
            churned_offset = rng.randint(signup_offset + 45, as_of)
            churned_date = _iso_date(churned_offset)
        is_active = 0 if churned else 1
        accounts.append(
            (
                account_id,
                name,
                country,
                industry,
                segment,
                _iso_date(signup_offset),
                churned_date,
                is_active,
            )
        )
        account_meta.append(
            {
                "account_id": account_id,
                "country": country,
                "segment": segment,
                "signup_offset": signup_offset,
                "churned_offset": churned_offset,
                "currency": CURRENCY_BY_COUNTRY.get(country, "USD"),
            }
        )

    users: list[tuple] = []
    users_by_account: list[list[int]] = [[] for _ in range(N_ACCOUNTS + 1)]
    next_user_id = 1
    for meta in account_meta:
        n_users = rng.randint(1, 8)
        for _ in range(n_users):
            user_id = next_user_id
            next_user_id += 1
            role = _weighted(rng, USER_ROLES)
            created_offset = min(
                meta["signup_offset"] + rng.randint(0, 30), as_of
            )
            created_at = _iso_datetime(created_offset, rng)
            if rng.random() < 0.15:
                last_login_at: str | None = None
            else:
                login_offset = rng.randint(created_offset, as_of)
                last_login_at = _iso_datetime(login_offset, rng)
            is_active = 1 if rng.random() < 0.85 else 0
            email = f"user{user_id}@account{meta['account_id']}.example"
            full_name = (
                f"{NAME_PREFIXES[rng.randrange(len(NAME_PREFIXES))]}"
                f" {NAME_ROOTS[rng.randrange(len(NAME_ROOTS))]}"
            )
            users.append(
                (
                    user_id,
                    meta["account_id"],
                    email,
                    full_name,
                    role,
                    created_at,
                    last_login_at,
                    is_active,
                )
            )
            users_by_account[meta["account_id"]].append(user_id)

    plan_by_id = {plan[0]: plan for plan in PLANS}
    paid_plan_ids = [1, 2, 3, 4]
    plan_weights = [40, 30, 18, 12]

    subscriptions: list[tuple] = []
    subscription_meta: list[dict] = []
    next_subscription_id = 1
    for meta in account_meta:
        n_subs = 1 if rng.random() < 0.62 else 2
        for sub_index in range(n_subs):
            subscription_id = next_subscription_id
            next_subscription_id += 1
            # Occasionally a free/trial plan; otherwise a weighted paid plan.
            roll = rng.random()
            if roll < 0.08:
                plan_id = 6  # Trial
            elif roll < 0.13:
                plan_id = 5  # Legacy Free
            else:
                plan_id = rng.choices(paid_plan_ids, weights=plan_weights, k=1)[0]
            plan = plan_by_id[plan_id]
            seat_cap = plan[4] if plan[4] is not None else 250
            seats = rng.randint(1, max(1, seat_cap))
            start_offset = min(
                meta["signup_offset"] + sub_index * rng.randint(90, 210) + rng.randint(0, 20),
                as_of - 15,
            )
            mrr_cents = plan[2] * seats  # $0 for free/trial plans
            if meta["churned_offset"] is not None and sub_index == n_subs - 1:
                status = "cancelled"
                end_offset = max(meta["churned_offset"], start_offset + 20)
                ended_at: str | None = _iso_date(min(end_offset, as_of))
            elif sub_index < n_subs - 1:
                # A superseded earlier subscription: ended before the next began.
                status = "cancelled"
                end_offset = min(start_offset + rng.randint(60, 150), as_of)
                ended_at = _iso_date(end_offset)
            else:
                status = _weighted(
                    rng,
                    [("active", 74), ("trialing", 10), ("past_due", 16)],
                )
                if plan_id == 6:
                    status = "trialing"
                ended_at = None
                end_offset = as_of
            subscriptions.append(
                (
                    subscription_id,
                    meta["account_id"],
                    plan_id,
                    status,
                    seats,
                    _iso_date(start_offset),
                    ended_at,
                    mrr_cents,
                )
            )
            subscription_meta.append(
                {
                    "subscription_id": subscription_id,
                    "account_id": meta["account_id"],
                    "plan_id": plan_id,
                    "start_offset": start_offset,
                    "end_offset": end_offset,
                    "mrr_cents": mrr_cents,
                    "currency": meta["currency"],
                }
            )

    invoices: list[tuple] = []
    invoice_meta: list[dict] = []
    next_invoice_id = 1
    for sub in subscription_meta:
        start_day = EPOCH + timedelta(days=sub["start_offset"])
        end_day = EPOCH + timedelta(days=sub["end_offset"])
        months = _month_starts(start_day, min(end_day, AS_OF))
        for month_index, month_start in enumerate(months):
            invoice_id = next_invoice_id
            next_invoice_id += 1
            issued_offset = (month_start - EPOCH).days
            due_offset = issued_offset + 14
            base_amount = sub["mrr_cents"]
            # ~3% credit / $0 invoices even on paying plans (edge rows).
            if base_amount > 0 and rng.random() < 0.03:
                amount_cents = 0
            else:
                amount_cents = base_amount
            is_recent = month_index >= len(months) - 2
            roll = rng.random()
            if amount_cents == 0:
                status = "paid"
            elif is_recent and roll < 0.45:
                status = "open"
            elif roll < 0.04:
                status = "void"
            elif roll < 0.07:
                status = "uncollectible"
            else:
                status = "paid"
            invoices.append(
                (
                    invoice_id,
                    sub["account_id"],
                    sub["subscription_id"],
                    _iso_date(issued_offset),
                    _iso_date(due_offset),
                    amount_cents,
                    status,
                    sub["currency"],
                )
            )
            invoice_meta.append(
                {
                    "invoice_id": invoice_id,
                    "account_id": sub["account_id"],
                    "due_offset": due_offset,
                    "amount_cents": amount_cents,
                    "status": status,
                }
            )

    payments: list[tuple] = []
    next_payment_id = 1
    for inv in invoice_meta:
        # Only positive, paid invoices settle -> open/void/uncollectible/$0
        # invoices intentionally have no payment row (LEFT JOIN edge cases).
        if inv["status"] != "paid" or inv["amount_cents"] <= 0:
            continue
        payment_id = next_payment_id
        next_payment_id += 1
        paid_offset = min(inv["due_offset"] + rng.randint(-6, 9), _as_of_offset())
        payments.append(
            (
                payment_id,
                inv["invoice_id"],
                inv["account_id"],
                _iso_date(paid_offset),
                inv["amount_cents"],
                _weighted(rng, PAYMENT_METHODS),
            )
        )

    support_tickets: list[tuple] = []
    next_ticket_id = 1
    for meta in account_meta:
        n_tickets = rng.choices([0, 1, 2, 3, 4, 6, 9], weights=[16, 24, 22, 16, 10, 8, 4], k=1)[0]
        for _ in range(n_tickets):
            ticket_id = next_ticket_id
            next_ticket_id += 1
            opened_offset = rng.randint(meta["signup_offset"], as_of)
            opened_at = _iso_datetime(opened_offset, rng)
            is_open = rng.random() < 0.2
            if is_open:
                closed_at: str | None = None
                satisfaction: int | None = None
            else:
                closed_offset = min(opened_offset + rng.randint(0, 20), as_of)
                closed_at = _iso_datetime(closed_offset, rng)
                # ~30% of resolved tickets go unrated (NULL).
                satisfaction = None if rng.random() < 0.30 else rng.randint(1, 5)
            support_tickets.append(
                (
                    ticket_id,
                    meta["account_id"],
                    opened_at,
                    closed_at,
                    _weighted(rng, TICKET_PRIORITIES),
                    _weighted(rng, TICKET_CATEGORIES),
                    satisfaction,
                )
            )

    usage_events: list[tuple] = []
    # Assign each account a deterministic "intensity" so usage is power-law-ish
    # (a few heavy accounts, a long tail of light ones).
    intensities = [rng.random() ** 2 for _ in range(N_ACCOUNTS)]
    total_intensity = sum(intensities) or 1.0
    next_event_id = 1
    for position, meta in enumerate(account_meta):
        account_users = users_by_account[meta["account_id"]]
        if not account_users:
            continue
        share = intensities[position] / total_intensity
        n_events = max(1, int(USAGE_EVENTS_TARGET * share))
        window_start = meta["signup_offset"]
        window_end = meta["churned_offset"] if meta["churned_offset"] is not None else as_of
        if window_end <= window_start:
            window_end = min(window_start + 30, as_of)
        for _ in range(n_events):
            event_id = next_event_id
            next_event_id += 1
            user_id = account_users[rng.randrange(len(account_users))]
            event_offset = rng.randint(window_start, window_end)
            usage_events.append(
                (
                    event_id,
                    meta["account_id"],
                    user_id,
                    _iso_date(event_offset),
                    _weighted(rng, EVENT_TYPES),
                    rng.randint(1, 50),
                )
            )

    return {
        "plans": [tuple(plan) for plan in PLANS],
        "accounts": accounts,
        "users": users,
        "subscriptions": subscriptions,
        "invoices": invoices,
        "payments": payments,
        "support_tickets": support_tickets,
        "usage_events": usage_events,
    }


INSERTS = {
    "plans": "INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?)",
    "accounts": "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "users": "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "subscriptions": "INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "invoices": "INSERT INTO invoices VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "payments": "INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?)",
    "support_tickets": "INSERT INTO support_tickets VALUES (?, ?, ?, ?, ?, ?, ?)",
    "usage_events": "INSERT INTO usage_events VALUES (?, ?, ?, ?, ?, ?)",
}

# Insert order respects foreign-key dependencies.
INSERT_ORDER = [
    "plans",
    "accounts",
    "users",
    "subscriptions",
    "invoices",
    "payments",
    "support_tickets",
    "usage_events",
]


def seed(db_path: Path) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    rows = build_rows()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        for table in INSERT_ORDER:
            conn.executemany(INSERTS[table], rows[table])
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return db_path


def default_db_path() -> Path:
    return Path(__file__).resolve().parent / "saaspulse.db"


def _summarize(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for table in INSERT_ORDER:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<16} {count:>7,} rows")
    finally:
        conn.close()
    size_mb = db_path.stat().st_size / (1024 * 1024)
    print(f"  {'file size':<16} {size_mb:>7.2f} MB")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the SaaSPulse benchmark fixture.")
    parser.add_argument(
        "--out",
        type=Path,
        default=default_db_path(),
        help="Destination .db path (default: benchmarks/fixtures/saaspulse.db).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the row-count / size summary.",
    )
    args = parser.parse_args()

    target = seed(args.out)
    if not args.quiet:
        print(f"Seeded {target}")
        _summarize(target)


if __name__ == "__main__":
    main()
