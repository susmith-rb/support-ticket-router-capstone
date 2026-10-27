# Ticket Store & Database Specification

This specification governs the database persistence layer and Cloud SQL PostgreSQL infrastructure for the Support Ticket Router & Assistant, linking to design requirements in `docs/TDD.md` §1.1, §3, §4.1, §5.1, §5.2, and §12.7.

---

## 1. Architectural Overview

Cloud SQL for PostgreSQL 16 serves as the authoritative system of record for support tickets, immutable audit event streams, synthetic order mocks, and time-bounded PII redaction maps.

### Data Model & Classification

| Table | Purpose | Security Class | Retention / TTL | Access Roles |
|---|---|---|---|---|
| `tickets` | Master ticket records with classification decisions & status | C2 Internal | 2-year operational | `ticket_rw` (CRUD), `queue_rw` (Select + column Update) |
| `ticket_events` | Immutable lifecycle audit trail | C2 Internal | 2-year operational | `ticket_rw` (Insert/Select), `queue_rw` (Insert/Select) |
| `orders_mock` | Mock e-commerce order state for order-lookup MCP tool | C2 Internal | Non-PII mock | `orders_ro` (Select with Row-Level Security) |
| `redaction_maps` | PII token to real entity mappings (pgcrypto encrypted) | C3 Confidential | 30-day TTL | `queue_rw` (Select for claimed tickets only) |

---

## 2. Requirements

### REQ-STORE-001: Cloud SQL PostgreSQL Instance & Database Configuration
- **Statement**: The ticket storage infrastructure MUST provision a managed PostgreSQL 16 instance named `ticket-store` in `us-central1` with database `support`, enforcing `deletion_protection = true`, automated daily backups with Point-in-Time Recovery (PITR), and Cloud SQL IAM database authentication (`cloudsql.iam_authentication = "on"`). In the capstone minimal cost posture, the instance defaults to zonal deployment (`availability_type = "ZONAL"`) and tier `db-custom-1-3840` (with regional HA and custom machine tiers parameterized via Terraform variables).
- **Rationale**: TDD §1.1, §4.1, and §12.7. PostgreSQL 16 provides JSONB indexing, Row-Level Security, and IAM database authentication required for zero-trust microservice access.
- **Acceptance**: `tests/infra/test_cloud_sql_static.py::test_cloud_sql_instance_configuration`
- **Status**: done
- **Owner**: store

### REQ-STORE-002: Public IP Security Invariant and Connector Enclosure
- **Statement**: In compliance with organization policy `sql.restrictAuthorizedNetworks` and the zero-trust connectivity model, the Cloud SQL instance MUST enforce an empty `authorized_networks` list and mandate SSL/TLS encryption (`ssl_mode = "ENCRYPTED_ONLY"`). All database connections from microservices (Cloud Run, GKE Autopilot, VM, local developers) MUST connect through the Cloud SQL Python Connector or Cloud SQL Auth Proxy over IAM-authenticated TLS.
- **Rationale**: TDD §5.1. Argolis org policy enforces `sql.restrictAuthorizedNetworks` while permitting public IP. Direct public IP access without the connector is strictly prohibited.
- **Acceptance**: `tests/infra/test_cloud_sql_static.py::test_cloud_sql_network_security`
- **Status**: done
- **Owner**: store

### REQ-STORE-003: Least-Privilege IAM Database Service Accounts and Break-Glass Credentials
- **Statement**: Dedicated service accounts (`sa-ticket-mcp`, `sa-order-mcp`, `sa-queue`) MUST be provisioned with project roles `roles/cloudsql.client` and `roles/cloudsql.instanceUser`, and registered as Cloud SQL IAM database users with `type = "CLOUD_IAM_SERVICE_ACCOUNT"` (omitting the `.gserviceaccount.com` domain suffix). A break-glass administrative user (`postgres_admin`) MUST have its randomized high-entropy password stored in Google Cloud Secret Manager (`cloudsql-ticket-store-admin-password`). No developer or application SA may be granted `roles/owner` or `roles/editor`.
- **Rationale**: TDD §5.1 least-privilege matrix, zero-key policy (`iam.disableServiceAccountKeyCreation`), and Secret Manager credential isolation.
- **Acceptance**: `tests/infra/test_cloud_sql_static.py::test_cloud_sql_iam_and_secret`
- **Status**: done
- **Owner**: store

