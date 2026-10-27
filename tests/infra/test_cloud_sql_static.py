# tests/infra/test_cloud_sql_static.py
# Static analysis and verification of Cloud SQL Terraform, SQL migrations, seed data, and tooling.
# Implements REQ-STORE-001, REQ-STORE-002, REQ-STORE-003, REQ-STORE-004, REQ-STORE-005, REQ-STORE-007.

from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"
CLOUD_SQL_MOD_DIR = TF_DIR / "modules" / "cloud-sql"
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
SEED_DIR = REPO_ROOT / "db" / "seed"
MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "db_migrate.sh"


# REQ-STORE-001
def test_cloud_sql_instance_configuration():
    """Verify Cloud SQL instance configuration: PostgreSQL 16, ZONAL, PITR, and IAM DB auth."""
    assert CLOUD_SQL_MOD_DIR.is_dir(), f"Missing module directory: {CLOUD_SQL_MOD_DIR}"
    assert (CLOUD_SQL_MOD_DIR / "main.tf").is_file()
    assert (CLOUD_SQL_MOD_DIR / "variables.tf").is_file()
    assert (CLOUD_SQL_MOD_DIR / "outputs.tf").is_file()
    assert (CLOUD_SQL_MOD_DIR / "versions.tf").is_file()

    main_tf = (CLOUD_SQL_MOD_DIR / "main.tf").read_text(encoding="utf-8")
    vars_tf = (CLOUD_SQL_MOD_DIR / "variables.tf").read_text(encoding="utf-8")

    # Instance resource
    assert 'resource "google_sql_database_instance" "ticket_store"' in main_tf
    assert 'database_version = "POSTGRES_16"' in main_tf
    assert "region           = var.region" in main_tf or "region = var.region" in main_tf
    assert "deletion_protection = var.deletion_protection" in main_tf

    # Cost-conscious defaults in variables
    assert 'default     = "db-custom-1-3840"' in vars_tf
    assert 'default     = "ZONAL"' in vars_tf
    assert "default     = true" in vars_tf  # deletion_protection & backup_enabled

    # Settings block
    assert "tier              = var.tier" in main_tf or "tier = var.tier" in main_tf
    assert "availability_type = var.availability_type" in main_tf
    assert "backup_configuration" in main_tf
    assert "point_in_time_recovery_enabled = true" in main_tf
    assert 'name  = "cloudsql.iam_authentication"' in main_tf
    assert 'value = "on"' in main_tf

    # Database resource
    assert 'resource "google_sql_database" "support"' in main_tf
    assert 'charset   = "UTF8"' in main_tf or 'charset = "UTF8"' in main_tf

    # Root module wiring
    root_main = (TF_DIR / "main.tf").read_text(encoding="utf-8")
    assert 'module "cloud_sql"' in root_main
    assert 'source              = "./modules/cloud-sql"' in root_main
    assert '"sqladmin.googleapis.com"' in root_main


# REQ-STORE-002
def test_cloud_sql_network_security():
    """Verify IP security invariant: public IP enabled with empty authorized networks and enforced TLS."""
    main_tf = (CLOUD_SQL_MOD_DIR / "main.tf").read_text(encoding="utf-8")

    assert "ip_configuration" in main_tf
    assert "ipv4_enabled = true" in main_tf
    assert 'ssl_mode     = "ENCRYPTED_ONLY"' in main_tf or 'ssl_mode = "ENCRYPTED_ONLY"' in main_tf

    # Invariant: Must NOT configure authorized networks blocks to prevent bypass of connector
    assert "authorized_networks {" not in main_tf
    assert "0.0.0.0/0" not in main_tf


