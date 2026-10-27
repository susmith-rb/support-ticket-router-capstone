# Specification: Customer Identity & Authentication Subsystem (`customer-identity`)

This specification defines the customer authentication lifecycle, Identity Platform integration, relational identity mapping, and zero-trust propagation across agents and MCP servers, closing the caller-asserted identity vulnerability (TDD §3, §4, §5.1, §5.2).

---

## 1. Overview & Threat Model

In legacy workflows, customer identity was caller-asserted (`ctx.session.user_id` defaulting to `cust_anon`). While downstream MCP tools enforce Postgres Row-Level Security (RLS) on `orders_mock`, binding RLS to an unauthenticated caller assertion creates an Insecure Direct Object Reference (IDOR / BOLA) vulnerability.

```
Inbound Request [customer_id_token (Firebase JWT)]
                   │
                   ▼
┌────────────────────────────────────────────────────────┐
│ TriageRouter (verify_firebase_token)                   │
│   ├── Valid JWT ──▶ resolve_customer(iss, sub) ──────┐ │
│   └── Missing/Invalid ──▶ customer_id = None (anon)  │ │
└──────────────────┬───────────────────────────────────┼─┘
                   │ A2A Handoff                       │ ticket-db-mcp
                   │ (sa-triage@ signed OIDC)          ▼
                   ▼                     ┌───────────────────────────┐
┌──────────────────────────────────────┐ │ Cloud SQL PostgreSQL 16   │
│ FaqResolver (ALLOWED_CALLERS verify) │ │ customers                 │
│   ├── X-Customer-ID: cust_1001       │ │ customer_identities       │
│   └── X-Customer-ID: anon:... (FAIL) │ │ identity_events           │
└──────────────────┬───────────────────┘ └───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────┐
│ order-lookup-mcp (orders_ro RLS)     │
│   └── Rejects anonymous / unverified │
└──────────────────────────────────────┘
```

### Threat Mitigations

1. **Caller-Asserted Spoofing (IDOR/BOLA)**: Inbound tokens verified server-side; `session.user_id` is never trusted for authorization.
2. **Cross-Project Replay**: JWT verification strictly binds `aud` to `susmith-fde-ticket-router` and `iss` to the project's secure token endpoint.
3. **Stale / Revoked Identity**: Resolution verifies `customers.status = 'active'`, failing closed for suspended accounts.
4. **Data Exfiltration**: Anonymous sessions (`anon:*`) are forbidden from invoking `order-lookup-mcp`.

---

## 2. Relational Identity Schema

Migration `db/migrations/0003_customer_identity.sql` establishes relational mapping:

| Table | Columns | Purpose | Access Roles |
|---|---|---|---|
| `customers` | `customer_id` (PK), `status`, `created_at` | Master customer entity (`active`, `suspended`) | `ticket_rw` (SELECT), admin (CRUD) |
| `customer_identities` | `issuer` (PK), `subject` (PK), `customer_id` (FK), `email_verified`, `linked_at` | Maps IdP identity to customer (zero email storage) | `ticket_rw` (SELECT), admin (CRUD) |
| `identity_events` | `event_id` (PK), `customer_id`, `event_type`, `actor`, `payload`, `created_at` | Immutable audit trail for identity linkage | Admin / script only |

---

## 3. Requirements

### REQ-IDENT-001: Cloud Identity Platform IdP Configuration & Token Format
- **Statement**: Customer authentication MUST be managed by Google Cloud Identity Platform (`identitytoolkit.googleapis.com`) provisioned via Terraform (`google_identity_platform_config`) with email/password sign-in enabled, authorized domains configured, and multi-tenancy disabled. Customer ID tokens MUST be Firebase-format JWTs where `iss = "https://securetoken.google.com/susmith-fde-ticket-router"`, `aud = "susmith-fde-ticket-router"`, and `sub` contains the unique Identity Platform user identifier (UID).
- **Rationale**: TDD §5.1 and §5.2. Offloads credential storage and token issuance to a managed GCP IdP without custom auth servers.
- **Acceptance**: `tests/identity/test_token_verification.py::test_idp_token_claims_and_format`
- **Status**: done
- **Owner**: identity

### REQ-IDENT-002: Relational Customer Identity Schema & Migration 0003
- **Statement**: Migration `db/migrations/0003_customer_identity.sql` MUST idempotently create tables `customers` (backfilled from `orders_mock` for `cust_1001`..`cust_1050`), `customer_identities` (indexed on `customer_id`), and `identity_events`. Foreign keys MUST enforce referential integrity (`ON DELETE CASCADE`). Table permissions MUST grant `SELECT ON customers, customer_identities TO ticket_rw`, while write permissions remain restricted to administrative break-glass roles. PII protection MUST be strictly enforced: real email addresses MUST NOT be stored in Postgres columns (subject UID only).
- **Rationale**: TDD §3, §5.1, and §5.2. Provides relational integrity and auditability while upholding C2/C3 zero-PII storage invariants.
- **Acceptance**: `tests/infra/test_migration_0003.py::test_customer_identity_schema`
- **Status**: done
- **Owner**: store

### REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution via ticket-db-mcp
- **Statement**: `TriageRouter` MUST cryptographically verify customer ID tokens on every inbound request using `google.oauth2.id_token.verify_firebase_token` validating signature, `iss`, `aud`, `exp`, and `iat`; the identity key is (`iss`, `sub`) — `email_verified` is recorded for audit but NOT required (email/password demo accounts are unverified at signUp). Inbound tokens MUST be extracted from session state key `customer_id_token` or message metadata; caller-asserted `session.user_id` MUST NEVER be trusted as `customer_id`. Verified `(issuer, subject)` pairs MUST be resolved to `customer_id` via `ticket-db-mcp` tool `resolve_customer(issuer, subject)`. Agent runtimes MUST NOT hold direct database credentials. Verified identity mappings MAY be cached ONLY in server-side process memory keyed by SHA-256(token) with TTL ≤ token `exp`; any `customer_id` or verification result found in caller-writable session state or message metadata MUST be ignored — only the raw token is read from there.
- **Rationale**: Eliminates caller-asserted identity spoofing while upholding REQ-AGENT-010 (agents hold no direct database credentials).
- **Acceptance**: `tests/identity/test_token_verification.py::test_verify_and_resolve_customer`
- **Status**: done
- **Owner**: triage-router

### REQ-IDENT-004: Fail-Closed Anonymous Fallback & Authorization Boundaries
- **Statement**: If a customer ID token is missing, expired, cryptographically invalid, or maps to an unlinked identity or suspended customer (`status != 'active'`), the system MUST fail closed: set `customer_id = None`, set `customer_verified = false`, and store ticket customer ID as `'anon:' || session_id` to satisfy schema `NOT NULL` constraints. Unauthenticated sessions MAY receive generic RAG FAQ answers, but MUST be strictly denied order lookups, personal PII, or customer ticket history. Tier 0 safety rules and human queue escalations MUST continue to execute fully, preserving the add-only escalation invariant (`REQ-AGENT-003`).
- **Rationale**: Enforces least privilege and prevents unauthorized data exposure without dropping distressed anonymous customer interactions.
- **Acceptance**: `tests/identity/test_token_verification.py::test_fail_closed_anonymous_fallback`
- **Status**: done
- **Owner**: triage-router

### REQ-IDENT-005: Cryptographic Trust Chain Propagation across A2A & MCP
- **Statement**: When `TriageRouter` dispatches an inquiry to `FaqResolver` via A2A, the payload MUST explicitly propagate `{customer_id, customer_verified: bool, identity_issuer: str | None}` signed with the caller's target-scoped OIDC token (`sa-triage@`). `FaqResolver` MUST trust verified customer identity exclusively from authorized callers (`ALLOWED_CALLERS`). When `FaqResolver` invokes `order-lookup-mcp`, it MUST propagate `X-Customer-ID` authenticated by its own service account token (`sa-faq@`). `order-lookup-mcp` MUST reject requests where `customer_verified` is false or `X-Customer-ID` starts with `'anon:'`.
- **Rationale**: Guarantees end-to-end cryptographic provenance and non-repudiation across multi-agent boundaries (TDD §3.3, §4, §5.1, REQ-AGENT-006).
- **Acceptance**: `tests/agents/test_a2a_identity_propagation.py::test_identity_trust_chain`
- **Status**: done
- **Owner**: agent-service

### REQ-IDENT-006: Administrative Account Linking & Deterministic Demo Seeding
- **Statement**: Customer account linking MUST be managed administratively via CLI script `scripts/link_customer_identity.py` executing with Cloud SQL IAM break-glass credentials to map `(issuer, subject)` to a known `customer_id` and log an event in `identity_events`. Automated test suites and demo environments MUST provision at least 2 deterministic Identity Platform test accounts (via REST API `identitytoolkit.googleapis.com/v1/accounts:signUp` using a Terraform-provisioned restricted API key) and link them to `cust_1001` and `cust_1002`. Automatic just-in-time account creation upon token presentation is strictly prohibited in v1.
- **Rationale**: Prevents account takeover, prevents unverified linkage of arbitrary emails to existing customer order history, and provides repeatable end-to-end evaluation seeds.
- **Acceptance**: `tests/infra/test_link_identity_script.py::test_account_linking_and_seeding`
- **Status**: done
- **Owner**: identity

### REQ-IDENT-007: Identity Threat Mitigations (Anti-Spoofing, Replay, IDOR, Suspension)
- **Statement**: The identity subsystem MUST explicitly mitigate: (1) Caller-asserted identity spoofing by requiring server-side JWT verification; (2) Cross-project token replay by enforcing `aud = "susmith-fde-ticket-router"`; (3) Insecure Direct Object References (IDOR) by deriving database RLS keys strictly from verified JWT claims; and (4) Stale or revoked authorizations by querying `customers.status` upon resolution and rejecting suspended accounts (`status = 'suspended'`).
- **Rationale**: Addresses critical attack vectors in enterprise support platforms (TDD §5.2 threat model).
- **Acceptance**: `tests/identity/test_token_verification.py::test_threat_mitigations`
- **Status**: done
- **Owner**: identity

### REQ-IDENT-008: Verification Test Suite (Unit, Contract, Migration, Live E2E)
- **Statement**: The customer identity subsystem MUST be covered by automated test suites encompassing: (1) Unit tests for JWT parsing, expiry, invalid audience/issuer, and unlinked fallback; (2) MCP contract tests for `resolve_customer` schema and error handling; (3) Migration static checks validating PKs, FKs, indices, and absence of email columns; and (4) Live/mocked E2E tests verifying that test user A accesses `cust_1001` orders, is rejected for `cust_1002` orders, and anonymous callers are denied order lookups.
- **Rationale**: TDD §7.1 and §7.2 verification gates guaranteeing zero regressions across identity boundaries.
- **Acceptance**: `tests/identity/test_customer_identity_e2e.py::test_e2e_identity_verification`
- **Status**: open
- **Owner**: identity

---

## 4. Open Questions / Deltas

1. **JIT Auto-Provisioning**: Self-service user registration and automated linking are deferred to Phase 2 to prevent account takeover vectors on legacy order histories.
2. **Session Persistence**: Session cache for verified identities is stored in-memory per session instance; distributed Redis caching is evaluated for high-concurrency multi-instance scaling.
