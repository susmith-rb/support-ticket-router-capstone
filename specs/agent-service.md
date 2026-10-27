# Specification: Multi-Agent Service & MCP Subsystem (`agent-service`)

This specification defines the multi-agent runtime topology, inter-agent communication, tool contracts (Model Context Protocol), routing policies, resilience controls, and execution budgets for the Support Ticket Router & Assistant service.

---

## 1. Overview & Deployment Topology

The service is composed of three cooperative agents and two dedicated MCP servers partitioned across Google Cloud managed runtimes by architectural responsibility:

```
Inbound Support Ticket (Session-authenticated)
                   │
                   ▼
┌────────────────────────────────────────────────────────┐
│ TriageRouterAgent                                      │
│ Runtime: GEAP Agent Runtime (Cloud Run fallback)       │
│ Identity: sa-triage@ (PredictionUser, MCP Invoker, PS) │
└────────────┬─────────────────────────────┬─────────────┘
             │                             │
    [p_esc < 0.15 & faq]          [p_esc >= 0.35 | urgent]
             ▼                             ▼
┌───────────────────────────┐ ┌───────────────────────────┐
│ FaqResolverAgent          │ │ HumanQueueAgent           │
│ Runtime: GKE Autopilot    │ │ Runtime: Cloud Run        │
│ Deployment + HPA (2-10)   │ │ Identity: sa-queue@       │
│ Identity: sa-faq@ (WI)    │ │ View: Ops Console / GE App│
└──────┬──────────────┬─────┘ └────────────┬──────────────┘
       │              │                    │
       ▼              ▼                    ▼
┌──────────────┐ ┌───────────────┐ ┌──────────────────────┐
│ Agent Search │ │order-lookup   │ │ticket-db             │
│ Engine (us)  │ │MCP Server     │ │MCP Server            │
│ + Laya Rerank│ │(Cloud Run)    │ │(Cloud Run)           │
│ (router-s1)  │ │sa-order-mcp@  │ │sa-ticket-mcp@        │
└──────────────┘ └───────────────┘ └──────────────────────┘
```

### Runtime Topology and Service Account Mapping

| Component | Target Runtime | Scalability & Packaging | Dedicated GCP Service Account | Database IAM Role |
|---|---|---|---|---|
| `TriageRouterAgent` | **GEAP Agent Runtime** *(Cloud Run fallback)* | Native serverless session host | `sa-triage@` (predictionUser, run.invoker on ticket-db-mcp, pubsub.publisher) | **None** *(Accesses DB strictly via ticket-db-mcp)* |
| `FaqResolverAgent` | **GKE Autopilot** | Deployment + HPA (2–10 pods, 60% CPU) | `ksa-faq` $\to$ `sa-faq@` (WI: discoveryengine.viewer, predictionUser, run.invoker) | **None** *(Accesses DB strictly via MCP servers)* |
| `HumanQueueAgent` | **Cloud Run** | Min 1 warm replica, concurrency 80 | `sa-queue@` (queue_rw Cloud SQL client, run.invoker on agent-search-mcp) | `queue_rw` *(Claim/resolve and decrypt redaction_maps)* |
| `order-lookup-mcp` | **Cloud Run** | Min 0, scale-to-zero, concurrency 100 | `sa-order-mcp@` (Cloud SQL client) | `orders_ro` *(SELECT orders_mock with Postgres RLS)* |
| `ticket-db-mcp` | **Cloud Run** | Min 1 warm replica, concurrency 100 | `sa-ticket-mcp@` (Cloud SQL client) | `ticket_rw` *(CRUD tickets, ticket_events)* |

### Package Directory Structure

```
src/ticket_router/
├── agents/
│   ├── triage_router/    # Tier 0 rules, Tier 1/2 routing policy, calming ack templates
│   │   ├── __init__.py
│   │   ├── agent.py
│   │   ├── policy.py
│   │   └── templates.py
│   ├── faq_resolver/     # Bounded ReAct, Agent Search + Laya rerank, citation validation
│   │   ├── __init__.py
│   │   ├── agent.py
│   │   ├── search.py
│   │   └── validator.py
│   └── human_queue/      # Ticket claiming, list/inspect, audited PII re-identification
│       ├── __init__.py
│       ├── agent.py
│       └── audit.py
└── mcp/
    ├── ticket_db/        # Ticket persistence, lifecycle events, pgcrypto redaction map
    │   ├── __init__.py
    │   ├── server.py
    │   └── schemas.py
    └── order_lookup/     # Read-only order mock repository with Postgres RLS
        ├── __init__.py
        ├── server.py
        └── schemas.py
```

