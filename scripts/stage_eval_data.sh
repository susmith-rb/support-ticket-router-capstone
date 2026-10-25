#!/usr/bin/env bash
# scripts/stage_eval_data.sh — Prepares and stages DEV-only evaluation data for CI/CD gates.
#
# Usage:
#   ./scripts/stage_eval_data.sh [--dry-run] [--upload] [--bucket BUCKET] [--out-dir DIR]
#
# HARD INVARIANT: Only DEV queries (d-prefixed) and multi-annotator labels are staged.
# Test queries (t-prefixed) and test split rows are strictly rejected.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
if [ ! -f "${PYTHON_BIN}" ]; then
  PYTHON_BIN="python3"
fi

exec "${PYTHON_BIN}" "${REPO_ROOT}/scripts/stage_eval_data.py" "$@"
