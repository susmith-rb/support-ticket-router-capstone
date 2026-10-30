# Agent Identity & Customer Context Token Specification

This spec covers how the AI agents prove **who they are**, which is the actor (workload) identity, and how the **customer they act for**, the subject identity, travels to the database without being trusted from headers. It implements the target design in the TDD (§4.1 Authentication & Authorization Matrix) and the zero-trust review feedback from 2026-10-29: carry both actor and subject identity on every hop, verify both at each receiver, use short-lived just-in-time credentials, and grant least privilege.

Related specs: [customer-identity.md](customer-identity.md) (REQ-IDENT-*: ingress token verification and `(issuer, subject) → customer_id`), [agent-service.md](agent-service.md) (REQ-AGENT-010 BOLA/IDOR), [ticket-store.md](ticket-store.md) (RLS).

---

## 1. Requirements

### REQ-AUTH-001: Least-Privilege Custom Inference Roles (replace `roles/aiplatform.user`)
- **Statement**: AI agent identities MUST NOT hold `roles/aiplatform.user`. That role has ~459 permissions, including `reasoningEngines.delete`, `endpoints.deploy` and governance-policy writes. Terraform (`modules/agents-iam`) MUST define project-level custom roles with exactly enumerated permissions, derived from the API calls each agent makes:
  1. `ticketRouterAgentInference` (FaqResolver, `sa-faq`): `aiplatform.endpoints.predict` (Gemini `generateContent` on publisher models).
  2. `ticketRouterTriageRuntime` (TriageRouter, `sa-triage`, and its agent identity under REQ-AUTH-004): `aiplatform.endpoints.predict`, plus the Agent Engine session permissions used by `AdkApp` / `VertexAiSessionService`: `aiplatform.sessions.create`, `aiplatform.sessions.get`, `aiplatform.sessions.list`, `aiplatform.sessions.update`, `aiplatform.sessions.delete`, `aiplatform.sessions.run`, `aiplatform.sessionEvents.append`, `aiplatform.sessionEvents.list`.
  
  The `google_project_iam_member` bindings of `roles/aiplatform.user` to `sa-triage` and `sa-faq` MUST be removed and replaced with bindings to these custom roles. The endpoint-scoped `roles/aiplatform.predictionUser` bindings on router-s1 remain unchanged. When Memory Bank is built, its `aiplatform.memories.*` permissions MUST be added to the custom role explicitly, never by re-granting `aiplatform.user`.
- **Rationale**: An agent holding `aiplatform.user` could delete other agents or rewrite the policies that constrain it (FDE security guidance; TDD §4).
- **Acceptance**: `tests/infra/test_agent_custom_roles_static.py::test_agents_use_enumerated_custom_roles` and `manual: CD smoke after apply (cloud_run_smoke 9/9, identity e2e 5/5, Model Armor 5/5) passes with the custom roles`
- **Status**: partial
- **Owner**: identity

### REQ-AUTH-002: Signed Customer Context Token (minting at TriageRouter)
- **Statement**: When, and only when, a turn is handed to FaqResolver for a **verified** customer (`customer_verified = True`, `customer_id` not `anon:`/empty), TriageRouter MUST mint a **customer context token (CCT)**: a compact JWS (ES256) with header `{"alg":"ES256","typ":"JWT","kid":<full KMS cryptoKeyVersion name>}` and claims `iss` (configured minter ID, `CCT_ISSUER`), `sub` (= verified `customer_id`), `aud` (= `CCT_AUDIENCE`, default `order-lookup-mcp`), `iat`, `exp` (= `iat + CCT_TTL_S`, default 300 s, max 600 s), `jti` (random UUID4), `act` (`{"sub": "triage-router"}`) and `tid` (ticket ID). The signature MUST come from Cloud KMS `asymmetricSign` (key purpose `ASYMMETRIC_SIGN`, algorithm `EC_SIGN_P256_SHA256`), with the DER signature converted to raw `r||s` for JWS. No private key material ever exists in application memory or config. Anonymous or unverified turns MUST NOT carry a CCT. Minting failures MUST fail safe: no CCT, the turn continues, and order lookup is then denied downstream. The token, claims and `customer_id` MUST NOT be logged (REQ-AGENT PII rule; log `jti` and `tid` only).
- **Rationale**: Today downstream hops trust `customer_id` because of who the caller is, not because of a signature. A compromised FaqResolver could assert any customer (TDD §4.1 gap).
- **Acceptance**: `tests/identity/test_customer_context_token.py::test_mint_only_for_verified_customers` and `tests/identity/test_customer_context_token.py::test_token_format_and_claims`
- **Status**: done
- **Owner**: identity

