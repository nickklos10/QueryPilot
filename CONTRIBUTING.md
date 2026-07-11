# Contributing to QueryPilot

Thanks for considering a contribution. QueryPilot is an eval-driven SQL
reliability toolkit for AI agents — every change is measured against an
execution-truth eval suite, so the same standards apply to outside
contributions as to maintainer commits.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,eval]"
.venv/bin/pytest                                   # full test suite
.venv/bin/querypilot eval run \
    --suite suites/smoke.yaml \
    --generator demo \
    --report /tmp/eval-out.json                    # smoke harness end-to-end
```

Optional extras for provider-specific work:

```bash
.venv/bin/pip install -e ".[openai,anthropic,server,mcp]"
```

## Workflow

1. **Branch off `main`.** Use a short, scope-prefixed name:
   - `eval/<slug>` for eval-harness changes
   - `launch/<slug>` for distribution / launch-readiness changes
   - `fix/<slug>` for bug fixes
   - `feat/<slug>` for new features
2. **One PR per logical change.** Keep changes focused — multi-PR sequences
   are preferred over one large PR. PR-N+1 doesn't start until PR-N is
   merged.
3. **Tests are required.** New behavior needs a unit test; new validator
   guards need a safety-suite case; user-facing CLI changes need a CLI test.
4. **Review before merge.** Every PR gets maintainer review, and CI must be
   green — unit tests, lint, and the smoke eval gate run on every PR.
5. **Don't break the eval baseline.** CI runs `querypilot eval check` against
   `.eval/baseline.json`. If your change deliberately changes pass/safety/
   correctness rates, regenerate the baseline in the same PR and explain why
   in the description.

## Code style

- Python 3.11+, full type hints, `from __future__ import annotations` at the
  top of new modules.
- Pydantic v2 for serialized data shapes. Pure dataclasses or plain classes
  for internal-only data.
- No new mandatory dependencies. New integrations live behind a
  `[project.optional-dependencies]` extra (see `[eval]`, `[openai]`,
  `[anthropic]`).
- Keep the core (`src/querypilot/core/client.py`) clean. New eval, replay, or
  adapter code should compose existing public methods, not instrument the
  core.

## Reporting security issues

Don't open public issues for security vulnerabilities. Email
`nklos@inceptaanalytics.ai` instead. See `SECURITY.md` for the threat model
and disclosure process.

## License

By contributing, you agree your contributions are licensed under the MIT
License (see `LICENSE`).
