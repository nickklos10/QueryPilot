"""Keyword search over tables, columns, comments and sample values.

Matching is lexical but forgiving: names are split on snake_case and camelCase
boundaries, plural and singular forms match each other ("customer" finds
``customers``), and a question word that is a prefix of a name part ("subscr")
still counts at a lower weight.
"""
from __future__ import annotations

import re

from querypilot.core.types import DatabaseSchema, SchemaMatch, TableSchema

_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "can", "did", "do", "does",
        "each", "every", "for", "from", "give", "has", "have", "how", "i", "in", "is",
        "it", "its", "list", "many", "me", "much", "of", "on", "or", "our", "per",
        "please", "show", "that", "the", "their", "them", "there", "these", "this",
        "those", "to", "top", "was", "were", "what", "when", "where", "which", "who",
        "whose", "why", "with", "all", "most", "least", "return",
        "named", "column", "columns", "table", "tables", "row", "rows",
    }
)

# Weights: a table-name hit dominates, then column names, then comments/values.
_TABLE_EXACT, _TABLE_PREFIX = 6, 3
_COLUMN_EXACT, _COLUMN_PREFIX = 2, 1
_COMMENT_HIT = 1
_VALUE_HIT = 2
_MIN_PREFIX = 4


def search_schema(schema: DatabaseSchema, query: str) -> list[SchemaMatch]:
    """Rank tables (and the columns that matched) by relevance to ``query``."""
    terms = query_terms(query)
    if not terms:
        return []
    matches: list[SchemaMatch] = []
    for table in schema.tables:
        score, column_hits, reasons = _score_table(table, terms)
        if score:
            matches.append(
                SchemaMatch(
                    table=table.name,
                    column=", ".join(column_hits) if column_hits else None,
                    score=score,
                    reason="Matched " + ", ".join(reasons),
                )
            )
    return sorted(matches, key=lambda match: (-match.score, match.table))


def table_scores(schema: DatabaseSchema, query: str) -> dict[str, int]:
    """Relevance score per table name (lower-cased); tables with no hit are omitted."""
    terms = query_terms(query)
    scores: dict[str, int] = {}
    if not terms:
        return scores
    for table in schema.tables:
        score, _, _ = _score_table(table, terms)
        if score:
            scores[table.name.lower()] = score
    return scores


def query_terms(text: str) -> set[str]:
    """Stemmed, stopword-free terms of a question or search query."""
    return {_stem(token) for token in split_words(text) if token not in _STOPWORDS}


def split_words(text: str) -> list[str]:
    """Split text into lower-case words on non-alphanumerics, snake_case and camelCase."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    return [word for word in re.findall(r"[a-z]+|[0-9]+", spaced.lower()) if word]


def name_terms(name: str) -> set[str]:
    return {_stem(word) for word in split_words(name)}


def _stem(word: str) -> str:
    """Very small plural -> singular folding, enough for schema names."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _term_hits(terms: set[str], candidates: set[str]) -> tuple[int, int]:
    """Count exact and prefix hits of query terms against name terms."""
    exact = len(terms & candidates)
    prefix = 0
    for term in terms - candidates:
        if len(term) < _MIN_PREFIX:
            continue
        if any(
            (candidate.startswith(term) or term.startswith(candidate))
            and len(candidate) >= _MIN_PREFIX
            for candidate in candidates
        ):
            prefix += 1
    return exact, prefix


def _score_table(table: TableSchema, terms: set[str]) -> tuple[int, list[str], list[str]]:
    reasons: list[str] = []
    exact, prefix = _term_hits(terms, name_terms(table.name))
    score = exact * _TABLE_EXACT + prefix * _TABLE_PREFIX
    if exact or prefix:
        reasons.append("table name")
    if table.comment:
        comment_hits = len(terms & name_terms(table.comment))
        if comment_hits:
            score += comment_hits * _COMMENT_HIT
            reasons.append("table description")

    column_hits: list[str] = []
    for column in table.columns:
        col_exact, col_prefix = _term_hits(terms, name_terms(column.name))
        col_score = col_exact * _COLUMN_EXACT + col_prefix * _COLUMN_PREFIX
        if column.comment:
            col_score += len(terms & name_terms(column.comment)) * _COMMENT_HIT
        if column.sample_values:
            value_terms: set[str] = set()
            for value in column.sample_values:
                value_terms |= name_terms(value)
            col_score += len(terms & value_terms) * _VALUE_HIT
        if col_score:
            column_hits.append(column.name)
            score += col_score
    if column_hits:
        reasons.append("column names, descriptions or values")
    return score, column_hits, reasons
