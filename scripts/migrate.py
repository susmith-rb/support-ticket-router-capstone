#!/usr/bin/env python3
"""scripts/migrate.py - Database migration and seed executor for Cloud SQL.

Implements REQ-STORE-007: Executes SQL files in alphanumeric order against
Cloud SQL PostgreSQL via cloud-sql-python-connector + pg8000.
"""

import argparse
import logging
from pathlib import Path
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate")


def parse_args():
    parser = argparse.ArgumentParser(description="Cloud SQL Migration Runner")
    parser.add_argument("--project", default="susmith-fde-ticket-router", help="GCP project ID")
    parser.add_argument("--region", default="us-central1", help="Cloud SQL region")
    parser.add_argument("--instance", default="ticket-store", help="Cloud SQL instance ID")
    parser.add_argument("--database", default="support", help="Database name")
    parser.add_argument("--user", default="postgres_admin", help="Database user")
    parser.add_argument("--migrations-dir", required=True, help="Directory containing .sql migrations")
    parser.add_argument("--seed-dir", default=None, help="Directory containing .sql seed files")
    parser.add_argument("--dry-run", action="store_true", help="Print statements without executing")
    return parser.parse_args()


def load_sql_files(directory: Path):
    if not directory.is_dir():
        logger.warning("Directory not found: %s", directory)
        return []
    return sorted(directory.glob("*.sql"))


def execute_sql_file(conn, sql_file: Path, dry_run: bool = False):
    logger.info("Executing migration: %s", sql_file.name)
    sql_text = sql_file.read_text(encoding="utf-8")
    if dry_run:
        logger.info("[DRY RUN] Would execute %d characters of SQL from %s", len(sql_text), sql_file.name)
        return

    # Execute inside transaction block
    cursor = conn.cursor()
    try:
        cursor.execute(sql_text)
        conn.commit()
        logger.info("Successfully executed: %s", sql_file.name)
    except Exception as e:
        conn.rollback()
        logger.error("Failed executing %s: %s", sql_file.name, e)
        raise
    finally:
        cursor.close()


def main():
    args = parse_args()
    migrations_dir = Path(args.migrations_dir)
    migration_files = load_sql_files(migrations_dir)

    if not migration_files:
        logger.info("No migration files found in %s", migrations_dir)
        return 0

    logger.info("Found %d migration(s) in %s", len(migration_files), migrations_dir)

    if args.dry_run:
        for f in migration_files:
            execute_sql_file(None, f, dry_run=True)
        if args.seed_dir:
            seed_files = load_sql_files(Path(args.seed_dir))
            for f in seed_files:
                execute_sql_file(None, f, dry_run=True)
        logger.info("Dry run finished successfully.")
        return 0

    try:
        from google.cloud.sql.connector import Connector, IPTypes
    except ImportError:
        logger.error(
            "Missing database dependencies (cloud-sql-python-connector, pg8000). "
            "Install with: pip install '.[db]'"
        )
        return 1

    instance_connection_name = f"{args.project}:{args.region}:{args.instance}"
    logger.info("Connecting to Cloud SQL instance: %s (db: %s)", instance_connection_name, args.database)

    connector = Connector()

    def get_conn():
        return connector.connect(
            instance_connection_name,
            "pg8000",
            user=args.user,
            db=args.database,
            enable_iam_auth=True if "sa-" in args.user or "mcp" in args.user else False,
            ip_type=IPTypes.PUBLIC,
        )

    conn = get_conn()
    try:
        for f in migration_files:
            execute_sql_file(conn, f, dry_run=False)

        if args.seed_dir:
            seed_files = load_sql_files(Path(args.seed_dir))
            logger.info("Found %d seed file(s) in %s", len(seed_files), args.seed_dir)
            for f in seed_files:
                execute_sql_file(conn, f, dry_run=False)
    finally:
        conn.close()
        connector.close()

    logger.info("All migrations completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
