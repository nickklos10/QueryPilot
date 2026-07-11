from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

import pytest

from querypilot.evals.compare import NAN, RowsetMatch, ValueMismatch, compare_rows, has_order_by
from querypilot.evals.loader import load_suite, write_suite
from querypilot.evals.suite import BenchmarkCase, BenchmarkSuite, ComparisonConfig


def _rows(*rows: tuple) -> list[dict]:
    keys = ("a", "b")
    return [dict(zip(keys, row)) for row in rows]


def test_identical_rows_match() -> None:
    gold = [{"id": 1, "name": "Acme"}, {"id": 2, "name": "Globex"}]

    result = compare_rows(gold, list(gold), "SELECT * FROM customers")

    assert isinstance(result, RowsetMatch)
    assert result.matched is True
    assert result.missing_rows == []
    assert result.extra_rows == []
    assert result.mismatched_values == []
    assert result.column_mismatch == []


def test_empty_both_match() -> None:
    result = compare_rows([], [], "SELECT * FROM customers")

    assert result.matched is True


def test_empty_gold_but_candidate_has_rows() -> None:
    candidate = [{"id": 1}]

    result = compare_rows([], candidate, "SELECT * FROM customers")

    assert result.matched is False
    assert result.extra_rows == candidate


def test_empty_candidate_but_gold_has_rows() -> None:
    gold = [{"id": 1}]

    result = compare_rows(gold, [], "SELECT * FROM customers")

    assert result.matched is False
    assert result.missing_rows == gold


def test_order_insensitive_match_when_no_order_by() -> None:
    gold = [{"id": 1}, {"id": 2}]
    candidate = [{"id": 2}, {"id": 1}]

    result = compare_rows(gold, candidate, "SELECT id FROM customers")

    assert result.matched is True
    assert result.order_sensitive is False


def test_order_sensitive_when_gold_has_order_by() -> None:
    gold = [{"id": 1}, {"id": 2}]
    candidate = [{"id": 2}, {"id": 1}]

    result = compare_rows(gold, candidate, "SELECT id FROM customers ORDER BY id")

    assert result.matched is False
    assert result.order_sensitive is True
    assert len(result.mismatched_values) == 2


def test_order_sensitive_when_config_disables_ignore_row_order() -> None:
    gold = [{"id": 1}, {"id": 2}]
    candidate = [{"id": 2}, {"id": 1}]

    result = compare_rows(
        gold,
        candidate,
        "SELECT id FROM customers",
        ComparisonConfig(ignore_row_order=False),
    )

    assert result.order_sensitive is True
    assert result.matched is False


def test_missing_rows_detected_unordered() -> None:
    gold = [{"id": 1}, {"id": 2}, {"id": 3}]
    candidate = [{"id": 1}, {"id": 2}]

    result = compare_rows(gold, candidate, "SELECT id FROM customers")

    assert result.matched is False
    assert result.missing_rows == [{"id": 3}]
    assert result.extra_rows == []


def test_extra_rows_detected_unordered() -> None:
    gold = [{"id": 1}]
    candidate = [{"id": 1}, {"id": 2}]

    result = compare_rows(gold, candidate, "SELECT id FROM customers")

    assert result.matched is False
    assert result.missing_rows == []
    assert result.extra_rows == [{"id": 2}]


def test_mismatched_values_ordered() -> None:
    gold = [{"id": 1, "name": "Acme"}, {"id": 2, "name": "Globex"}]
    candidate = [{"id": 1, "name": "Acme"}, {"id": 2, "name": "Initech"}]

    result = compare_rows(gold, candidate, "SELECT id, name FROM customers ORDER BY id")

    assert result.matched is False
    assert result.order_sensitive is True
    assert result.mismatched_values == [
        ValueMismatch(row_index=1, column="name", gold="Globex", candidate="Initech")
    ]


def test_length_mismatch_ordered() -> None:
    gold = [{"id": 1}, {"id": 2}]
    candidate = [{"id": 1}]

    result = compare_rows(gold, candidate, "SELECT id FROM customers ORDER BY id")

    assert result.matched is False
    assert result.missing_rows == [{"id": 2}]
    assert result.mismatched_values == []


def test_column_set_mismatch() -> None:
    gold = [{"id": 1, "name": "Acme"}]
    candidate = [{"id": 1, "revenue": 100}]

    result = compare_rows(gold, candidate, "SELECT * FROM customers")

    assert result.matched is False
    assert any("name" in m for m in result.column_mismatch)
    assert any("revenue" in m for m in result.column_mismatch)
    assert result.mismatched_values == []