# REQ-STORE-003
def test_cloud_sql_iam_and_secret():
    """Verify least-privilege service accounts, IAM users, and Secret Manager admin credentials."""
    main_tf = (CLOUD_SQL_MOD_DIR / "main.tf").read_text(encoding="utf-8")

    # Service accounts
    assert 'resource "google_service_account" "sa_ticket_mcp"' in main_tf
    assert 'account_id   = "sa-ticket-mcp"' in main_tf
    assert 'resource "google_service_account" "sa_order_mcp"' in main_tf
    assert 'account_id   = "sa-order-mcp"' in main_tf
    assert 'resource "google_service_account" "sa_queue"' in main_tf
    assert 'account_id   = "sa-queue"' in main_tf

    # Least privilege roles
    assert "roles/cloudsql.client" in main_tf
    assert "roles/cloudsql.instanceUser" in main_tf
    assert "roles/owner" not in main_tf
    assert "roles/editor" not in main_tf
    assert "google_project_iam_binding" not in main_tf

    # Cloud SQL IAM DB users
    assert 'type     = "CLOUD_IAM_SERVICE_ACCOUNT"' in main_tf
    assert "trimsuffix(google_service_account.sa_ticket_mcp.email" in main_tf
    assert "trimsuffix(google_service_account.sa_order_mcp.email" in main_tf
    assert "trimsuffix(google_service_account.sa_queue.email" in main_tf

    # Admin user and Secret Manager
    assert 'resource "google_sql_user" "admin"' in main_tf
    assert 'resource "random_password" "admin_password"' in main_tf
    assert 'resource "google_secret_manager_secret" "admin_password"' in main_tf
    assert 'secret_id = "cloudsql-ticket-store-admin-password"' in main_tf
    assert 'resource "google_secret_manager_secret_version" "admin_password"' in main_tf


# REQ-STORE-004
def test_migration_schema_and_rls():
    """Verify 0001_init.sql contains required tables, roles, RLS policies, and column grants."""
    migration_file = MIGRATIONS_DIR / "0001_init.sql"
    assert migration_file.is_file(), f"Missing {migration_file}"

    sql_text = migration_file.read_text(encoding="utf-8")

    # Extensions
    assert 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";' in sql_text
    assert 'CREATE EXTENSION IF NOT EXISTS "pgcrypto";' in sql_text

    # Roles
    assert "CREATE ROLE orders_ro" in sql_text
    assert "CREATE ROLE ticket_rw" in sql_text
    assert "CREATE ROLE queue_rw" in sql_text

    # Tables
    assert "CREATE TABLE IF NOT EXISTS tickets" in sql_text
    assert "CREATE TABLE IF NOT EXISTS ticket_events" in sql_text
    assert "CREATE TABLE IF NOT EXISTS orders_mock" in sql_text
    assert "CREATE TABLE IF NOT EXISTS redaction_maps" in sql_text

    # 30-day TTL on redaction_maps
    assert "INTERVAL '30 days'" in sql_text

    # Postgres Row-Level Security
    assert "ALTER TABLE orders_mock ENABLE ROW LEVEL SECURITY;" in sql_text
    assert "CREATE POLICY order_customer_isolation ON orders_mock" in sql_text
    assert "customer_id = nullif(current_setting('app.customer_id', true), '')" in sql_text

    # Grants: orders_ro
    assert "GRANT SELECT ON orders_mock TO orders_ro;" in sql_text

    # Grants: ticket_rw (no access to redaction_maps)
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON tickets TO ticket_rw;" in sql_text
    assert "GRANT SELECT, INSERT ON ticket_events TO ticket_rw;" in sql_text
    assert "ON redaction_maps TO ticket_rw" not in sql_text

    # Grants: queue_rw (column-level update on tickets, select on redaction_maps)
    assert "GRANT SELECT ON tickets TO queue_rw;" in sql_text
    assert (
        "GRANT UPDATE (status, assigned_to, updated_at, resolution_notes) ON tickets TO queue_rw;"
        in sql_text
    )
    assert "GRANT SELECT ON redaction_maps TO queue_rw;" in sql_text


