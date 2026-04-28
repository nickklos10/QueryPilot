from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture()
def demo_db_url(tmp_path: Path) -> str:
    db_path = tmp_path / "demo.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            customer_name TEXT NOT NULL,
            arr INTEGER NOT NULL,
            revenue INTEGER NOT NULL
        );

        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            revenue INTEGER NOT NULL
        );

        INSERT INTO customers (customer_name, arr, revenue) VALUES
            ('Acme Corp', 120000, 120000),
            ('Globex', 95000, 95000),
            ('Initech', 40000, 40000);

        INSERT INTO invoices (customer_id, month, revenue) VALUES
            (1, '2026-03', 10000),
            (1, '2026-04', 9000),
            (2, '2026-04', 8000);
        """
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{db_path}"