def test_column_order_mismatch_when_config_disables_ignore() -> None:
    gold = [{"a": 1, "b": 2}]
    candidate = [{"b": 2, "a": 1}]

    result = compare_rows(
        gold,
        candidate,
        "SELECT a, b FROM t",
        ComparisonConfig(ignore_column_order=False),
    )

    assert result.matched is False
    assert result.column_mismatch
    assert "column order mismatch" in result.column_mismatch[0]


def test_column_order_ignored_by_default() -> None:
    gold = [{"a": 1, "b": 2}]
    candidate = [{"b": 2, "a": 1}]

    result = compare_rows(gold, candidate, "SELECT a, b FROM t")

    assert result.matched is True


def test_int_vs_float_coerced_equal() -> None:
    gold = [{"x": 100}]
    candidate = [{"x": 100.0}]

    result = compare_rows(
        gold, candidate, "SELECT x FROM t", ComparisonConfig(float_tolerance=0.001)
    )

    assert result.matched is True


def test_decimal_to_float_coerced() -> None:
    gold = [{"x": Decimal("100.00")}]
    candidate = [{"x": 100.0}]

    result = compare_rows(
        gold, candidate, "SELECT x FROM t", ComparisonConfig(float_tolerance=0.001)
    )

    assert result.matched is True


def test_float_tolerance_within_bound_match() -> None:
    gold = [{"x": 100.0001}]
    candidate = [{"x": 100.0002}]

    result = compare_rows(
        gold, candidate, "SELECT x FROM t", ComparisonConfig(float_tolerance=0.001)
    )

    assert result.matched is True


def test_float_tolerance_outside_bound_mismatch() -> None:
    gold = [{"x": 100.0}]
    candidate = [{"x": 100.5}]

    result = compare_rows(
        gold, candidate, "SELECT x FROM t", ComparisonConfig(float_tolerance=0.001)
    )

    assert result.matched is False


def test_zero_tolerance_strict_float_compare() -> None:
    gold = [{"x": 100.0001}]
    candidate = [{"x": 100.0002}]

    result = compare_rows(
        gold, candidate, "SELECT x FROM t", ComparisonConfig(float_tolerance=0.0)
    )

    assert result.matched is False


def test_float_tolerance_at_decimal_boundary_ordered() -> None:
    # Regression: round(1.0004, 3)=1.0 vs round(1.0006, 3)=1.001 used to false-mismatch
    # under the old quantization-based tolerance even though abs(diff)=0.0002 < 0.001.
    gold = [{"x": 1.0004}]
    candidate = [{"x": 1.0006}]

    result = compare_rows(
        gold,
        candidate,
        "SELECT x FROM t ORDER BY x",
        ComparisonConfig(float_tolerance=0.001),
    )

    assert result.matched is True


def test_float_tolerance_at_decimal_boundary_unordered() -> None:
    gold = [{"x": 1.0004}]
    candidate = [{"x": 1.0006}]

    result = compare_rows(
        gold, candidate, "SELECT x FROM t", ComparisonConfig(float_tolerance=0.001)
    )

    assert result.matched is True


def test_float_tolerance_unordered_with_duplicates() -> None:
    gold = [{"x": 1.0001}, {"x": 2.0001}, {"x": 2.0001}]
    candidate = [{"x": 2.0002}, {"x": 1.00005}, {"x": 2.00005}]

    result = compare_rows(
        gold, candidate, "SELECT x FROM t", ComparisonConfig(float_tolerance=0.001)
    )

    assert result.matched is True


def test_nan_matches_nan() -> None:
    gold = [{"x": float("nan")}]
    candidate = [{"x": float("nan")}]

    result = compare_rows(gold, candidate, "SELECT x FROM t ORDER BY x")

    assert result.matched is True


def test_nan_does_not_match_real_string() -> None:
    gold = [{"x": float("nan")}]
    candidate = [{"x": "__NaN__"}]

    result = compare_rows(gold, candidate, "SELECT x FROM t ORDER BY x")

    assert result.matched is False


def test_nan_normalized_to_sentinel_in_normalized_rows() -> None:
    gold = [{"x": float("nan")}]
    candidate = [{"x": float("nan")}]

    result = compare_rows(gold, candidate, "SELECT x FROM t")

    assert result.normalized_gold_rows == [{"x": NAN}]
    assert result.normalized_candidate_rows == [{"x": NAN}]


def test_nan_does_not_match_non_nan_float() -> None:
    gold = [{"x": float("nan")}]
    candidate = [{"x": 0.0}]

    result = compare_rows(
        gold,
        candidate,
        "SELECT x FROM t ORDER BY x",
        ComparisonConfig(float_tolerance=0.1),
    )

    assert result.matched is False
    assert result.mismatched_values[0].gold is NAN


