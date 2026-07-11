"""01 - Quickstart: safe SQL against the demo fixture, no API keys required.

Run:
    python examples/01_quickstart.py

This connects QueryPilot to the bundled demo SQLite fixture, runs a validated
read-only query, then answers a natural-language question through the offline
deterministic demo generator -- so it works with no LLM key configured.
"""

from __future__ import annotations

from _common import demo_database_url

from querypilot import QueryPilot


def main() -> None:
    qp = QueryPilot.connect(
        database_url=demo_database_url(),
        dialect="sqlite",
        readonly=True,
        max_rows=100,
    )

    # 1. Inspect the schema QueryPilot introspected from the fixture.
    schema = qp.get_schema()
    print("Tables:", ", ".join(table.name for table in schema.tables))

    # 2. Execute raw SQL. QueryPilot validates + rewrites before running.
    result = qp.execute_sql(
        "SELECT customer_name, arr, segment FROM customers ORDER BY arr DESC"
    )
    print(f"\nexecute_sql -> {result.row_count} rows")
    print("rewritten SQL:", result.sql)
    for row in result.rows:
        print("  ", row)

    # 3. Offline natural-language ask() via the deterministic demo generator.
    answer = qp.ask("Top customers by revenue")
    print("\nask('Top customers by revenue')")
    print("generated SQL:", answer.sql)
    print("explanation:  ", answer.explanation)
    print("rows:")
    for row in answer.rows:
        print("  ", row)

    # 4. The validation risk level attached to that answer.
    print("\nvalidation.risk_level:", answer.validation.risk_level)
    print("validation.valid:     ", answer.validation.valid)


if __name__ == "__main__":
    main()
