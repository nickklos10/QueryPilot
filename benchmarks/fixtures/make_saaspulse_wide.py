"""Build SaaSPulse-Wide: the SaaSPulse fixture plus hundreds of unrelated tables.

Real warehouses have hundreds of tables, and the full schema stops fitting
comfortably in a prompt. This copies ``saaspulse.db`` and adds deterministic
distractor tables from other business domains whose column names overlap with
the real ones (``account_id``, ``status``, ``amount_cents``, ``created_at``...).
The gold SQL in the SaaSPulse suites is unchanged, so any suite can be pointed at
the wide fixture to measure large-schema behaviour.

    python benchmarks/fixtures/make_saaspulse_wide.py [--tables 300] [--output PATH]
"""
from __future__ import annotations

import argparse
import random
import shutil
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 20261020

DOMAINS = [
    "hr", "payroll", "marketing", "ads", "crm", "inventory", "warehouse", "shipping",
    "procurement", "vendor", "legal", "compliance", "security", "it", "facilities",
    "fleet", "events", "webinar", "partner", "reseller", "affiliate", "survey",
    "content", "seo", "email", "sms", "push", "mobile", "web", "experiment",
    "forecast", "budget", "expense", "travel", "recruiting", "training", "asset",
    "contract", "quote", "pricing", "tax", "ledger", "treasury", "audit",
]
ENTITIES = [
    "records", "items", "logs", "entries", "requests", "snapshots", "metrics",
    "assignments", "approvals", "batches", "jobs", "tasks", "notes", "allocations",
    "reviews", "targets", "periods", "segments", "channels", "rules",
]
SHARED_COLUMNS = [
    ("account_id", "INTEGER"), ("user_id", "INTEGER"), ("status", "TEXT"),
    ("amount_cents", "INTEGER"), ("created_at", "TEXT"), ("updated_at", "TEXT"),
    ("currency", "TEXT"), ("name", "TEXT"), ("segment", "TEXT"), ("priority", "TEXT"),
    ("region", "TEXT"), ("owner_email", "TEXT"), ("score", "REAL"), ("plan_id", "INTEGER"),
]
OWN_COLUMNS = [
    "code", "label", "category", "source", "quantity", "unit_cost_cents", "rate",
    "started_on", "ended_on", "approved_by", "external_ref", "is_active", "notes",
    "weight", "channel", "campaign", "stage", "outcome", "duration_minutes", "budget_cents",
]
STATUS_VALUES = ["active", "inactive", "pending", "closed", "archived"]


def build(output: Path, table_count: int) -> Path:
    source = HERE / "saaspulse.db"
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    rng = random.Random(SEED)
    names: list[str] = []
    while len(names) < table_count:
        name = f"{rng.choice(DOMAINS)}_{rng.choice(ENTITIES)}"
        if name not in names:
            names.append(name)
    conn = sqlite3.connect(output)
    try:
        for name in names:
            columns = [("id", "INTEGER PRIMARY KEY")]
            columns += rng.sample(SHARED_COLUMNS, rng.randint(3, 7))
            columns += [(column, rng.choice(["TEXT", "INTEGER", "REAL"])) for column in rng.sample(OWN_COLUMNS, rng.randint(2, 6))]
            ddl = ", ".join(f"{column} {kind}" for column, kind in columns)
            conn.execute(f"CREATE TABLE {name} ({ddl})")
            rows = []
            for row_id in range(1, rng.randint(5, 40) + 1):
                row = [row_id]
                for column, kind in columns[1:]:
                    if column == "status":
                        row.append(rng.choice(STATUS_VALUES))
                    elif kind.startswith("INTEGER"):
                        row.append(rng.randint(0, 100_000))
                    elif kind == "REAL":
                        row.append(round(rng.random() * 100, 2))
                    else:
                        row.append(f"{column}_{rng.randint(1, 50)}")
                rows.append(row)
            placeholders = ", ".join("?" for _ in columns)
            conn.executemany(f"INSERT INTO {name} VALUES ({placeholders})", rows)
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tables", type=int, default=300)
    parser.add_argument("--output", default=str(HERE / "saaspulse_wide.db"))
    args = parser.parse_args()
    path = build(Path(args.output), args.tables)
    print(f"Wrote {path} with {args.tables} distractor tables")


if __name__ == "__main__":
    main()