def test_datetime_normalized_to_iso() -> None:
    gold = [{"ts": datetime(2026, 4, 27, 12, 0, 0)}]
    candidate = [{"ts": "2026-04-27T12:00:00"}]

    result = compare_rows(gold, candidate, "SELECT ts FROM events")

    assert result.matched is True


def test_date_normalized_to_iso() -> None:
    gold = [{"d": date(2026, 4, 27)}]
    candidate = [{"d": "2026-04-27"}]

    result = compare_rows(gold, candidate, "SELECT d FROM events")

    assert result.matched is True


def test_time_normalized_to_iso() -> None:
    gold = [{"t": time(12, 30, 0)}]
    candidate = [{"t": "12:30:00"}]

    result = compare_rows(gold, candidate, "SELECT t FROM events")

    assert result.matched is True


def test_normalize_datetimes_disabled() -> None:
    ts = datetime(2026, 4, 27, 12, 0, 0)
    gold = [{"ts": ts}]
    candidate = [{"ts": "2026-04-27T12:00:00"}]

    result = compare_rows(
        gold, candidate, "SELECT ts FROM events", ComparisonConfig(normalize_datetimes=False)
    )

    assert result.matched is False


def test_case_insensitive_strings_when_enabled() -> None:
    gold = [{"name": "Acme"}]
    candidate = [{"name": "ACME"}]

    result = compare_rows(
        gold,
        candidate,
        "SELECT name FROM customers",
        ComparisonConfig(case_insensitive_strings=True),
    )

    assert result.matched is True


def test_case_sensitive_strings_by_default() -> None:
    gold = [{"name": "Acme"}]
    candidate = [{"name": "ACME"}]

    result = compare_rows(gold, candidate, "SELECT name FROM customers")

    assert result.matched is False


def test_none_equals_none() -> None:
    gold = [{"x": None, "y": 1}]
    candidate = [{"x": None, "y": 1}]

    result = compare_rows(gold, candidate, "SELECT x, y FROM t")

    assert result.matched is True


def test_none_vs_value_mismatch() -> None:
    gold = [{"x": None}]
    candidate = [{"x": 0}]

    result = compare_rows(gold, candidate, "SELECT x FROM t ORDER BY x")

    assert result.matched is False
    assert result.mismatched_values[0].gold is None
    assert result.mismatched_values[0].candidate == 0


def test_bool_treated_as_int_consistent_with_python() -> None:
    gold = [{"flag": True}]
    candidate = [{"flag": 1}]

    result = compare_rows(gold, candidate, "SELECT flag FROM t")

    assert result.matched is True


def test_duplicate_rows_counted() -> None:
    gold = [{"id": 1}, {"id": 1}, {"id": 2}]
    candidate = [{"id": 1}, {"id": 2}, {"id": 2}]

    result = compare_rows(gold, candidate, "SELECT id FROM t")

    assert result.matched is False
    assert result.missing_rows == [{"id": 1}]
    assert result.extra_rows == [{"id": 2}]


def test_serializable_to_json() -> None:
    gold = [{"id": 1, "name": "Acme"}]
    candidate = [{"id": 2, "name": "Acme"}]

    result = compare_rows(gold, candidate, "SELECT * FROM customers ORDER BY id")
    payload = result.model_dump(mode="json")

    assert payload["matched"] is False
    assert payload["order_sensitive"] is True
    assert payload["mismatched_values"][0]["row_index"] == 0


def test_normalized_rows_returned_in_result() -> None:
    gold = [{"x": Decimal("1.0")}]
    candidate = [{"x": 1.0}]

    result = compare_rows(
        gold, candidate, "SELECT x FROM t", ComparisonConfig(float_tolerance=0.001)
    )

    assert result.normalized_gold_rows == [{"x": 1.0}]
    assert result.normalized_candidate_rows == [{"x": 1.0}]


# --------------------------------------------------------------------------- #
# ignore_column_names: values-only (Spider/BIRD execution accuracy) comparison
# --------------------------------------------------------------------------- #

_VALUES_ONLY = ComparisonConfig(ignore_column_names=True)


def test_count_alias_mismatch_passes_with_ignore_column_names() -> None:
    # The exact failure found today: identical values, different alias.
    gold = [{"count": 3}]
    candidate = [{"customer_count": 3}]

    assert compare_rows(gold, candidate, "SELECT COUNT(*) AS count FROM t", _VALUES_ONLY).matched
    # ...and it still (correctly) fails under the default name-aware comparison.
    assert not compare_rows(gold, candidate, "SELECT COUNT(*) AS count FROM t").matched