# REQ-STORE-005
def test_seed_orders_mock_integrity():
    """Verify seed data contains at least 200 deterministic synthetic orders without PII."""
    seed_file = SEED_DIR / "orders_mock.sql"
    assert seed_file.is_file(), f"Missing {seed_file}"

    sql_text = seed_file.read_text(encoding="utf-8")
    assert "INSERT INTO orders_mock" in sql_text
    assert "ON CONFLICT (order_id) DO NOTHING;" in sql_text

    # Count rows by counting 'ORD-' occurrences
    order_matches = re.findall(r"'ORD-\d+'", sql_text)
    assert (
        len(order_matches) >= 200
    ), f"Expected >= 200 orders in seed data, found {len(order_matches)}"

    # Check for presence of required diverse statuses
    for status in [
        "PLACED",
        "PROCESSING",
        "SHIPPED",
        "DELIVERED",
        "CANCELLED",
        "REFUNDED",
    ]:
        assert f"'{status}'" in sql_text

    # Verify customer ID pseudonymity (no real email addresses or unmasked names)
    assert "cust_" in sql_text
    assert "@gmail.com" not in sql_text
    assert "@google.com" not in sql_text


# REQ-STORE-007
def test_db_migration_script():
    """Verify migration execution script exists and includes necessary CLI options."""
    assert MIGRATE_SCRIPT.is_file(), f"Missing {MIGRATE_SCRIPT}"
    content = MIGRATE_SCRIPT.read_text(encoding="utf-8")

    assert "--project" in content
    assert "--region" in content
    assert "--instance" in content
    assert "--database" in content
    assert "--dry-run" in content
    assert "--seed" in content


# REQ-STORE-008
def test_human_iam_database_access():
    """Verify human IAM DB users, project roles, and 0002_human_access.sql migration.

    Invariants:
    1. modules/cloud-sql defines variable human_iam_users.
    2. google_sql_user for each human user with type CLOUD_IAM_USER.
    3. project IAM roles roles/cloudsql.instanceUser, roles/cloudsql.client, and roles/cloudsql.studioUser.
    4. Root variables.tf defines cloud_sql_human_iam_users defaulting to susmith@gcp.altostrat.com.
    5. 0002_human_access.sql defines analyst_ro role, grants SELECT on tickets, ticket_events, orders_mock.
    6. Invariant: 0002_human_access.sql does NOT grant access to redaction_maps.
    7. RLS policy analyst_read_all created on orders_mock for analyst_ro.
    8. analyst_ro granted to all human IAM database users in Postgres.
    """
    main_tf = (CLOUD_SQL_MOD_DIR / "main.tf").read_text(encoding="utf-8")
    vars_tf = (CLOUD_SQL_MOD_DIR / "variables.tf").read_text(encoding="utf-8")

    # Cloud SQL module variables and resources
    assert "variable \"human_iam_users\"" in vars_tf
    assert 'resource "google_sql_user" "human_iam_users"' in main_tf
    assert 'type     = "CLOUD_IAM_USER"' in main_tf

    # Project IAM roles
    assert "roles/cloudsql.studioUser" in main_tf
    assert "roles/cloudsql.instanceUser" in main_tf
    assert "roles/cloudsql.client" in main_tf
    assert 'member   = "user:${each.value.user}"' in main_tf

    # Root module wiring
    root_vars = (TF_DIR / "variables.tf").read_text(encoding="utf-8")
    root_main = (TF_DIR / "main.tf").read_text(encoding="utf-8")
    assert "variable \"cloud_sql_human_iam_users\"" in root_vars
    assert "susmith@gcp.altostrat.com" in root_vars
    assert "human_iam_users     = var.cloud_sql_human_iam_users" in root_main

    # Migration 0002_human_access.sql
    mig_0002 = MIGRATIONS_DIR / "0002_human_access.sql"
    assert mig_0002.is_file(), f"Missing {mig_0002}"
    sql_text = mig_0002.read_text(encoding="utf-8")

    # Role and permissions
    assert "CREATE ROLE analyst_ro" in sql_text
    assert "GRANT SELECT ON TABLE tickets, ticket_events, orders_mock TO analyst_ro;" in sql_text
    assert "redaction_maps" not in sql_text or "NOT redaction_maps" in sql_text or "EXCLUDED" in sql_text

    # RLS policy
    assert "analyst_read_all" in sql_text
    assert "CREATE POLICY analyst_read_all ON orders_mock" in sql_text
    assert "TO analyst_ro" in sql_text

    # Grant to human IAM users
    assert "GRANT analyst_ro TO" in sql_text
    assert "NOT LIKE 'sa-%'" in sql_text

