"""Seed the canonical demo SQLite fixture used by built-in eval suites.

Run from the repo root:

    python tests/fixtures/seed_demo.py

Idempotent: drops and recreates each table on every run, then VACUUMs to
keep the file deterministic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
DROP TABLE IF EXISTS sales_opportunities;
DROP TABLE IF EXISTS churn_risk;
DROP TABLE IF EXISTS usage_events;
DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS subscriptions;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    arr INTEGER NOT NULL,
    revenue INTEGER NOT NULL,
    segment TEXT NOT NULL
);

CREATE TABLE subscriptions (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    plan TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL
);

CREATE TABLE invoices (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    month TEXT NOT NULL,
    revenue INTEGER NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE usage_events (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    event_type TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE churn_risk (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    risk_score REAL NOT NULL,
    flagged_at TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE sales_opportunities (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    stage TEXT NOT NULL,
    amount INTEGER NOT NULL,
    expected_close TEXT NOT NULL
);
"""

CUSTOMERS = [
    (1, "Acme Corp", 120000, 120000, "enterprise"),
    (2, "Globex", 95000, 95000, "enterprise"),
    (3, "Initech", 40000, 40000, "midmarket"),
    (4, "Hooli", 60000, 50000, "midmarket"),
    (5, "Soylent", 18000, 18000, "smb"),
]

SUBSCRIPTIONS = [
    (1, 1, "enterprise", "2024-01-15", None, "active"),
    (2, 2, "enterprise", "2023-09-01", None, "active"),
    (3, 3, "growth", "2025-06-01", None, "active"),
    (4, 4, "growth", "2024-04-01", "2026-03-31", "cancelled"),
    (5, 5, "starter", "2025-11-01", None, "active"),
]

INVOICES = [
    (1, 1, "2026-03", 10000, "paid"),
    (2, 1, "2026-04", 10000, "paid"),
    (3, 2, "2026-03", 8000, "paid"),
    (4, 2, "2026-04", 8000, "paid"),
    (5, 3, "2026-04", 3500, "open"),
    (6, 4, "2026-03", 5000, "paid"),
    (7, 5, "2026-04", 1500, "open"),
]

USAGE_EVENTS = [
    (1, 1, "api_call", 12000, "2026-04-25"),
    (2, 1, "report_view", 320, "2026-04-25"),
    (3, 2, "api_call", 8500, "2026-04-25"),
    (4, 3, "api_call", 1200, "2026-04-25"),
    (5, 4, "api_call", 600, "2026-04-25"),
    (6, 5, "report_view", 40, "2026-04-25"),
]

CHURN_RISK = [
    (1, 4, 0.82, "2026-04-20", "Subscription cancelled"),
    (2, 3, 0.45, "2026-04-22", "Payment delinquent 30 days"),
    (3, 5, 0.30, "2026-04-22", "Low engagement"),
]

SALES_OPPORTUNITIES = [
    (1, 1, "negotiation", 50000, "2026-06-15"),
    (2, 2, "proposal", 30000, "2026-07-01"),
    (3, 3, "discovery", 12000, "2026-09-01"),
]


def seed(db_path: Path) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO customers VALUES (?, ?, ?, ?, ?)", CUSTOMERS
        )
        conn.executemany(
            "INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?)", SUBSCRIPTIONS
        )
        conn.executemany("INSERT INTO invoices VALUES (?, ?, ?, ?, ?)", INVOICES)
        conn.executemany(
            "INSERT INTO usage_events VALUES (?, ?, ?, ?, ?)", USAGE_EVENTS
        )
        conn.executemany(
            "INSERT INTO churn_risk VALUES (?, ?, ?, ?, ?)", CHURN_RISK
        )
        conn.executemany(
            "INSERT INTO sales_opportunities VALUES (?, ?, ?, ?, ?)", SALES_OPPORTUNITIES
        )
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return db_path


def default_db_path() -> Path:
    return Path(__file__).resolve().parent / "demo.db"


if __name__ == "__main__":
    target = default_db_path()
    seed(target)
    print(f"Seeded {target}")
