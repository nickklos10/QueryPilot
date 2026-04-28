# QueryPilot

QueryPilot is a safe SQL tool layer for AI agents. It gives agents controlled access to relational databases through schema discovery, SQL generation, validation, read-only execution, and result explanation.

This first slice is offline-first: SQLite works end to end, PostgreSQL has a connector structure through SQLAlchemy, and natural-language generation uses a deterministic demo generator until an LLM provider is configured.