---

## 2. Requirement Index

| ID | Title | Status | Acceptance |
|---|---|---|---|
| `REQ-AGENT-001` | Multi-Agent Package Structure and Deployment Topology | `open` | `tests/agents/test_package_layout.py::test_directory_structure` |
| `REQ-AGENT-002` | TriageRouter Tiered Decision Policy & Thresholds | `done` | `tests/agents/test_triage_router.py::test_tiered_policy_thresholds` |
| `REQ-AGENT-003` | Invariant: Add-Only Escalation and Abuse-Resistant Routing | `done` | `tests/agents/test_triage_router.py::test_add_only_escalation_invariant` |
| `REQ-AGENT-004` | Gaming-Resistant Queue Priority Assignment | `done` | `tests/agents/test_triage_router.py::test_gaming_resistant_priority` |
| `REQ-AGENT-005` | Templated Calming Acknowledgement and Idempotent Ticket Upsert | `done` | `tests/agents/test_triage_router.py::test_calming_ack_and_idempotent_upsert` |
| `REQ-AGENT-006` | Inter-Agent A2A Handoff Protocol | `done` | `tests/agents/test_triage_router.py::test_a2a_handoff_protocol` |
| `REQ-AGENT-007` | FaqResolver Bounded ReAct Loop, Search & Laya Rerank Integration | `done` | `tests/agents/test_faq_resolver.py::test_bounded_react_and_rerank` |
| `REQ-AGENT-008` | Mandatory Grounded Citations with Non-Hallucination Fallback | `done` | `tests/agents/test_faq_resolver.py::test_mandatory_citations_or_escalate` |
| `REQ-AGENT-009` | HumanQueue Workflow and Audited PII Re-Identification | `done` | `tests/agents/test_human_queue.py::test_queue_claim_and_pii_reid` |
| `REQ-AGENT-010` | MCP Tool Contracts, JSON Schemas, and Role-Based DB Access | `done` | `tests/mcp/test_tool_contracts.py::test_schemas_and_roles` |
| `REQ-AGENT-011` | Distributed Circuit Breakers and Fallback Hierarchies | `done` | `tests/common/test_resilience.py::test_circuit_breakers_and_fallbacks` |
| `REQ-AGENT-012` | Execution Budgets, Service Timeouts, and Latency SLAs | `done` | `tests/common/test_resilience.py::test_latency_budgets_and_timeouts` |
| `REQ-AGENT-013` | Centralized Model Configuration Resolution | `open` | `tests/config/test_agent_models.py::test_model_registry_resolution` |
| `REQ-AGENT-014` | Observability: Distributed Tracing & PII-Free Structured Logging | `done` | `tests/common/test_observability.py::test_otel_spans_and_pii_free_logs` |
| `REQ-AGENT-015` | Test Strategy: Hermetic Unit Tests, Schema Contracts, and E2E Smoke | `open` | `tests/agents/test_agent_smoke.py::test_e2e_triage_smoke` |
| `REQ-AGENT-016` | Dedicated Agent Service Accounts and Least-Privilege IAM | `done` | `tests/infra/test_agent_infra_static.py::test_agents_iam_least_privilege` |
| `REQ-AGENT-017` | Agent Runtime Deployment for TriageRouterAgent | `done` | `tests/infra/test_agent_infra_static.py::test_triage_deployment_script` |
| `REQ-AGENT-018` | GKE Autopilot Workload Identity & HPA for FaqResolverAgent | `done` | `tests/infra/test_agent_infra_static.py::test_faq_gke_manifests` |
| `REQ-AGENT-019` | Cloud Run MCP Microservices with IAM Authentication and Zero Standby Cost | `done` | `tests/infra/test_agent_infra_static.py::test_cloud_run_services_iam_and_cost_posture` |
| `REQ-AGENT-020` | Resilient Escalation Queue with Dead-Letter Handling | `done` | `tests/infra/test_agent_infra_static.py::test_pubsub_escalation_and_dlq` |