### REQ-AUTH-003: CCT Transport and Verification at order-lookup-mcp (RLS from verified claims)
- **Statement**: The CCT MUST travel unchanged from TriageRouter to order-lookup-mcp and be verified there before any data access, as follows:
  1. **Transport**: TriageRouter puts the CCT in the A2A payload field `customer_context_token`. FaqResolver MUST forward it **unchanged** (byte-identical) to `order-lookup-mcp` in the HTTP header `X-Customer-Context`. FaqResolver MUST NOT decode, re-sign or modify it.
  2. **Verification**: `order-lookup-mcp` MUST verify the CCT before any repository call: ES256 signature against the KMS public key for `kid` (fetched with `getPublicKey`, cached ≤ 1 h, `kid` MUST start with the configured `CCT_KEY_PREFIX`), `aud == CCT_AUDIENCE`, `iss ∈ CCT_ALLOWED_ISSUERS`, `exp > now − 60 s`, `iat ≤ now + 60 s`, `exp − iat ≤ 600`, and `sub` non-empty and not `anon:`. The verified `sub` becomes the `customer_id` passed to the repository, which sets `app.customer_id` for RLS (REQ-STORE RLS policy `order_customer_isolation`). Any failure → HTTP 403 `invalid_customer_context`. If a legacy `X-Customer-ID` header is also present and differs from `sub` → 403 `customer_context_mismatch`. A `customer_id` in the request body remains a 400 (REQ-AGENT-010).
  3. **Rollout mode**: `CCT_MODE ∈ {off, accept, require}` (config/env, default `accept`). In `off`, legacy header behavior applies. In `accept`, a present CCT is verified (failure → 403), and when absent the legacy header path applies. In `require`, a CCT is mandatory and the headers alone are rejected (403). Production moves to `require` once the deployed e2e identity check passes in `accept`.
- **Rationale**: Makes RLS rest on a cryptographic customer claim minted at ingress, not on caller trust. Short-lived (5 min) and audience-scoped, so a leaked token is useless elsewhere.
- **Acceptance**: `tests/mcp/test_order_lookup_customer_context.py::test_cct_verification_matrix` and `tests/agents/test_faq_forwards_cct_unchanged.py::test_forward_unchanged`
- **Status**: done
- **Owner**: identity

### REQ-AUTH-004: SPIFFE Agent Identity for TriageRouter (Agent Runtime)
- **Statement**: TriageRouter MUST run under its Agent Runtime **agent identity**, a SPIFFE-based IAM principal `principal://agents.global.org-<ORG>.system.id.goog/resources/aiplatform/projects/<PROJECT_NUMBER>/locations/<REGION>/reasoningEngines/<ID>` with certificate-bound, auto-rotated credentials, instead of the `sa-triage` service account. All grants listed under REQ-AUTH-001/002 for `sa-triage` (custom triage role, router-s1 `predictionUser`, Pub/Sub publisher, `run.invoker` on Cloud Run MCP services, KMS signer, Model Armor user, Cloud Trace agent, Discovery Engine where used) MUST be granted to the agent principal in Terraform. Any remaining need for a Google-signed OIDC ID token (Cloud Run invoker, FaqResolver A2A) MUST be met without long-lived keys, and the mechanism MUST be documented here. The pilot runs on a staging engine first; the production engine switches only after the CD smoke suite passes on staging.
- **Rationale**: Per-agent SPIFFE identity, with no shared SA and no keys, gives precise audit attribution (FDE security guidance; review feedback 2026-10-29).
- **Acceptance**: `manual: staging engine deployed with identity_type=AGENT_IDENTITY; cloud_run_smoke + identity e2e + Model Armor smoke pass against it`
- **Status**: open
- **Owner**: identity

### REQ-AUTH-005: Stated Service-Account Exceptions
- **Statement**: Service accounts remain ONLY where agent identity is unsupported or inappropriate, and each MUST be keyless: (1) FaqResolver on GKE (Workload Identity `ksa-faq → sa-faq`, because GKE has no agent identity); (2) Cloud Run MCP services and HumanHandoffService (`sa-ticket-mcp`, `sa-order-mcp`, `sa-queue`), because Cloud SQL IAM database authentication requires a user or SA principal; (3) CI/CD (`ticket-router-build`, `sa-cd-deployer`). No SA keys may exist (org policy `iam.disableServiceAccountKeyCreation`).
- **Rationale**: Makes the exceptions explicit and reviewable instead of implicit.
- **Acceptance**: `tests/infra/test_agent_custom_roles_static.py::test_no_service_account_keys`
- **Status**: done
- **Owner**: identity

---

## 2. Open Questions / Deltas

1. **KMS key ring permanence**: Cloud KMS key rings and keys cannot be deleted, only key versions destroyed. The key ring `ticket-router-identity` is therefore permanent in the project.
2. **Agent identity and ID tokens**: whether an Agent Runtime agent identity can mint audience-scoped Google ID tokens directly is unverified as of 2026-10-29. REQ-AUTH-004 resolves it in the staging pilot and records the result here.
