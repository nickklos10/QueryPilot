from __future__ import annotations

from querypilot.core.client import QueryPilot
from querypilot.evals.cases import EvalCase, EvalReport, EvalResult


def run_eval_cases(querypilot: QueryPilot, cases: list[EvalCase]) -> EvalReport:
    results = [_run_case(querypilot, case) for case in cases]
    passed = sum(1 for result in results if result.passed)
    return EvalReport(
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        results=results,
    )


def _run_case(querypilot: QueryPilot, case: EvalCase) -> EvalResult:
    errors: list[str] = []
    sql = case.sql
    if sql is None:
        if case.question is None:
            errors.append("EvalCase requires either sql or question.")
            return EvalResult(name=case.name, passed=False, errors=errors)
        generated = querypilot.generate_sql(case.question)
        sql = generated.sql
        if sql is None:
            errors.extend(generated.errors)
            return EvalResult(name=case.name, passed=not case.should_pass, errors=errors)

    validation = querypilot.validate_sql(sql)
    rewritten_sql = validation.rewritten_sql or sql

    if validation.valid != case.should_pass:
        errors.append(
            f"Expected validation pass={case.should_pass}, got pass={validation.valid}."
        )

    missing_tables = [
        table for table in case.expected_tables if table not in validation.tables
    ]
    if missing_tables:
        errors.append(f"Missing expected tables: {', '.join(missing_tables)}")

    for expected in case.expected_sql_contains:
        if expected.lower() not in rewritten_sql.lower():
            errors.append(f"Expected SQL to contain: {expected}")

    for forbidden in case.must_not_contain:
        if forbidden.lower() in rewritten_sql.lower():
            errors.append(f"SQL contained forbidden text: {forbidden}")

    return EvalResult(
        name=case.name,
        passed=not errors,
        generated_sql=rewritten_sql,
        validation=validation,
        errors=errors,
    )
