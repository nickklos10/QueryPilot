from __future__ import annotations

from querypilot.connectors.postgres import _normalize_postgres_url


def test_postgres_url_defaults_to_psycopg_driver() -> None:
    assert (
        _normalize_postgres_url("postgresql://user:pass@localhost/db")
        == "postgresql+psycopg://user:pass@localhost/db"
    )
    assert (
        _normalize_postgres_url("postgres://user:pass@localhost/db")
        == "postgresql+psycopg://user:pass@localhost/db"
    )
    assert (
        _normalize_postgres_url("postgresql+psycopg://user:pass@localhost/db")
        == "postgresql+psycopg://user:pass@localhost/db"
    )
