# Specification: Model Armor Security & Sanitization Subsystem (`model-armor`)

This specification defines the ingress and egress security architecture using Google Cloud Model Armor to protect agents and customers against prompt injection, jailbreaks, malicious URIs, harmful content, and PII leakage (TDD §3.7, §4, §5).

---

## 1. Overview & Security Topology

Model Armor acts as an inline defensive barrier for prompt sanitization (ingress) and response validation (egress). Ingress sanitization runs concurrently with the `router-s1` Tier-1 encoder classifier, and outbound calls use regional keep-alive HTTPS sessions.

```
Inbound Support Ticket (Session Authenticated)
                   │
                   ▼
┌────────────────────────────────────────────────────────┐
│ Concurrent Evaluation (ThreadPoolExecutor)             │
│   ├── ModelArmorClient.sanitize_user_prompt (Regional) │
│   └── RouterS1Client.predict (Tier 1 Encoder)          │
└──────────────────┬─────────────────────────────────────┘
                   │
                   ▼
┌────────────────────────────────────────────────────────┐
│ Pre-LLM / Pre-A2A Security Gate (Model Armor Verdict)  │
│   ├── PI / Jailbreak / Malicious URI ──▶ Human Queue   │
│   ├── Harassment / Abuse ──▶ Boundary Ack + Human      │
│   ├── Degraded / Timeout ──▶ Fail-Safe Human Standard  │
│   └── Clean ──▶ Apply SDP Redaction & Continue Triage  │
└──────────────────┬─────────────────────────────────────┘
                   │ Non-escalated RAG inquiry
                   ▼
┌────────────────────────────────────────────────────────┐
│ FaqResolverAgent                                       │
│   └── LLM Answer ──▶ sanitize_model_response (Egress)  │
│         ├── Clean ──▶ Deliver Answer                   │
│         └── Blocked ──▶ Handoff to Human Queue         │
└────────────────────────────────────────────────────────┘
```

---

## 2. Requirement Index

| ID | Title | Status | Acceptance |
|---|---|---|---|
| `REQ-MA-001` | Declarative Terraform Infrastructure, Template & Least-Privilege IAM | `done` | `tests/infra/test_model_armor_infra_static.py::test_model_armor_template_and_iam` |
| `REQ-MA-002` | Regional Keep-Alive REST Client, Typed Verdicts & Measured Timeout | `done` | `tests/security/test_model_armor_client.py::test_client_verdict_and_resilience` |
| `REQ-MA-003` | Ingress Concurrent Evaluation, Security Policy & Fail-Safe Escalation | `done` | `tests/security/test_model_armor_policy.py::test_triage_ingress_security_policy` |
| `REQ-MA-004` | Egress Sanitization for FaqResolver Answers & Fallback Handoff | `done` | `tests/security/test_model_armor_policy.py::test_faq_egress_sanitization` |
| `REQ-MA-005` | In-Region Latency Budget, Concurrent SLA & Degraded Metrics | `done` | `tests/security/test_model_armor_budget.py::test_latency_budget_and_short_circuit` |
| `REQ-MA-006` | Verification Test Strategy: Fakes, Contracts, Static TF & Live Smoke | `done` | `tests/security/test_model_armor_contracts.py::test_contracts_and_smoke_harness` |

---

## 3. Requirements

