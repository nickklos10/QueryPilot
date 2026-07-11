"""04 - Access control: blocked columns, row filters, and masking. No API keys.

Run:
    python examples/04_access_control.py

Read-only SQL is necessary but not sufficient. This example attaches an
``AccessPolicy`` to the demo fixture that:

  * blocks the ``customers.arr`` column outright,
  * injects a mandatory row filter (only the ``enterprise`` segment is visible),
  * masks ``customers.revenue`` in results after execution.

It then shows a blocked query being rejected, and an allowed query returning a
row-filtered + masked result.
"""

from __future__ import annotations

from _common import demo_database_url

from querypilot import QueryPilot
from querypilot.access import AccessPolicy, MaskingRule


def main() -> None:
    policy = AccessPolicy(
        blocked_columns={"customers": ["arr"]},
        row_filters={"customers": "segment = 'enterprise'"},
        masking_rules={"customers": {"revenue": MaskingRule(mode="redact")}},
    )

    qp = QueryPilot.connect(
        database_url=demo_database_url(),
        dialect="sqlite",
        access_policy=policy,
    )

    print("Applied access policy:")
    for key, value in policy.summary().items():
        print(f"  {key}: {value}")

    # 1. A query touching a blocked column is rejected before execution.
    blocked_sql = "SELECT customer_name, arr FROM customers"
    print(f"\n[blocked query] {blocked_sql}")
    validation = qp.validate_sql(blocked_sql)
    print("  valid:         ", validation.valid)
    print("  blocked_reason:", validation.blocked_reason)
    try:
        qp.execute_sql(blocked_sql)
    except ValueError as exc:
        print("  execute_sql raised:", exc)

    # 2. An allowed query: row filter narrows to enterprise, revenue is masked.
    allowed_sql = "SELECT customer_name, revenue, segment FROM customers ORDER BY revenue DESC"
    print(f"\n[allowed query] {allowed_sql}")
    result = qp.execute_sql(allowed_sql)
    print("  rewritten SQL:", result.sql)
    print(f"  {result.row_count} rows returned (row filter applied):")
    for row in result.rows:
        print("  ", row)
    print("  result.access_policy:", result.access_policy)


if __name__ == "__main__":
    main()