---

## 3. Detailed Requirements

### REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
- **Statement**: The agent codebase MUST be structured under `src/ticket_router/agents/{triage_router,faq_resolver,human_queue}/` and `src/ticket_router/mcp/{ticket_db,order_lookup}/`. Runtimes MUST map to: `TriageRouterAgent` on GEAP Agent Runtime (SA `sa-triage@`), `FaqResolverAgent` on GKE Autopilot Deployment with HPA and Workload Identity (K8s SA `ksa-faq` bound to `sa-faq@`), `HumanQueueAgent` on Cloud Run (SA `sa-queue@`), and MCP servers on Cloud Run (`sa-order-mcp@`, `sa-ticket-mcp@`). ALL infrastructure (SAs, IAM roles, Cloud Run services, GKE Autopilot cluster + Deployment/HPA/KSA binding, Agent Runtime deployment config, Pub/Sub topics) MUST be declared in Terraform under `deploy/terraform/single-project/modules/*`; imperative scripts are permitted only for artifact builds and model/agent deployment steps that the Terraform provider cannot express, with each exception explicitly documented.
- **Rationale**: Deconstructs monolithic routing into isolated, least-privilege services adhering to TDD §3.5 and GCP security boundaries, with declarative infrastructure reproducibility.
- **Acceptance**: `tests/agents/test_package_layout.py::test_directory_structure`
- **Status**: open
- **Owner**: agent-service

### REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
- **Statement**: `TriageRouterAgent` MUST evaluate inbound messages sequentially through: (1) Tier 0 deterministic safety, abuse, and keyword regex rules; (2) Tier 1 `router-s1` encoder via `:rawPredict` evaluating `{escalate, sentiment, urgency, intent}`; and (3) Tier 2 `gemini-3.8-flash` invoked ONLY when Tier 1 confidence lands in the uncertainty band ($\tau_{\text{low}} \le p_{\text{esc}} < \tau_{\text{esc}}$) or predicts `intent="other"`. Standard operating thresholds MUST be loaded from configuration: $\tau_{\text{esc}} = 0.35$, $\tau_{\text{low}} = 0.15$, $\tau_{\text{intent}} = 0.70$.
- **Rationale**: Enforces System-1/System-2 economics and guarantees sub-100 ms routing for definitive cases (TDD §3.6, §5.2).
- **Acceptance**: `tests/agents/test_triage_router.py::test_tiered_policy_thresholds`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
- **Statement**: Any message triggering `escalate=True`, `sentiment="angry"`, or `urgency="immediate"` from any tier MUST route immediately to the human queue. Downstream tiers, models, or retry logic MUST NEVER clear an escalation decision ($\text{final.escalate} = \text{rules} \lor \text{tier1} \lor \text{tier2}$). Profanity, toxic phrases, or aggressive insults MUST trigger immediate escalation to human queues and MUST NEVER result in ticket drops, silent suppression, or eviction from support channels.
- **Rationale**: Guarantees customer safety, prevents churn-prone interactions from being trapped in automated loops, and upholds AGENTS.md Non-negotiable 1.
- **Acceptance**: `tests/agents/test_triage_router.py::test_add_only_escalation_invariant`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment
- **Statement**: Expressed customer anger or abusive sentiment MUST bypass automated self-service into human review queues, but MUST NOT artificially inflate operational ticket priority (e.g. promoting a normal inquiry to Urgent/P1). Queue priority (P1 Urgent vs P2 Standard vs P3 Low) MUST be determined strictly from verified business facts (e.g. verified delivery delay exceeding SLA, monetary transaction value, stranded returns, or VIP account status via `order-lookup-mcp`).
- **Rationale**: Prevents malicious or manipulative customers from gaming queue wait times by simply shouting or feigning anger (TDD §3.5, §3.6).
- **Acceptance**: `tests/agents/test_triage_router.py::test_gaming_resistant_priority`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
- **Statement**: When escalating to a human queue, `TriageRouterAgent` MUST generate a calming acknowledgement containing the unique `ticket_id` and expected SLA response ETA (e.g. "15 minutes" for urgent, "2 hours" for standard) using a deterministic fallback template (with optional contextual tone enrichment via Flash). Ticket state MUST be upserted to `ticket-db-mcp` using an idempotency key derived from `session_id:message_hash` to eliminate duplicate rows on retried requests.
- **Rationale**: Provides immediate customer reassurance in <1 s while guaranteeing exactly-once ticket creation (TDD §3.2, §3.5).
- **Acceptance**: `tests/agents/test_triage_router.py::test_calming_ack_and_idempotent_upsert`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-006: Inter-Agent A2A Handoff Protocol
- **Statement**: Non-escalated factual inquiries ($p_{\text{esc}} < \tau_{\text{low}}$ and $p_{\text{intent}} \ge \tau_{\text{intent}}$) MUST be dispatched to `FaqResolverAgent` via Agent-to-Agent (A2A) protocol over Agent Gateway. The handoff payload MUST include `{ticket_id, session_id, predicted_intent, redacted_message, memory_summary}` signed with a per-hop target-scoped OIDC token minted for `sa-faq@`.
- **Rationale**: Enables modular multi-agent collaboration with independent lifecycle scaling and per-hop mutual authentication (TDD §3.3, §4, §5.1).
- **Acceptance**: `tests/agents/test_triage_router.py::test_a2a_handoff_protocol`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-007: FaqResolver Bounded ReAct Loop, Search & Laya Rerank Integration
- **Statement**: `FaqResolverAgent` MUST execute a bounded ReAct loop constrained to $\le 6$ tool calls, $\le 2$ retrieval passes, $\le 1$ answer regeneration, and a 20-second wall-clock budget. Document retrieval MUST query Agent Search `support-kb-search` using production configuration `intent_soft_noisy_0.2` (`relevanceThreshold=LOWEST`, query cleaning, soft intent boost 0.2), followed by cross-encoder candidate scoring against the `router-s1` Vertex Prediction endpoint (`task: "rerank"`). If order-specific information is required, the agent MUST query `order-lookup-mcp`.
- **Rationale**: Guarantees bounded execution time and prevents infinite reasoning loops while applying validated top-k retrieval and reranking models (TDD §3.3, §3.5, §6.3, specs/rag-retrieval.md).
- **Acceptance**: `tests/agents/test_faq_resolver.py::test_bounded_react_and_rerank`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-008: Mandatory Grounded Citations with Non-Hallucination Fallback
- **Statement**: Every answer synthesized by `FaqResolverAgent` MUST include $\ge 1$ verbatim citation quotation matching text in the retrieved `faq-articles` documents. If citation validation fails, the agent MUST attempt exactly one answer regeneration with explicit grounding constraints. If validation fails a second time, the agent MUST NOT return an ungrounded or speculative response to the customer; it MUST update ticket status to `ESCALATED`, assign queue `human-standard` with reason `uncited_answer`, and return a calming handoff message.
- **Rationale**: Prevents hallucinated retail policies and enforces the zero-unverified-answer invariant (TDD §3.3, §6.4).
- **Acceptance**: `tests/agents/test_faq_resolver.py::test_mandatory_citations_or_escalate`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification
- **Statement**: `HumanQueueAgent` MUST provide operations to list queue depths, claim tickets, and resolve tickets. Customer PII unmasking from `redaction_maps` MUST be executed by `HumanQueueAgent` under dedicated identity `sa-queue` holding Cloud SQL database role `queue_rw` (with `SELECT` grant on `redaction_maps`), and MUST be restricted exclusively to the human operator who has actively claimed the ticket. Every re-identification call MUST emit an immutable audit event to `ticket_events` logging `operator_id`, `ticket_id`, timestamp, and fields unmasked.
- **Rationale**: Enforces strict customer data privacy and role separation: only `queue_rw` (`sa-queue`) has access to `redaction_maps`; `ticket_rw` (`sa-ticket-mcp`) has NO access to `redaction_maps`, preventing unauthorized data browsing and satisfying C3 confidential compliance (TDD §3.5, §5.2).
- **Acceptance**: `tests/agents/test_human_queue.py::test_queue_claim_and_pii_reid`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
- **Statement**: MCP servers MUST expose typed JSON contracts with strict input/output validation:
  1. `ticket-db-mcp`: `upsert_ticket(idempotency_key, customer_id, channel, body_redacted, decision) -> {ticket_id, status}`, `claim_ticket(ticket_id, agent_id) -> {success}`, `resolve_ticket(ticket_id, agent_id, resolution_notes) -> {status}`, `reidentify_pii(ticket_id, agent_id) -> {unmasked_fields, error}` (denies with `forbidden_role_ticket_rw_cannot_access_redaction_maps_use_human_queue` because database role `ticket_rw` has no access to `redaction_maps`).
  2. `order-lookup-mcp`: `get_order(order_id) -> {order_id, status, items, tracking_number, promised_delivery, delivery_address_redacted}`.
  Agents reach the database exclusively through MCP servers over Cloud Run HTTPS with OIDC auth (`roles/run.invoker`); agents (`sa-triage@`, `sa-faq@`) MUST NOT hold Cloud SQL client permissions or Postgres database roles. Dedicated Cloud SQL IAM database roles are held strictly by MCP and queue service accounts: `orders_ro` held by `sa-order-mcp@` (read-only `orders_mock` with Postgres RLS), `ticket_rw` held by `sa-ticket-mcp@` (read-write `tickets`, `ticket_events`; NO access to `redaction_maps`), and `queue_rw` held by `sa-queue@` (`tickets`, `ticket_events`, decrypt `redaction_maps`). Customer ID MUST be extracted from verified session claims, never passed as an untrusted tool parameter.
