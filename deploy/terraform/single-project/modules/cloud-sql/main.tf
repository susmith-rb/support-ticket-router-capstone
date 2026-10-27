# deploy/terraform/single-project/modules/cloud-sql/main.tf
# Cloud SQL PostgreSQL 16 instance and database users for support ticket store.
# Implements REQ-STORE-001, REQ-STORE-002, REQ-STORE-003.

resource "google_sql_database_instance" "ticket_store" {
  name             = var.instance_name
  database_version = "POSTGRES_16"
  region           = var.region

  deletion_protection = var.deletion_protection

  settings {
    # PG16 defaults to ENTERPRISE_PLUS, which rejects custom tiers; capstone cost posture
    edition           = "ENTERPRISE"
    tier              = var.tier
    availability_type = var.availability_type
    disk_type         = "PD_SSD"
    disk_size         = 20
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
      # HARD INVARIANT: Empty authorized networks per org policy sql.restrictAuthorizedNetworks.
      # All client connections must route through Cloud SQL IAM Connectors / Auth Proxies.
    }

    backup_configuration {
      enabled                        = var.backup_enabled
      start_time                     = "04:00"
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = 7
        retention_unit   = "COUNT"
      }
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    user_labels = var.labels
  }
}

resource "google_sql_database" "support" {
  name      = var.database_name
  instance  = google_sql_database_instance.ticket_store.name
  charset   = "UTF8"
  collation = "en_US.UTF8"
}

# -----------------------------------------------------------------------------
# Service Accounts for Least-Privilege MCP Server and Queue Access
# -----------------------------------------------------------------------------

resource "google_service_account" "sa_ticket_mcp" {
  project      = var.project_id
  account_id   = "sa-ticket-mcp"
  display_name = "Ticket Store MCP service account"
}

resource "google_service_account" "sa_order_mcp" {
  project      = var.project_id
  account_id   = "sa-order-mcp"
  display_name = "Order Lookup MCP service account"
}

resource "google_service_account" "sa_queue" {
  project      = var.project_id
  account_id   = "sa-queue"
  display_name = "Human Queue MCP service account"
}

# -----------------------------------------------------------------------------
# Least-Privilege IAM Bindings (Cloud SQL Client & Instance User)
# -----------------------------------------------------------------------------

locals {
  db_service_accounts = {
    ticket_mcp = google_service_account.sa_ticket_mcp.email
    order_mcp  = google_service_account.sa_order_mcp.email
    queue      = google_service_account.sa_queue.email
  }

  db_sa_roles = [
    "roles/cloudsql.client",
    "roles/cloudsql.instanceUser",
  ]

  db_sa_role_pairs = flatten([
    for sa_key, sa_email in local.db_service_accounts : [
      for role in local.db_sa_roles : {
        key   = "${sa_key}-${role}"
        email = sa_email
        role  = role
      }
    ]
  ])
}

resource "google_project_iam_member" "db_sa_roles" {
  for_each = { for pair in local.db_sa_role_pairs : pair.key => pair }
  project  = var.project_id
  role     = each.value.role
  member   = "serviceAccount:${each.value.email}"
}

# -----------------------------------------------------------------------------
# Cloud SQL IAM Database Users
# Cloud SQL Postgres strips the .gserviceaccount.com domain suffix for IAM DB users.
# -----------------------------------------------------------------------------

resource "google_sql_user" "sa_ticket_mcp" {
  name     = trimsuffix(google_service_account.sa_ticket_mcp.email, ".gserviceaccount.com")
  instance = google_sql_database_instance.ticket_store.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

resource "google_sql_user" "sa_order_mcp" {
  name     = trimsuffix(google_service_account.sa_order_mcp.email, ".gserviceaccount.com")
  instance = google_sql_database_instance.ticket_store.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

resource "google_sql_user" "sa_queue" {
  name     = trimsuffix(google_service_account.sa_queue.email, ".gserviceaccount.com")
  instance = google_sql_database_instance.ticket_store.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

# -----------------------------------------------------------------------------
# Break-Glass Admin User & Secret Manager Storage
# -----------------------------------------------------------------------------

resource "random_password" "admin_password" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "google_sql_user" "admin" {
  name     = "postgres_admin"
  instance = google_sql_database_instance.ticket_store.name
  password = random_password.admin_password.result
  type     = "BUILT_IN"
}

resource "google_secret_manager_secret" "admin_password" {
  project   = var.project_id
  secret_id = "cloudsql-ticket-store-admin-password"

  replication {
    auto {}
  }

  labels = var.labels
}

resource "google_secret_manager_secret_version" "admin_password" {
  secret      = google_secret_manager_secret.admin_password.id
  secret_data = random_password.admin_password.result
}
