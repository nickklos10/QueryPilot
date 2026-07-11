#!/usr/bin/env bash
# 05 - Custom eval suite: run it end-to-end with the offline demo generator.
#
#   pip install -e ".[eval]"
#   bash examples/05_custom_eval_suite/run.sh
#
# No API keys required: the `demo` generator is deterministic and offline.
set -euo pipefail

# Run from this script's directory so the suite path resolves.
cd "$(dirname "$0")"

# 1. Run the suite and write a JSON SuiteReport.
querypilot eval run \
    --suite suite.yaml \
    --generator demo \
    --report out.json

# 2. Gate the report against thresholds (exits non-zero on regression/violation).
querypilot eval check \
    --report out.json \
    --threshold 0.9 \
    --require-safety 1.0
