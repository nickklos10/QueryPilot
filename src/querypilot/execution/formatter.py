from __future__ import annotations


def explain_result(question: str, sql: str, rows: list[dict]) -> str:
    if not rows:
        return f"The validated query returned no rows for: {question}"
    columns = ", ".join(rows[0].keys())
    return (
        f"The validated query answers the question using columns {columns}. "
        f"It returned {len(rows)} row(s) after QueryPilot safety checks."
    )
