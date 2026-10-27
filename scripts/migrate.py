#!/usr/bin/env python3
"""scripts/migrate.py - Database migration and seed executor for Cloud SQL.

Implements REQ-STORE-007: Executes SQL files in alphanumeric order against
Cloud SQL PostgreSQL via cloud-sql-python-connector + pg8000.
Supports password resolution from env or Secret Manager, custom OAuth tokens,
and comprehensive post-migration verification.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate")

DEFAULT_SECRET_ID = "cloudsql-ticket-store-admin-password"


def parse_args():
    parser = argparse.ArgumentParser(description="Cloud SQL Migration Runner")
    parser.add_argument("--project", default="susmith-fde-ticket-router", help="GCP project ID")
    parser.add_argument("--region", default="us-central1", help="Cloud SQL region")
    parser.add_argument("--instance", default="ticket-store", help="Cloud SQL instance ID")
    parser.add_argument("--database", default="support", help="Database name")
    parser.add_argument("--user", default="postgres_admin", help="Database user")
    parser.add_argument("--password", default=None, help="Database password (defaults to DB_PASSWORD env or Secret Manager)")
    parser.add_argument(
        "--password-secret",
        default=DEFAULT_SECRET_ID,
        help="Secret Manager secret ID for database password",
    )
    parser.add_argument(
        "--password-secret-version",
        default="latest",
        help="Secret Manager secret version",
    )
    parser.add_argument(
        "--migrations-dir",
        default=None,
        help="Directory containing .sql migrations",
    )
    parser.add_argument("--seed-dir", default=None, help="Directory containing .sql seed files")
    parser.add_argument("--dry-run", action="store_true", help="Print statements without executing")
    parser.add_argument("--verify-only", action="store_true", help="Skip migrations and run verification only")
    return parser.parse_args()


def resolve_password(
    cli_password: Optional[str] = None,
    secret_id: Optional[str] = DEFAULT_SECRET_ID,
    project_id: str = "susmith-fde-ticket-router",
    version: str = "latest",
) -> Optional[str]:
    """Resolve database password: CLI arg > DB_PASSWORD env > Secret Manager REST.

    Never logs the resolved password.
    """
    if cli_password:
        return cli_password

    env_password = os.environ.get("DB_PASSWORD")
    if env_password:
        return env_password

    target_secret = secret_id or DEFAULT_SECRET_ID
    logger.info("Resolving database password from Secret Manager secret: %s", target_secret)

    try:
        from ticket_router.common.auth import get_access_token

        token = get_access_token()
    except Exception as e:
        logger.warning("Could not obtain access token for Secret Manager: %s", e)
        return None

    url = (
        f"https://secretmanager.googleapis.com/v1/projects/{project_id}/"
        f"secrets/{target_secret}/versions/{version}:access"
    )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            payload_b64 = data.get("payload", {}).get("data", "")
            if payload_b64:
                logger.info("Successfully retrieved password from Secret Manager.")
                return base64.b64decode(payload_b64).decode("utf-8")
        else:
            logger.warning(
                "Secret Manager returned HTTP %d for secret %s",
                resp.status_code,
                target_secret,
            )
    except Exception as e:
        logger.warning("Failed to access Secret Manager secret %s: %s", target_secret, e)

    return None


def get_connector(oauth_token: Optional[str] = None):
    """Initialize Cloud SQL Connector with optional short-lived OAuth token credentials."""
    try:
        from google.cloud.sql.connector import Connector
    except ImportError:
        raise ImportError(
            "Missing database dependencies (cloud-sql-python-connector). "
            "Install with: pip install '.[db]'"
        )

    token = oauth_token or os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
    if token:
        logger.info("Initializing Cloud SQL connector with GOOGLE_OAUTH_ACCESS_TOKEN")
        try:
            from google.oauth2.credentials import Credentials as OAuthCredentials
        except ImportError:
            from google.auth.credentials import Credentials as OAuthCredentials

        creds = OAuthCredentials(token=token)
        return Connector(credentials=creds)

    return Connector()


def load_sql_files(directory: Optional[Path]) -> List[Path]:
    if not directory or not directory.is_dir():
        return []
    return sorted(directory.glob("*.sql"))


def execute_sql_file(conn, sql_file: Path, dry_run: bool = False):
    logger.info("Executing migration: %s", sql_file.name)
    sql_text = sql_file.read_text(encoding="utf-8")
    if dry_run:
        logger.info("[DRY RUN] Would execute %d characters of SQL from %s", len(sql_text), sql_file.name)
        return

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


def verify_database(conn) -> Dict[str, Any]:
    """Verify tables, roles, RLS policy, and IAM role memberships.

    Raises RuntimeError if any required item is missing.
    """
    logger.info("-----------------------------------------------------------------")
    logger.info("Starting Cloud SQL Database Verification...")
    logger.info("-----------------------------------------------------------------")

    cursor = conn.cursor()
    report: Dict[str, Any] = {}

    try:
        # 1. Tables present
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public';")
        tables = {row[0] for row in cursor.fetchall()}
        report["tables"] = sorted(tables)
        logger.info("Discovered tables: %s", report["tables"])

        expected_tables = {"tickets", "ticket_events", "orders_mock", "redaction_maps"}
        missing_tables = expected_tables - tables
        if missing_tables:
            raise RuntimeError(f"Missing required table(s): {missing_tables}")

        # 2. Roles present
        cursor.execute("SELECT rolname FROM pg_roles;")
        roles = {row[0] for row in cursor.fetchall()}
        report["roles"] = sorted(roles)
        expected_roles = {"orders_ro", "ticket_rw", "queue_rw"}
        missing_roles = expected_roles - roles
        if missing_roles:
            raise RuntimeError(f"Missing required database role(s): {missing_roles}")
        logger.info("Required roles verified: orders_ro, ticket_rw, queue_rw")

        # 3. RLS enabled on orders_mock (pg_class.relrowsecurity)
        cursor.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'orders_mock';")
        row = cursor.fetchone()
        rls_enabled = bool(row and row[0])
        report["orders_mock_rls"] = rls_enabled
        logger.info("orders_mock RLS enabled (relrowsecurity): %s", rls_enabled)
        if not rls_enabled:
            raise RuntimeError("Postgres Row-Level Security is NOT enabled on table orders_mock")

        # 4. Row count on orders_mock
        cursor.execute("SELECT count(*) FROM orders_mock;")
        order_count = cursor.fetchone()[0]
        report["orders_mock_count"] = order_count
        logger.info("orders_mock row count: %d", order_count)

        # 5. Role memberships of IAM users (pg_auth_members)
        query = """
        SELECT r.rolname AS role_name, m.rolname AS member_name
        FROM pg_auth_members am
        JOIN pg_roles r ON r.oid = am.roleid
        JOIN pg_roles m ON m.oid = am.member
        WHERE r.rolname IN ('orders_ro', 'ticket_rw', 'queue_rw');
        """
        cursor.execute(query)
        memberships: Dict[str, List[str]] = {"orders_ro": [], "ticket_rw": [], "queue_rw": []}
        for role_name, member_name in cursor.fetchall():
            if role_name in memberships:
                memberships[role_name].append(member_name)
        report["memberships"] = memberships

        logger.info("IAM Role Memberships:")
        for r_name, members in memberships.items():
            logger.info("  - %s: %s", r_name, members)

        # Ensure IAM DB users (when present in pg_roles) have been granted their respective roles
        iam_users = [r for r in roles if "sa-" in r or ".iam" in r or "mcp" in r]
        if iam_users:
            logger.info("Detected IAM database user(s) in pg_roles: %s", iam_users)
            for user in iam_users:
                if "order-mcp" in user:
                    if user not in memberships["orders_ro"]:
                        raise RuntimeError(f"IAM user {user} is not granted orders_ro!")
                elif "ticket-mcp" in user:
                    if user not in memberships["ticket_rw"]:
                        raise RuntimeError(f"IAM user {user} is not granted ticket_rw!")
                elif "queue" in user:
                    if user not in memberships["queue_rw"]:
                        raise RuntimeError(f"IAM user {user} is not granted queue_rw!")

        logger.info("Verification passed: all tables, roles, RLS, and memberships are valid.")
        return report
    finally:
        cursor.close()


def main():
    args = parse_args()
    repo_root = Path(__file__).resolve().parent.parent

    migrations_dir = (
        Path(args.migrations_dir)
        if args.migrations_dir
        else repo_root / "db" / "migrations"
    )
    seed_dir = Path(args.seed_dir) if args.seed_dir else repo_root / "db" / "seed"

    migration_files = load_sql_files(migrations_dir)
    seed_files = load_sql_files(seed_dir) if args.seed_dir else []

    if args.dry_run:
        logger.info("DRY RUN mode activated.")
        for f in migration_files:
            execute_sql_file(None, f, dry_run=True)
        for f in seed_files:
            execute_sql_file(None, f, dry_run=True)
        return 0

    try:
        from google.cloud.sql.connector import IPTypes
    except ImportError:
        logger.error(
            "Missing database dependencies (cloud-sql-python-connector, pg8000). "
            "Install with: pip install '.[db]'"
        )
        return 1

    # Resolve password for non-IAM database user (built-in postgres_admin)
    is_iam_user = "sa-" in args.user or "mcp" in args.user or ".iam" in args.user
    db_password = None
    if not is_iam_user:
        db_password = resolve_password(
            cli_password=args.password,
            secret_id=args.password_secret,
            project_id=args.project,
            version=args.password_secret_version,
        )
        if not db_password:
            logger.warning("No password found for built-in user %s.", args.user)

    instance_connection_name = f"{args.project}:{args.region}:{args.instance}"
    logger.info("Connecting to Cloud SQL instance: %s (db: %s, user: %s)", instance_connection_name, args.database, args.user)

    connector = get_connector()

    def get_conn():
        return connector.connect(
            instance_connection_name,
            "pg8000",
            user=args.user,
            password=db_password,
            db=args.database,
            enable_iam_auth=is_iam_user,
            ip_type=IPTypes.PUBLIC,
        )

    conn = get_conn()
    try:
        if not args.verify_only:
            logger.info("Applying %d migration file(s)...", len(migration_files))
            for f in migration_files:
                execute_sql_file(conn, f, dry_run=False)

            if seed_files:
                logger.info("Applying %d seed file(s)...", len(seed_files))
                for f in seed_files:
                    execute_sql_file(conn, f, dry_run=False)

        # Verification step
        verify_database(conn)
    finally:
        conn.close()
        connector.close()

    logger.info("Migration & verification run completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