### REQ-MA-001: Declarative Terraform Infrastructure, Template & Least-Privilege IAM
- **Statement**: Infrastructure MUST declare `modelarmor.googleapis.com` in `modules/project-services` and provision `google_model_armor_template` resource `support-router-lean` in `us-central1`. The template MUST configure: (1) `pi_and_jailbreak_filter_settings` with `filter_enforcement = "ENABLED"` and `confidence_level = "MEDIUM_AND_ABOVE"`; (2) `malicious_uri_filter_settings` with `filter_enforcement = "ENABLED"`; (3) `rai_settings` enabling filters `SEXUALLY_EXPLICIT`, `HATE_SPEECH`, `HARASSMENT`, and `DANGEROUS` at `confidence_level = "MEDIUM_AND_ABOVE"`; and (4) `sdp_settings` with `advanced_config { inspect_template = <inspect_template_id> }` (migrated from `basic_config`) referencing a regional `google_data_loss_prevention_inspect_template` in `us-central1` configured with `min_likelihood = "LIKELY"` (avoids tokenizing order IDs), `include_quote = false`, and info types: `EMAIL_ADDRESS`, `PHONE_NUMBER`, `US_SOCIAL_SECURITY_NUMBER`, `CREDIT_CARD_NUMBER`, `STREET_ADDRESS`, and `IBAN_CODE`. Model Armor service agent `service-<PROJECT_NUMBER>@gcp-sa-modelarmor.iam.gserviceaccount.com` MUST be granted `roles/dlp.user` and `roles/dlp.reader` on the project, with explicit `depends_on` ensuring inspect template and IAM readiness prior to Model Armor template creation. SDP inspection MUST be verified to preserve order identifiers (`ORD-10001`) and replacement tokens (`[ORDER_ID]`) unredacted while redacting payment credentials (e.g. credit cards). IAM role `roles/modelarmor.user` MUST be granted strictly to `sa-triage@` and `sa-faq@`.
- **Rationale**: TDD §3.7, §4, and §5. Enforces centralized declarative guardrails without manual cloud console drift or excessive permissions.
- **Acceptance**: `tests/infra/test_model_armor_infra_static.py::test_model_armor_template_and_iam`
- **Status**: done
- **Owner**: infra

### REQ-MA-002: Regional Keep-Alive REST Client, Typed Verdicts & Measured Timeout
- **Statement**: Security client `src/ticket_router/security/model_armor.py` MUST target regional endpoint `https://modelarmor.us-central1.rep.googleapis.com` using persistent pooled keep-alive HTTPS connections (`requests.Session` with `HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)`) and ADC bearer authentication (`ticket_router.common.auth.get_access_token`). It MUST expose `sanitize_user_prompt(text)` and `sanitize_model_response(text)`, parsing responses into a typed verdict dataclass (`is_clean`, `blocked`, `action`, `pi_jb`, `malicious_uri`, `rai_categories`, `sdp_findings`, `sanitized_text`). Outbound calls MUST sit behind an in-memory `CircuitBreaker` (name `"model_armor"`, 3 failures $\to$ 30s cooldown, max 300s, $\pm20\%$ jitter). Client HTTP timeout MUST be calibrated from live in-region measurements as $\max(500\text{ ms}, p99 \times 1.5)$. On timeout or breaker trip, client MUST emit a `model_armor_degraded` structured log and metric counter.
- **Rationale**: TDD §3.5, §10.1, and §10.4. Minimizes TLS handshake overhead, enforces typed safety contracts, and prevents slow service degradation from silently escalating traffic.
- **Acceptance**: `tests/security/test_model_armor_client.py::test_client_verdict_and_resilience`
- **Status**: done
- **Owner**: security

### REQ-MA-003: Ingress Concurrent Evaluation, Security Policy & Fail-Safe Escalation
- **Statement**: `TriageRouterAgent` MUST execute `sanitize_user_prompt` CONCURRENTLY with the `router-s1` Tier-1 encoder forward pass via threadpool futures before any neural Tier-2 call (`gemini-3.8-flash`) or FaqResolver handoff: (1) Inbound prompts MUST ALWAYS be evaluated by Model Armor without length bypass; (2) Inputs flagged for prompt injection/jailbreak (`pi_jb`) or malicious URIs MUST NEVER be forwarded to LLM tiers or `FaqResolverAgent`, routing immediately to human queue with security flag and a neutral safe acknowledgement; (3) Inputs flagged for RAI harassment/abuse MUST escalate to human queue with a calming boundary acknowledgement, MUST NEVER be dropped, and MUST NOT artificially inflate priority (`REQ-AGENT-004`); (4) SDP findings MUST replace message text with Model Armor's de-identified text (`body_redacted`); (5) If Model Armor is unavailable, times out, or trips its circuit breaker, the system MUST fail safe: skip automated resolution, assign `human-standard` with `degraded = True`, and send a calming acknowledgement. Verdict summaries (zero raw text) MUST be recorded in ticket decision metadata and structured logs (`REQ-AGENT-014`).
- **Rationale**: TDD §3.5, §3.6, §3.7, and §8. Guarantees prompt injection isolation and upholds add-only escalation invariants (`REQ-AGENT-003`).
- **Acceptance**: `tests/security/test_model_armor_policy.py::test_triage_ingress_security_policy`
- **Status**: done
- **Owner**: triage-router

