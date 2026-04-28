from __future__ import annotations

import re
from typing import Protocol

from querypilot.core.types import DatabaseSchema, GeneratedSQL


class SQLGenerator(Protocol):
    def generate(self, question: str, schema: DatabaseSchema, max_rows: int) -> GeneratedSQL:
        ...


class DemoSQLGenerator:
    def generate(self, question: str, schema: DatabaseSchema, max_rows: int) -> GeneratedSQL:
        normalized = question.lower()
        explicit_limit = _extract_limit(normalized)
        limit = min(explicit_limit or max_rows, max_rows)

        if "count" in normalized and schema.tables:
            table = _best_table(normalized, schema) or schema.tables[0]
            return GeneratedSQL(
                question=question,
                sql=f"SELECT COUNT(*) AS count FROM {table.name} LIMIT {limit}",
                explanation=f"Counts rows in {table.name}.",
            )

        customer_table = schema.get_table("customers")
        if customer_table and any(term in normalized for term in ["customer", "customers"]):
            if "revenue" in normalized and customer_table.get_column("revenue"):
                return GeneratedSQL(
                    question=question,
                    sql=(
                        "SELECT customer_name, revenue FROM customers "
                        f"ORDER BY revenue DESC LIMIT {limit}"
                    ),
                    explanation="Ranks customers by revenue.",
                )
            if "arr" in normalized and customer_table.get_column("arr"):
                return GeneratedSQL(
                    question=question,
                    sql=f"SELECT customer_name, arr FROM customers ORDER BY arr DESC LIMIT {limit}",
                    explanation="Ranks customers by ARR.",
                )
            return GeneratedSQL(
                question=question,
                sql=f"SELECT * FROM customers LIMIT {limit}",
                explanation="Returns customer records.",
            )

        table = _best_table(normalized, schema)
        if table and any(term in normalized for term in ["show", "list", "top"]):
            return GeneratedSQL(
                question=question,
                sql=f"SELECT * FROM {table.name} LIMIT {limit}",
                explanation=f"Returns rows from {table.name}.",
            )

        return GeneratedSQL(
            question=question,
            sql=None,
            explanation=None,
            errors=[
                "Offline generator could not map this question to a safe SQL template. "
                "Configure a SQLGenerator provider for broader natural-language support."
            ],
        )


def _extract_limit(question: str) -> int | None:
    match = re.search(r"\btop\s+(\d+)\b|\blimit\s+(\d+)\b", question)
    if not match:
        return None
    value = match.group(1) or match.group(2)
    return int(value)


def _best_table(question: str, schema: DatabaseSchema):
    for table in schema.tables:
        if table.name.lower() in question or table.name.lower().rstrip("s") in question:
            return table
    return None
