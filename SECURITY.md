# Security Policy

QueryPilot is a safety layer in front of real databases, so security reports
get priority attention.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | ✅        |

## Reporting a vulnerability

Please do not open a public issue for security problems.

Use one of these private channels instead:

- **GitHub private vulnerability reporting** (preferred):
  [Report a vulnerability](https://github.com/nickklos10/QueryPilot/security/advisories/new)
- **Email:** nklos@inceptaanalytics.ai

Include a proof-of-concept where possible — for example, SQL that bypasses
the validator, the access-policy enforcement (blocked columns, row filters,
masking), or the read-only execution guarantees.

## What to expect

- Acknowledgement within 72 hours.
- An assessment and remediation plan within 7 days for confirmed reports.
- Credit in the release notes for the fix, unless you prefer otherwise.

## Threat model notes

QueryPilot's validation is defense in depth, not a replacement for database
permissions. The documented production posture is a dedicated least-privilege
database role plus QueryPilot's read-only transaction and statement timeout.
Reports that require a fully-privileged database role and ignore that
guidance may be classified as hardening advice rather than vulnerabilities —
but send them anyway; we'd rather triage than miss something.
