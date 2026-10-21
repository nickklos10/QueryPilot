"""Seed the canonical demo SQLite fixture used by built-in eval suites.

Run from the repo root:

    python tests/fixtures/seed_demo.py

Idempotent: recreates the file on every run, then VACUUMs to keep it
deterministic. The data lives in ``querypilot.demo`` so ``querypilot demo``
ships the same database.
"""

from __future__ import annotations

from pathlib import Path

from querypilot.demo import create_demo_db


def seed(db_path: Path) -> Path:
    return create_demo_db(db_path)


def default_db_path() -> Path:
    return Path(__file__).resolve().parent / "demo.db"


if __name__ == "__main__":
    target = default_db_path()
    seed(target)
    print(f"Seeded {target}")
