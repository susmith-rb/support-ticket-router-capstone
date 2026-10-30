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
| `redaction_maps` | PII token to real value mappings (KMS envelope-encrypted, REQ-STORE-009) | C3 Confidential | 30-day TTL, purged daily (REQ-STORE-010) | `ticket_rw` (Insert only), `queue_rw` (Select for claimed tickets, Delete expired) |

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

### REQ-STORE-009: Reversible PII Tokens and KMS Envelope-Encrypted Redaction Maps
- **Statement**: PII found by Model Armor at ingress MUST be replaced with **reversible typed tokens** and the token map stored encrypted:
  1. **Tokenize**: `ticket_router.security.pii_tokens.tokenize_pii(text, findings) -> (tokenized_text, mapping)` replaces each SDP finding span (`location.codepointRange`) with `[<INFO_TYPE>_<n>]` (e.g. `[EMAIL_ADDRESS_1]`, `[PHONE_NUMBER_1]`), numbering per type in order of appearance and reusing the same token for repeated identical values. `mapping` is `{token: original_value}`. Card numbers (`CREDIT_CARD_NUMBER`) are tokenized as `[CREDIT_CARD_NUMBER_n]` but their original value is NEVER stored in the mapping (value replaced by `"<withheld>"`), keeping the system out of PCI scope (TDD §4.2 C4).
  2. **Encrypt**: `ticket_router.security.envelope.EnvelopeCipher(kms_key_name)` generates a random 256-bit DEK per map, encrypts the JSON mapping with AES-256-GCM (random 96-bit nonce, AAD = `ticket_id`), and wraps the DEK with Cloud KMS `encrypt` on a symmetric key (`ENCRYPT_DECRYPT`, `GOOGLE_SYMMETRIC_ENCRYPTION`). The stored blob is a versioned JSON envelope `{v:1, kid:<cryptoKey name>, wdek:<b64>, nonce:<b64>, ct:<b64>}`. Plaintext DEKs and mappings exist only in memory and are never logged. KMS calls go through `create_google_session()`/`to_mtls_url()` (REQ-AUTH-006).
  3. **Persist**: TriageRouter persists the tokenized text as `body_redacted` and saves the envelope via a new ticket-db-mcp tool `save_redaction_map(ticket_id, customer_id, envelope_b64, key_id)` after the ticket row exists. PII tokenization runs on every persisted path, including Tier 0 escalations (Model Armor runs before persisting `body_redacted`). Migration `0004_redaction_envelope.sql` grants `ticket_rw` **INSERT only** on `redaction_maps` (no SELECT/UPDATE/DELETE), changes the `key_id` default from `'secret-manager-v1'` to `'kms-envelope-v1'`, and grants `queue_rw` DELETE for the TTL purge (REQ-STORE-010).
  4. **Restore**: human-queue `reidentify_pii` (claimed tickets only, audited) loads the ticket's maps, decrypts with `EnvelopeCipher.decrypt` (KMS `decrypt`), and returns `{token: value}`. The previous fake fallbacks (`pii_store` and synthesized `example.com` values) MUST be removed: no map → empty result.
  5. **IAM**: TriageRouter (agent identity principal and `sa-triage`) gets `roles/cloudkms.cryptoKeyEncrypter` on the key; `sa-queue` gets `roles/cloudkms.cryptoKeyDecrypter`. No other principal holds either.
  Failure policy: if KMS or the save fails, the ticket still persists with tokenized text (fail closed: no plaintext anywhere), and a `redaction_map_failed` event is recorded.
- **Rationale**: TDD §3.3 step 1, §3.4 and §4.2 C3. Previously the redacted text was irreversible and `redaction_maps` was never written, while the reader decoded it as plain JSON.
- **Acceptance**: `tests/security/test_pii_tokens.py::test_tokenize_is_reversible_and_withholds_cards` and `tests/security/test_envelope.py::test_envelope_roundtrip_and_aad_binding` and `manual: live e2e message with an email produces a [EMAIL_ADDRESS_1] ticket body and a kms-envelope-v1 redaction map that human-queue restores after claim`
- **Status**: done
- **Owner**: ticket-store

### REQ-STORE-010: Redaction Map 30-Day TTL Purge
- **Statement**: Redaction maps MUST be deleted once `expires_at < now()`. human-queue exposes `POST /internal/purge-expired-redaction-maps`, authenticated by an OIDC token from the Cloud Scheduler service account (audience = endpoint URL; any other caller → 401). It runs `DELETE FROM redaction_maps WHERE expires_at < now()` under `queue_rw`, records the deleted count (no PII) in logs, and is idempotent. Terraform provisions a Cloud Scheduler job (daily 03:15 UTC) that calls it, plus `roles/run.invoker` on human-queue for the scheduler SA only.
- **Rationale**: TDD §3.4/§4.2 30-day TTL; the column and index existed but nothing deleted rows.
- **Acceptance**: `tests/agents/test_redaction_purge.py::test_purge_requires_scheduler_oidc_and_deletes_expired`
- **Status**: done
- **Owner**: ticket-store
