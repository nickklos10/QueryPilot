from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlglot import parse_one
from sqlglot.errors import ParseError

from querypilot.evals.suite import ComparisonConfig


class _NaNSentinel:
    _instance = None

    def __new__(cls) -> "_NaNSentinel":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<NaN>"

    def __reduce__(self):
        return (_NaNSentinel, ())


NAN = _NaNSentinel()


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
        return _compare_ordered(norm_gold, norm_candidate, tolerance=config.float_tolerance)
    return _compare_bag(norm_gold, norm_candidate, tolerance=config.float_tolerance)


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
            return NAN
        return value
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


def _compare_ordered(
    gold: list[dict], candidate: list[dict], *, tolerance: float
) -> RowsetMatch:
    mismatched: list[ValueMismatch] = []
    missing: list[dict] = []
    extra: list[dict] = []

    common = min(len(gold), len(candidate))
    for index in range(common):
        for column, gold_value in gold[index].items():
            candidate_value = candidate[index].get(column)
            if not _values_equal(gold_value, candidate_value, tolerance):
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
        order_sensitive=True,
        missing_rows=missing,
        extra_rows=extra,
        mismatched_values=mismatched,
        normalized_gold_rows=gold,
        normalized_candidate_rows=candidate,
    )


def _compare_bag(
    gold: list[dict], candidate: list[dict], *, tolerance: float
) -> RowsetMatch:
    if tolerance > 0:
        missing_rows, extra_rows = _greedy_match(gold, candidate, tolerance)
    else:
        missing_rows, extra_rows = _counter_diff(gold, candidate)

    matched = not missing_rows and not extra_rows
    return RowsetMatch(
        matched=matched,
        order_sensitive=False,
        missing_rows=missing_rows,
        extra_rows=extra_rows,
        normalized_gold_rows=gold,
        normalized_candidate_rows=candidate,
    )


def _counter_diff(
    gold: list[dict], candidate: list[dict]
) -> tuple[list[dict], list[dict]]:
    gold_counter = Counter(_row_key(row) for row in gold)
    candidate_counter = Counter(_row_key(row) for row in candidate)

    missing_keys = gold_counter - candidate_counter
    extra_keys = candidate_counter - gold_counter

    return _rows_for_keys(gold, missing_keys), _rows_for_keys(candidate, extra_keys)


def _greedy_match(
    gold: list[dict], candidate: list[dict], tolerance: float
) -> tuple[list[dict], list[dict]]:
    available = list(range(len(candidate)))
    missing: list[dict] = []
    matched_candidate: set[int] = set()

    for gold_row in gold:
        match_index = None
        for i in available:
            if _rows_equal(gold_row, candidate[i], tolerance):
                match_index = i
                break
        if match_index is None:
            missing.append(gold_row)
        else:
            available.remove(match_index)
            matched_candidate.add(match_index)

    extra = [candidate[i] for i in range(len(candidate)) if i not in matched_candidate]
    return missing, extra


def _rows_equal(gold: dict, candidate: dict, tolerance: float) -> bool:
    if set(gold.keys()) != set(candidate.keys()):
        return False
    for column, gold_value in gold.items():
        if not _values_equal(gold_value, candidate.get(column), tolerance):
            return False
    return True


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


def _values_equal(a: Any, b: Any, tolerance: float = 0.0) -> bool:
    if isinstance(a, _NaNSentinel) and isinstance(b, _NaNSentinel):
        return True
    if isinstance(a, _NaNSentinel) or isinstance(b, _NaNSentinel):
        return False
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if (
        isinstance(a, float)
        and isinstance(b, float)
        and not isinstance(a, bool)
        and not isinstance(b, bool)
        and tolerance > 0
    ):
        return abs(a - b) <= tolerance
    return a == b