### REQ-STORE-004: Idempotent Database Schema and Row-Level Security (RLS)
- **Statement**: Database schema initialization (`db/migrations/0001_init.sql`) MUST be idempotent, creating tables `tickets`, `ticket_events`, `orders_mock`, and `redaction_maps` (with 30-day default TTL). Tables MUST define database roles (`orders_ro`, `ticket_rw`, `queue_rw`). Table `orders_mock` MUST enforce Postgres Row-Level Security (RLS) such that `orders_ro` can only SELECT rows where `customer_id = nullif(current_setting('app.customer_id', true), '')`. Role `ticket_rw` MUST NOT have access to `redaction_maps`. Role `queue_rw` MUST have column-level UPDATE permissions on `tickets` restricted to `status`, `assigned_to`, `updated_at`, and `resolution_notes`.
- **Rationale**: TDD §3, §5.1, and §5.2 data classification (C2 Internal vs C3 Confidential separation).
- **Acceptance**: `tests/infra/test_cloud_sql_static.py::test_migration_schema_and_rls`
- **Status**: done
- **Owner**: store

### REQ-STORE-005: Deterministic Synthetic Order Mock Seed Data
- **Statement**: The database seed script (`db/seed/orders_mock.sql`) MUST generate at least 200 deterministic, synthetic retail order rows for `orders_mock` containing pseudonymous customer IDs (`cust_1001`..`cust_1050`), diverse statuses (`PLACED`, `PROCESSING`, `SHIPPED`, `DELIVERED`, `CANCELLED`, `REFUNDED`), realistic items, and tracking numbers, without any unmasked real customer PII.
- **Rationale**: TDD §4 and §6 for order-lookup MCP verification and offline testing.
- **Acceptance**: `tests/infra/test_cloud_sql_static.py::test_seed_orders_mock_integrity`
- **Status**: done
- **Owner**: store

### REQ-STORE-006: Repository Abstraction Layer and Test Fakes
- **Statement**: Package `ticket_router.store` MUST provide domain models (`Ticket`, `TicketCreate`, `TicketEvent`, `Order`, `RedactionMap`), repository interfaces (`TicketRepository`, `OrderRepository`), in-memory fakes (`FakeTicketRepo`, `FakeOrderRepo`), and concrete PostgreSQL implementations using `cloud-sql-python-connector` + `pg8000`. `TicketRepo.create_ticket` MUST be idempotent on duplicate ticket IDs. `TicketRepo.claim_ticket` MUST enforce optimistic concurrency to prevent dual-agent claiming. `OrderRepo.get_order` MUST set transaction-local `app.customer_id` ensuring tenant isolation.
- **Rationale**: TDD §7.1 and §1.1 for unit testing without live DB dependencies.
- **Acceptance**: `tests/store/test_repo.py::test_ticket_repo_crud_and_idempotency`
- **Status**: done
- **Owner**: store

### REQ-STORE-007: Database Migration Execution Tooling
- **Statement**: A migration runner script (`scripts/db_migrate.sh`) MUST execute versioned SQL migrations idempotently against Cloud SQL using the Cloud SQL Python Connector or Cloud Run job invocation, with dry-run and verbose logging options.
- **Rationale**: TDD §12.7 CI/CD and deployment lifecycle.
- **Acceptance**: `manual: scripts/db_migrate.sh --help`
- **Status**: done
- **Owner**: store

### REQ-STORE-008: Human Operator Read-Only Access via IAM Database Authentication
- **Statement**: Human operators (including developers and analysts) MUST be provisioned with read-only access to Cloud SQL using Cloud IAM Database Authentication (`type = "CLOUD_IAM_USER"`). The Terraform module `cloud-sql` MUST configure `google_sql_user` for each user in `human_iam_users` (defaulting to `["susmith@gcp.altostrat.com"]` in root `cloud_sql_human_iam_users`), and grant project IAM roles `roles/cloudsql.instanceUser`, `roles/cloudsql.client`, and `roles/cloudsql.studioUser`. Database migration `0002_human_access.sql` MUST define role `analyst_ro` with SELECT grants on `tickets`, `ticket_events`, and `orders_mock` (strictly excluding `redaction_maps`), configure an RLS policy `analyst_read_all` on `orders_mock`, and grant `analyst_ro` to all human IAM database users. All administrative and write operations remain restricted to break-glass credentials.
- **Rationale**: Enables human operators to inspect ticket status and query mock orders via Cloud SQL Studio in the Google Cloud Console or Cloud SQL Auth Proxy with `--auto-iam-authn`, while strictly protecting customer PII in `redaction_maps`.
- **Acceptance**: `tests/infra/test_cloud_sql_static.py::test_human_iam_database_access`
- **Status**: done
- **Owner**: store