### REQ-MA-004: Egress Sanitization for FaqResolver Answers & Fallback Handoff
- **Statement**: `FaqResolverAgent` MUST invoke `sanitize_model_response` on synthesized answers prior to customer delivery or ticket persistence. If Model Armor blocks the response due to RAI violations or sensitive information disclosure, the answer MUST NOT be shown to the customer; the ticket MUST transition to `ESCALATED` targeting `human-standard` with reason `model_armor_response_blocked` and a calming handoff message. If Model Armor is unavailable at egress, the agent MUST degrade gracefully to human review with `degraded = True`.
- **Rationale**: TDD §3.5, §3.7, and §6. Prevents agent hallucination of harmful, abusive, or sensitive content from reaching customers.
- **Acceptance**: `tests/security/test_model_armor_policy.py::test_faq_egress_sanitization`
- **Status**: done
- **Owner**: faq-resolver

### REQ-MA-005: In-Region Latency Budget, Concurrent SLA & Degraded Metrics
- **Statement**: Model Armor integration MUST enforce strict latency budgets: in-region execution budget is allocated $\le 300\text{ ms}$ (p95) using regional endpoint co-location in `us-central1`. Concurrent execution with `router-s1` allows Model Armor sanitization and Tier-1 routing to overlap within a $\le 500\text{ ms}$ combined window, protecting the 3,000 ms `TriageRouter` turn budget. Prompts of any word count MUST be evaluated. Live latency benchmarking script `scripts/model_armor_smoke.py --latency` MUST profile in-region round-trip times from `laya-tune` across 50 keep-alive calls to calibrate timeouts and verify that fail-safe degradations emit `model_armor_degraded` telemetry. Production timeout = max(500 ms, measured p99 × 1.5).
- **Measured (2026-10-28, laya-tune → us-central1 REP, 50 keep-alive calls, 0 degraded)**: p50 89.9 ms, p95 363.4 ms, p99 457.7 ms → `DEFAULT_TIMEOUT_S = 0.700`. The p95 exceeds the 300 ms aspiration; it overlaps router-s1 (p95 77 ms) and stays well inside the 3,000 ms escalation budget. Re-measure from the serving runtimes during the load test.
- **Note**: The cloudtop cannot reach the regional REP endpoint; run live smoke via laya-tune. Basic SDP returns inspect findings only, so redaction is applied locally from `codepointRange` offsets (`redact_sdp_findings`). The live response nests results as `filterResults.<filter>.<typed>FilterResult` (regression test `tests/security/test_model_armor_live_shape.py`).
- **Rationale**: TDD §3.5, §10.1. Concurrent execution eliminates sequential latency penalty while live measurement prevents blind SLA degradation.
- **Acceptance**: `tests/security/test_model_armor_budget.py::test_latency_budget_and_short_circuit`
- **Status**: done
- **Owner**: security

### REQ-MA-006: Verification Test Strategy: Fakes, Contracts, Static TF & Live Smoke
- **Statement**: Model Armor integration MUST be covered by: (1) Hermetic unit tests using `FakeModelArmorClient` validating prompt injection blocking, abuse boundary ack, priority preservation, SDP redaction, outage fail-safe, and egress blocking; (2) Contract tests validating request/response payload serialization against the Model Armor REST API specification; (3) Static Terraform tests asserting template configuration, filter thresholds, and least-privilege IAM bindings; and (4) Standalone live smoke script `scripts/model_armor_smoke.py` supporting `--latency` benchmark, benign passage, injection blocking, abuse detection, and SDP non-redaction of order tokens against live GCP endpoints post-apply.
- **Rationale**: TDD §7.1, §12.1. Guarantees hermetic offline CI execution while providing verified live smoke tooling.
- **Acceptance**: `tests/security/test_model_armor_contracts.py::test_contracts_and_smoke_harness`
- **Status**: done
- **Owner**: security
