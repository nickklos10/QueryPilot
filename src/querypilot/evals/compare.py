from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from querypilot.evals.suite import ComparisonConfig


class ValueMismatch(BaseModel):
    row_index: int
    column: str
    gold: Any
    candidate: Any


class RowsetMatch(BaseModel):
    matched: bool
    order_sensitive: bool
    column_mismatch: list[str] = Field(default_factory=list)
    missing_rows: list[dict] = Field(default_factory=list)
    extra_rows: list[dict] = Field(default_factory=list)
    mismatched_values: list[ValueMismatch] = Field(default_factory=list)
    normalized_gold_rows: list[dict] = Field(default_factory=list)
    normalized_candidate_rows: list[dict] = Field(default_factory=list)


def has_order_by(sql: str) -> bool:
    try:
        tree = parse_one(sql)
    except ParseError:
        return False
    if tree is None:
        return False
    return tree.args.get("order") is not None


def compare_rows(
    gold_rows: list[dict],
    candidate_rows: list[dict],
    gold_sql: str,
    config: ComparisonConfig | None = None,
) -> RowsetMatch:
    config = config or ComparisonConfig()

    order_sensitive = has_order_by(gold_sql) or not config.ignore_row_order

    column_mismatch = _column_mismatch(gold_rows, candidate_rows, config)
    if column_mismatch:
        return RowsetMatch(
            matched=False,
            order_sensitive=order_sensitive,
            column_mismatch=column_mismatch,
            normalized_gold_rows=gold_rows,
            normalized_candidate_rows=candidate_rows,
        )

    norm_gold = [_normalize_row(row, config) for row in gold_rows]
    norm_candidate = [_normalize_row(row, config) for row in candidate_rows]

    if order_sensitive:
        return _compare_ordered(norm_gold, norm_candidate, order_sensitive=True)
    return _compare_bag(norm_gold, norm_candidate, order_sensitive=False)


def _column_mismatch(
    gold_rows: list[dict], candidate_rows: list[dict], config: ComparisonConfig
) -> list[str]:
    if not gold_rows or not candidate_rows:
        return []
    gold_cols = list(gold_rows[0].keys())
    candidate_cols = list(candidate_rows[0].keys())

    if config.ignore_column_order:
        gold_set = set(gold_cols)
        candidate_set = set(candidate_cols)
        if gold_set == candidate_set:
            return []
        diffs: list[str] = []
        for col in sorted(gold_set - candidate_set):
            diffs.append(f"missing in candidate: {col}")
        for col in sorted(candidate_set - gold_set):
            diffs.append(f"unexpected in candidate: {col}")
        return diffs

    if gold_cols == candidate_cols:
        return []
    return [f"column order mismatch: gold={gold_cols} candidate={candidate_cols}"]


def _normalize_row(row: dict, config: ComparisonConfig) -> dict:
    return {key: _normalize_value(value, config) for key, value in row.items()}


def _normalize_value(value: Any, config: ComparisonConfig) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "__NaN__"
        return _quantize_float(value, config.float_tolerance)
    if isinstance(value, datetime):
        return value.isoformat() if config.normalize_datetimes else value
    if isinstance(value, date):
        return value.isoformat() if config.normalize_datetimes else value
    if isinstance(value, time):
        return value.isoformat() if config.normalize_datetimes else value
    if isinstance(value, str):
        if config.case_insensitive_strings:
            return value.casefold()
        return value
    return value


def _quantize_float(value: float, tolerance: float) -> float:
    if tolerance <= 0:
        return value
    digits = max(0, int(math.ceil(-math.log10(tolerance))))
    return round(value, digits)


def _compare_ordered(
    gold: list[dict], candidate: list[dict], *, order_sensitive: bool
) -> RowsetMatch:
    mismatched: list[ValueMismatch] = []
    missing: list[dict] = []
    extra: list[dict] = []

    common = min(len(gold), len(candidate))
    for index in range(common):
        for column, gold_value in gold[index].items():
            candidate_value = candidate[index].get(column)
            if not _values_equal(gold_value, candidate_value):
                mismatched.append(
                    ValueMismatch(
                        row_index=index,
                        column=column,
                        gold=gold_value,
                        candidate=candidate_value,
                    )
                )

    if len(gold) > len(candidate):
        missing = gold[common:]
    elif len(candidate) > len(gold):
        extra = candidate[common:]

    matched = not mismatched and not missing and not extra
    return RowsetMatch(
        matched=matched,
        order_sensitive=order_sensitive,
        missing_rows=missing,
        extra_rows=extra,
        mismatched_values=mismatched,
        normalized_gold_rows=gold,
        normalized_candidate_rows=candidate,
    )


def _compare_bag(
    gold: list[dict], candidate: list[dict], *, order_sensitive: bool
) -> RowsetMatch:
    gold_counter = Counter(_row_key(row) for row in gold)
    candidate_counter = Counter(_row_key(row) for row in candidate)

    missing_keys = gold_counter - candidate_counter
    extra_keys = candidate_counter - gold_counter

    missing_rows = _rows_for_keys(gold, missing_keys)
    extra_rows = _rows_for_keys(candidate, extra_keys)

    matched = not missing_keys and not extra_keys
    return RowsetMatch(
        matched=matched,
        order_sensitive=order_sensitive,
        missing_rows=missing_rows,
        extra_rows=extra_rows,
        normalized_gold_rows=gold,
        normalized_candidate_rows=candidate,
    )


def _row_key(row: dict) -> tuple:
    return tuple(sorted((key, _hashable(value)) for key, value in row.items()))


def _hashable(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


def _rows_for_keys(rows: list[dict], wanted: Counter) -> list[dict]:
    remaining = Counter(wanted)
    out: list[dict] = []
    for row in rows:
        key = _row_key(row)
        if remaining.get(key, 0) > 0:
            out.append(row)
            remaining[key] -= 1
    return out


def _values_equal(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, float) and isinstance(b, float):
        return a == b
    return a == b