- **Rationale**: Prevents BOLA/IDOR vulnerabilities (TDD §8.4; AGENTS.md rule 7) and enforces strict least-privilege database isolation without direct agent-to-DB coupling.
- **Acceptance**: `tests/mcp/test_tool_contracts.py::test_schemas_and_roles`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies
- **Statement**: Outbound client calls to external dependencies (`router-s1` endpoint, Agent Search, and MCP services) MUST sit behind in-memory circuit breakers configured for: 3 consecutive failures $\to$ `OPEN` state for 30 s base cooldown, exponential doubling up to 300 s cap, and $\pm 20\%$ random jitter. In `HALF_OPEN` state, 1 canary probe is permitted, requiring 2 consecutive successes to transition to `CLOSED`. When `router-s1` is open/unhealthy, triage MUST fall back to Tier 2 `gemini-3.8-flash`. When Agent Search is open/unhealthy, resolution MUST immediately route to human queues with a degraded notification.
- **Rationale**: Prevents cascading failures and protects user experience during infrastructure outages (TDD §3.4, §10.4; AGENTS.md rule 5).
- **Acceptance**: `tests/common/test_resilience.py::test_circuit_breakers_and_fallbacks`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-012: Execution Budgets, Service Timeouts, and Latency SLAs
- **Statement**: Service calls MUST enforce strict timeout budgets and connection pooling:
  - Clients MUST reuse connections via persistent pooled keep-alive HTTPS sessions (`requests.Session` with `HTTPAdapter(pool_connections, pool_maxsize, max_retries=0)`). Unpooled HTTPS/urllib connections incur ~185 ms p95 TLS handshake overhead. Persistent keep-alive connections achieve shared endpoint latency of p50 96 ms / p95 134 ms, and dedicated endpoint latency of p50 54 ms / p95 77 ms / p99 135 ms.
  - `TriageRouterAgent` overall turn budget: $\le 3,000\text{ ms}$. Tier 1 `router-s1` endpoint call HTTP timeout: 500 ms (protecting 150 ms model budget); Tier 2 Flash fallback timeout: 1,500 ms.
  - `FaqResolverAgent` overall turn budget: $\le 20,000\text{ ms}$. Agent Search query timeout: 800 ms; Laya rerank call HTTP timeout: 600 ms (protecting 200 ms model budget); `order-lookup-mcp` timeout: 300 ms; `ticket-db-mcp` timeout: 500 ms.
- **Rationale**: Enforces customer-facing latency SLAs and eliminates TLS connection setup overhead across repeated agent requests (TDD §3.5, §10.1).
- **Acceptance**: `tests/common/test_resilience.py::test_latency_budgets_and_timeouts`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-013: Centralized Model Configuration Resolution
- **Statement**: Agents and helper services MUST load all model IDs, endpoint URLs, and generation parameters dynamically from `src/ticket_router/config` using `models.get(role)` or `models.model_id(role)` (resolving roles `answer`, `router_tier2`, `tier3_multihop`). Hardcoded model identifier strings are strictly prohibited in application source code.
- **Rationale**: Decouples model version upgrades from application deployments and complies with AGENTS.md Non-negotiable 2.
- **Acceptance**: `tests/config/test_agent_models.py::test_model_registry_resolution`
- **Status**: open
- **Owner**: agent-service

