#!/usr/bin/env bash
# scripts/db_migrate.sh - Database migration runner for Cloud SQL PostgreSQL.
# Implements REQ-STORE-007: Idempotent migration execution against Cloud SQL.
#
# Supports IAM database authentication (default) or built-in password auth.
# Can be run locally, on the Workbench VM, or in Cloud Build / Cloud Run jobs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_ID="${PROJECT_ID:-susmith-fde-ticket-router}"
REGION="${REGION:-us-central1}"
INSTANCE_NAME="${INSTANCE_NAME:-ticket-store}"
DATABASE_NAME="${DATABASE_NAME:-support}"
DB_USER="${DB_USER:-postgres_admin}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-${REPO_ROOT}/db/migrations}"
SEED_DIR="${SEED_DIR:-${REPO_ROOT}/db/seed}"

DRY_RUN=false
APPLY_SEED=false

usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --project PROJECT_ID     GCP Project ID (default: ${PROJECT_ID})
  --region REGION          Cloud SQL Region (default: ${REGION})
  --instance INSTANCE      Cloud SQL Instance Name (default: ${INSTANCE_NAME})
  --database DATABASE      Database Name (default: ${DATABASE_NAME})
  --user USER              Database User (default: ${DB_USER})
  --seed                   Apply seed data from db/seed/ after migrations
  --dry-run                Print migration files without applying them
  --help                   Display this help message
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)
            PROJECT_ID="$2"
            shift 2
            ;;
        --region)
            REGION="$2"
            shift 2
            ;;
        --instance)
            INSTANCE_NAME="$2"
            shift 2
            ;;
        --database)
            DATABASE_NAME="$2"
            shift 2
            ;;
        --user)
            DB_USER="$2"
            shift 2
            ;;
        --seed)
            APPLY_SEED=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            usage
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            ;;
    esac
done

echo "================================================================="
echo "Cloud SQL Ticket Store Migration Runner"
echo "Target Instance: ${PROJECT_ID}:${REGION}:${INSTANCE_NAME}"
echo "Database:        ${DATABASE_NAME}"
echo "Migrations Dir:  ${MIGRATIONS_DIR}"
echo "Dry Run:         ${DRY_RUN}"
echo "================================================================="

if [[ ! -d "${MIGRATIONS_DIR}" ]]; then
    echo "Error: Migrations directory not found: ${MIGRATIONS_DIR}" >&2
    exit 1
fi

MIGRATION_FILES=($(find "${MIGRATIONS_DIR}" -maxdepth 1 -name "*.sql" | sort))

if [[ ${#MIGRATION_FILES[@]} -eq 0 ]]; then
    echo "No migration files found in ${MIGRATIONS_DIR}."
    exit 0
fi

echo "Discovered ${#MIGRATION_FILES[@]} migration file(s):"
for f in "${MIGRATION_FILES[@]}"; do
    echo "  - $(basename "$f")"
done

if [[ "${DRY_RUN}" == "true" ]]; then
    echo "Dry run complete. No changes applied."
    exit 0
fi

# Run migrations via python migration runner
PYTHON_EXEC="python3"
if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON_EXEC="${REPO_ROOT}/.venv/bin/python"
fi

export CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false

"${PYTHON_EXEC}" "${SCRIPT_DIR}/migrate.py" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --instance "${INSTANCE_NAME}" \
    --database "${DATABASE_NAME}" \
    --user "${DB_USER}" \
    --migrations-dir "${MIGRATIONS_DIR}" \
    $([ "${APPLY_SEED}" == "true" ] && echo "--seed-dir ${SEED_DIR}")

echo "Migration run completed successfully."