def test_ignore_column_names_permuted_columns_distinct_values() -> None:
    # Candidate presents the columns in a different order under different names;
    # each column's value multiset is distinct, so there is exactly one mapping.
    gold = [{"a": 1, "b": 10}, {"a": 2, "b": 20}]
    candidate = [{"x": 10, "y": 1}, {"x": 20, "y": 2}]

    assert compare_rows(gold, candidate, "SELECT a, b FROM t", _VALUES_ONLY).matched
    # Name-aware comparison rejects it (columns a,b vs x,y are a set mismatch).
    assert not compare_rows(gold, candidate, "SELECT a, b FROM t").matched


def test_ignore_column_names_identical_multiset_swap_is_accepted_false_positive() -> None:
    # DOCUMENTED false-positive class: two columns whose value multisets are
    # identical ({1, 2} each) can swap. The candidate has min/max reversed row by
    # row, which a name-aware, order-sensitive comparison rightly fails -- but
    # values-only permutes the columns and reports a match. Accepted for
    # benchmark scoring (see _compare_values_only docstring).
    gold = [{"a": 1, "b": 2}, {"a": 2, "b": 1}]
    candidate = [{"a": 2, "b": 1}, {"a": 1, "b": 2}]
    sql = "SELECT a, b FROM t ORDER BY a"

    assert not compare_rows(gold, candidate, sql).matched  # name-aware: swapped
    assert compare_rows(gold, candidate, sql, _VALUES_ONLY).matched  # values-only: passes


def test_ignore_column_names_composes_with_row_order_and_float_tolerance() -> None:
    # Permuted + renamed columns, reordered rows, and values only equal within
    # tolerance -- all normalizations still apply together.
    config = ComparisonConfig(ignore_column_names=True, float_tolerance=0.001)
    gold = [{"a": 1.0001, "b": 10.0}, {"a": 2.0, "b": 20.0}]
    candidate = [{"x": 20.0, "y": 2.00005}, {"x": 10.00005, "y": 1.0002}]

    assert compare_rows(gold, candidate, "SELECT a, b FROM t", config).matched


def test_ignore_column_names_column_count_mismatch_still_fails() -> None:
    gold = [{"a": 1, "b": 2}]
    candidate = [{"only": 1}]

    result = compare_rows(gold, candidate, "SELECT a, b FROM t", _VALUES_ONLY)

    assert result.matched is False
    assert any("column count mismatch" in m for m in result.column_mismatch)


def test_ignore_column_names_no_valid_permutation_fails() -> None:
    # Same column count, but no permutation reproduces gold's values.
    gold = [{"a": 1, "b": 2}]
    candidate = [{"x": 1, "y": 3}]

    result = compare_rows(gold, candidate, "SELECT a, b FROM t", _VALUES_ONLY)

    assert result.matched is False


def test_ignore_column_names_empty_both_match() -> None:
    assert compare_rows([], [], "SELECT a FROM t", _VALUES_ONLY).matched


def test_ignore_column_names_empty_candidate_fails() -> None:
    assert not compare_rows([{"a": 1}], [], "SELECT a FROM t", _VALUES_ONLY).matched


def test_ignore_column_names_column_cap_raises() -> None:
    wide = {f"c{i}": i for i in range(17)}
    with pytest.raises(ValueError, match="at most 16 columns"):
        compare_rows([wide], [dict(wide)], "SELECT * FROM t", _VALUES_ONLY)


def test_ignore_column_names_yaml_round_trip(tmp_path) -> None:
    suite = BenchmarkSuite(
        name="values_only",
        fixture_db="sqlite:///:memory:",
        comparison=ComparisonConfig(ignore_column_names=True),
        cases=[BenchmarkCase(id="c0", question="q", gold_sql="SELECT 1")],
    )
    path = write_suite(suite, tmp_path / "values_only.yaml")

    loaded = load_suite(path)

    assert loaded.comparison.ignore_column_names is True


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT id FROM t", False),
        ("SELECT id FROM t ORDER BY id", True),
        ("SELECT id FROM t ORDER BY id DESC LIMIT 10", True),
        ("SELECT * FROM (SELECT id FROM t ORDER BY id) sub", False),
        ("SELECT id FROM t UNION SELECT id FROM u ORDER BY id", True),
        ("WITH x AS (SELECT 1) SELECT * FROM x", False),
        ("WITH x AS (SELECT 1) SELECT * FROM x ORDER BY 1", True),
        ("INVALID SQL ORDER BY", False),
    ],
)
def test_has_order_by(sql: str, expected: bool) -> None:
    assert has_order_by(sql) is expected