### REQ-AGENT-014: Observability: Distributed Tracing & PII-Free Structured Logging
- **Statement**: All agent components MUST propagate W3C `traceparent` headers across Gateway, A2A, ReAct reasoning steps, MCP requests, and Pub/Sub events. Logs MUST be emitted as structured JSON containing `ticket_id`, `session_id`, `decision`, `tier`, `queue`, `degraded`, and `latency_ms`. Customer message bodies, raw queries, unmasked PII, and credentials MUST NEVER be written to logs.
- **Rationale**: Guarantees end-to-end request visibility in Cloud Trace while preserving PII compliance (TDD §3.1, §8; AGENTS.md rule 3).
- **Acceptance**: `tests/common/test_observability.py::test_otel_spans_and_pii_free_logs`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-015: Test Strategy: Hermetic Unit Tests, Schema Contracts, and E2E Smoke
- **Statement**: The agent test suite MUST provide: (1) hermetic unit tests with fakes (`FakeRouterEndpoint`, `FakeAgentSearch`, `FakeMcpClient`) requiring zero network or cloud credentials; (2) contract tests validating MCP request/response payloads against their formal JSON schemas; and (3) an end-to-end integration smoke test verifying the complete routing and answering pipeline across simulated tickets.
- **Rationale**: Ensures rapid local developer test cycles, catches schema regressions, and verifies multi-agent wiring before deployment (TDD §7.1, §12.1).
- **Acceptance**: `tests/agents/test_agent_smoke.py::test_e2e_triage_smoke`
- **Status**: open
- **Owner**: agent-service

---

## 4. Open Questions / Deltas

1. **GKE Autopilot vs. Cloud Run for FaqResolverAgent**: TDD §3.5 establishes GKE Autopilot as the primary target for `FaqResolverAgent` to support HPA autoscaling (2–10 pods) under high-throughput RAG search. In early dev/staging environments, deploying `FaqResolverAgent` to Cloud Run as a secondary target is permitted until GKE cluster provisioning is completed.
2. **Cloud SQL Ticket Store Implementation Sync**: The Cloud SQL tables (`tickets`, `ticket_events`, `orders_mock`, `redaction_maps`) and Postgres IAM roles (`orders_ro`, `ticket_rw`, `queue_rw`) are being provisioned by worker T. `ticket-db-mcp` and `order-lookup-mcp` will initially test against in-memory SQLite mocks before binding to Cloud SQL Auth Proxy.
3. **Endpoint Client Latency Headroom & Connection Pooling**: In-region live measurements from `laya-tune` (150 requests): unpooled HTTPS/urllib connection setup incurs ~185 ms p95 overhead. Persistent keep-alive HTTPS connections reduce shared endpoint latency to p50 96 ms / p95 134 ms, and dedicated endpoint latency to p50 54 ms / p95 77 ms / p99 135 ms. To satisfy customer SLAs while providing safe headroom against network jitter, client-side HTTP timeouts are set to 500 ms for route and 600 ms for rerank, and clients MUST maintain persistent pooled keep-alive connections via `requests.Session` with `HTTPAdapter`.

---

## Deployment (Terraform)

