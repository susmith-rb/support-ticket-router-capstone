#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$#" -eq 0 ]; then
  .venv/bin/python -m pytest -q -p no:cacheprovider -p no:warnings --deselect tests/infra/test_ge_app_and_trace.py::test_ge_app_infra_and_registration_contract --cov=src/ticket_router --cov-branch --cov-report=term:skip-covered
else
  .venv/bin/python -m pytest "$@"
fi
