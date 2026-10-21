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

    if config.ignore_column_names:
        return _compare_values_only(gold_rows, candidate_rows, order_sensitive, config)

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


_MAX_IGNORE_NAME_COLUMNS = 16


def _compare_values_only(
    gold_rows: list[dict],
    candidate_rows: list[dict],
    order_sensitive: bool,
    config: ComparisonConfig,
) -> RowsetMatch:
    """Compare candidate to gold by VALUES only, ignoring column names.

    This is the Spider/BIRD "execution accuracy" comparison: two result sets are
    equal when their *values* line up, regardless of the aliases the model chose
    (``COUNT(*) AS customer_count`` vs gold ``COUNT(*) AS count``).

    Semantics
    ---------
    * Column *count* must match. A different number of columns is a hard
      mismatch (reported in ``column_mismatch``), never reshaped away.
    * The candidate passes iff *some* bijection of its columns onto gold's
      columns makes the value rows equal to gold under all the normal
      normalizations — row-order-insensitivity (unless gold has ``ORDER BY`` or
      ``ignore_row_order`` is off), float tolerance, datetime/date/time ISO
      normalization, case folding, NaN/None handling. The chosen column
      permutation and the row matching are found together, not independently.
    * The search is backtracking over column assignments, pruned by
      column-value-*multiset* compatibility: a candidate column can only fill a
      gold column when the two hold the same bag of values (a necessary
      condition, since row reordering permutes every column identically). For
      the overwhelmingly common case of columns with distinct value multisets
      this collapses to a single forced assignment; it is *not* brute-force
      factorial. Most-constrained columns are assigned first and the search
      returns on the first assignment whose full rowset verifies.
    * Empty on either side short-circuits to the ordinary bag/ordered path,
      which already yields correct empty semantics (both empty -> match; one
      empty -> mismatch) without any column relabeling.

    Known false positives (accepted)
    ---------------------------------
    Because only values are matched, two columns whose value *multisets* are
    identical can be swapped and still "match" even if a name-aware human would
    call them different columns — e.g. gold ``(min, max)`` = ``[(1, 3)]`` vs
    candidate ``[(3, 1)]`` passes when ``ignore_row_order`` collapses the single
    row. This is the same over-permissiveness Spider/BIRD execution accuracy
    accepts upstream: for benchmark *scoring* it is far cheaper than the
    column-name false-*negatives* (identical values failing on an alias) it
    removes, so it is deliberately allowed here. Use name-aware comparison (the
    default, ``ignore_column_names=False``) when column identity matters.
    """
    tolerance = config.float_tolerance
    norm_gold = [_normalize_row(row, config) for row in gold_rows]
    norm_candidate = [_normalize_row(row, config) for row in candidate_rows]

    # Empty on either side: a column permutation is meaningless, and the
    # standard comparison already produces the right match/mismatch verdict.
    if not gold_rows or not candidate_rows:
        if order_sensitive:
            return _compare_ordered(norm_gold, norm_candidate, tolerance=tolerance)
        return _compare_bag(norm_gold, norm_candidate, tolerance=tolerance)

    gold_cols = list(gold_rows[0].keys())
    candidate_cols = list(candidate_rows[0].keys())

    if len(gold_cols) != len(candidate_cols):
        return RowsetMatch(
            matched=False,
            order_sensitive=order_sensitive,
            column_mismatch=[
                f"column count mismatch: gold has {len(gold_cols)} columns, "
                f"candidate has {len(candidate_cols)}"
            ],
            normalized_gold_rows=norm_gold,
            normalized_candidate_rows=norm_candidate,
        )

    n = len(gold_cols)
    if n > _MAX_IGNORE_NAME_COLUMNS:
        raise ValueError(
            f"ignore_column_names comparison supports at most "
            f"{_MAX_IGNORE_NAME_COLUMNS} columns; got {n}. The value-permutation "
            "search is factorial in the number of columns that share a value "
            "multiset, so wide result sets are rejected rather than run unbounded."
        )

    gold_vectors = [[row[col] for row in norm_gold] for col in gold_cols]
    cand_vectors = [[row[col] for row in norm_candidate] for col in candidate_cols]

    # Candidate columns each gold column could be filled by (multiset-compatible).
    compatible: list[list[int]] = [
        [
            j
            for j in range(n)
            if _multiset_equal(gold_vectors[i], cand_vectors[j], tolerance)
        ]
        for i in range(n)
    ]

    def _verify(assignment: list[int]) -> RowsetMatch | None:
        relabeled = [
            {gold_cols[i]: row[candidate_cols[assignment[i]]] for i in range(n)}
            for row in norm_candidate
        ]
        if order_sensitive:
            result = _compare_ordered(norm_gold, relabeled, tolerance=tolerance)
        else:
            result = _compare_bag(norm_gold, relabeled, tolerance=tolerance)
        return result if result.matched else None

    # Most-constrained gold columns first, to prune the backtracking hard.
    order = sorted(range(n), key=lambda i: len(compatible[i]))
    assignment = [-1] * n
    used = [False] * n

    def _search(depth: int) -> RowsetMatch | None:
        if depth == n:
            return _verify(assignment)
        col = order[depth]
        for j in compatible[col]:
            if used[j]:
                continue
            used[j] = True
            assignment[col] = j
            found = _search(depth + 1)
            if found is not None:
                return found
            used[j] = False
            assignment[col] = -1
        return None

    matched = _search(0)
    if matched is not None:
        return matched

    # No column permutation reproduces gold: return the identity (positional)
    # relabeling for diagnostics; it is guaranteed matched=False here.
    identity = [
        {gold_cols[i]: row[candidate_cols[i]] for i in range(n)}
        for row in norm_candidate
    ]
    if order_sensitive:
        return _compare_ordered(norm_gold, identity, tolerance=tolerance)
    return _compare_bag(norm_gold, identity, tolerance=tolerance)


def _multiset_equal(left: list[Any], right: list[Any], tolerance: float) -> bool:
    """Whether two column value-lists hold the same bag of values.

    A necessary condition for one candidate column to stand in for a gold column
    under value-only comparison, used to prune the permutation search. Honors the
    same float tolerance and NaN/None handling as row comparison (greedy pairing
    when a tolerance is set, exact counting otherwise — matching ``_compare_bag``).
    """
    if len(left) != len(right):
        return False
    if tolerance > 0:
        remaining = list(right)
        for value in left:
            match_index = None
            for index, candidate in enumerate(remaining):
                if _values_equal(value, candidate, tolerance):
                    match_index = index
                    break
            if match_index is None:
                return False
            remaining.pop(match_index)
        return True
    return Counter(_hashable(v) for v in left) == Counter(_hashable(v) for v in right)


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