### REQ-AGENT-016: Dedicated Agent Service Accounts and Least-Privilege IAM
- **Statement**: The agent tier MUST use dedicated Google Cloud Service Accounts: `sa-triage` for TriageRouter and `sa-faq` for FaqResolver (via GKE Workload Identity `ksa-faq` in namespace `faq-resolver`). Permissions MUST follow strict least privilege: resource-level `roles/aiplatform.predictionUser` on `router-s1` endpoint, `roles/aiplatform.user` for Gemini calls, `roles/discoveryengine.viewer` for Agent Search (FaqResolver only), `roles/pubsub.publisher` on `human-queue` for TriageRouter, and `roles/pubsub.subscriber` on `human-queue-sub` for `sa-queue`. Neither agent SA may be granted database roles directly; all database access occurs via Cloud Run MCP microservices.
- **Rationale**: Defense-in-depth isolation preventing agent prompt injection or model breakout from compromising customer order/ticket tables or unauthorized services.
- **Acceptance**: `tests/infra/test_agent_infra_static.py::test_agents_iam_least_privilege`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-017: Agent Runtime Deployment for TriageRouterAgent
- **Statement**: TriageRouterAgent is deployed to Vertex AI Agent Engine / Reasoning Engine via the Vertex AI SDK (`scripts/deploy_agents/deploy_triage_agent_runtime.py`) or Cloud Run fallback. The deployment packaging script MUST isolate execution environments by setting `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` in-process before importing or serializing agent closures, preventing local workstation project names from baking into the pickled artifact, and pin runtime dependencies (`cloudpickle==3.0.0`, `pydantic>=2.0.0`, `google-adk>=0.1.0`). The Reasoning Engine service agent MUST be granted `roles/storage.objectViewer` on the staging bucket.
- **Rationale**: Vertex AI Reasoning Engine manages serverless session state and Python code execution natively; isolation prevents deployment failure across dev/prod environments.
- **Acceptance**: `tests/infra/test_agent_infra_static.py::test_triage_deployment_script`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-018: GKE Autopilot Workload Identity & HPA for FaqResolverAgent
- **Statement**: FaqResolverAgent is deployed to a private GKE Autopilot cluster (`support-agents`) in `us-central1` on the `REGULAR` release channel, hosted on a dedicated VPC `support-vpc` and subnet `support-us-central1` (`10.10.0.0/20`, pods `10.20.0.0/14`, services `10.24.0.0/20`) with Private Google Access enabled. The deployment MUST use Kubernetes Workload Identity binding `ksa-faq` in namespace `faq-resolver` to `sa-faq@<project>.iam.gserviceaccount.com`. Pod scaling MUST be managed by HorizontalPodAutoscaler (`faq-resolver-hpa`) targeting 60% CPU utilization with minReplicas 1 and maxReplicas 10. Manifests MUST be applied via `scripts/deploy_agents/deploy_faq_gke.sh`. Cloud NAT is omitted as private worker nodes only communicate with Google APIs.
- **Rationale**: High-throughput RAG search requires elastic horizontal pod autoscaling with zero node management overhead, isolated private networking, and secure token exchange via Workload Identity.
- **Acceptance**: `tests/infra/test_agent_infra_static.py::test_faq_gke_manifests`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-019: Cloud Run MCP Microservices with IAM Authentication and Zero Standby Cost
- **Statement**: Shared backend microservices (`ticket-db-mcp`, `order-lookup-mcp`, `human-queue`) MUST be deployed as Cloud Run v2 services (`google_cloud_run_v2_service`) with `min_instance_count = 0` (cost posture minimal) and `max_instance_count = 3`. Ingress MUST be configured to `INGRESS_TRAFFIC_ALL` with strict IAM authorization (`roles/run.invoker` granted exclusively to authorized caller SAs `sa-triage` and `sa-faq`); public unauthenticated invocation (`allUsers`) is strictly forbidden. If `agents_image` is empty, service creation is conditionally omitted (`count = 0`).
- **Rationale**: Eliminates idle compute expenditure when no tickets are active while enforcing cryptographic identity checks on every tool call.
- **Acceptance**: `tests/infra/test_agent_infra_static.py::test_cloud_run_services_iam_and_cost_posture`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-020: Resilient Escalation Queue with Dead-Letter Handling
- **Statement**: Pub/Sub escalation infrastructure MUST provide a primary topic `human-queue`, a dead-letter topic `human-queue-dlq`, and a pull subscription `human-queue-sub` configured with a dead-letter policy (`max_delivery_attempts = 5`). Subscription retries MUST use exponential backoff (min 10s, max 600s). `sa-triage` is granted `roles/pubsub.publisher` on `human-queue` and `sa-queue` is granted `roles/pubsub.subscriber` on `human-queue-sub`.
- **Rationale**: Guarantees that unprocessable escalation events are not lost or stuck in infinite retry loops, providing operators a DLQ for poison-pill diagnosis.
- **Acceptance**: `tests/infra/test_agent_infra_static.py::test_pubsub_escalation_and_dlq`
- **Status**: done
- **Owner**: agent-service

