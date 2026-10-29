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
    [p_esc < 0.15 & faq]          [p_esc >= 0.50 | urgent]
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
| `REQ-AGENT-001` | Multi-Agent Package Structure and Deployment Topology | `done` | `tests/agents/test_package_layout.py::test_directory_structure` |
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
| `REQ-AGENT-013` | Centralized Model Configuration Resolution | `done` | `tests/agents/test_tier2_location.py::test_tier2_invoker_location_precedence` |
| `REQ-AGENT-014` | Observability: Distributed Tracing & PII-Free Structured Logging | `done` | `tests/common/test_observability.py::test_otel_spans_and_pii_free_logs` |
| `REQ-AGENT-015` | Test Strategy: Hermetic Unit Tests, Schema Contracts, and E2E Smoke | `done` | `tests/agents/test_agent_smoke.py::test_e2e_triage_smoke` |
| `REQ-AGENT-016` | Dedicated Agent Service Accounts and Least-Privilege IAM | `done` | `tests/infra/test_agent_infra_static.py::test_agents_iam_least_privilege` |
| `REQ-AGENT-017` | Agent Runtime Deployment for TriageRouterAgent | `done` | `tests/infra/test_agent_infra_static.py::test_triage_deployment_script` |
| `REQ-AGENT-018` | GKE Autopilot Workload Identity & HPA for FaqResolverAgent | `done` | `tests/infra/test_agent_infra_static.py::test_faq_gke_manifests` |
| `REQ-AGENT-019` | Cloud Run MCP Microservices with IAM Authentication and Zero Standby Cost | `done` | `tests/infra/test_agent_infra_static.py::test_cloud_run_services_iam_and_cost_posture` |
| `REQ-AGENT-020` | Resilient Escalation Queue with Dead-Letter Handling | `done` | `tests/infra/test_agent_infra_static.py::test_pubsub_escalation_and_dlq` |
| `REQ-AGENT-021` | GKE Node Artifact Registry Pull Access and Least-Privilege Node Identity | `done` | `tests/infra/test_agent_infra_static.py::test_gke_node_artifact_registry_reader` |
| `REQ-AGENT-022` | Out-of-Scope Query Refusal via Tier-2 System Prompt | `done` | `tests/agents/test_out_of_scope.py::test_out_of_scope_refusal_end_to_end` |
| `REQ-AGENT-023` | Gemini Enterprise Web App Front-Door and ADK Agent Registration | `done` | `tests/infra/test_ge_app_and_trace.py::test_ge_app_infra_and_registration_contract` |
| `REQ-AGENT-024` | Conversational Greeting and Pleasantry Handling | `done` | `tests/agents/test_greetings.py::test_conversational_greetings_do_not_escalate` |
| `REQ-AGENT-025` | Gemini Call Deadlines, Hedged Requests, and Router Thinking Cap | `done` | `tests/common/test_hedging.py::test_hedged_gemini_calls_bound_tail_latency` |
| `REQ-AGENT-026` | Deterministic Order-Lookup Intent Rule and Pleasantry Coverage | `done` | `tests/agents/test_order_lookup_rule.py::test_order_lookup_rule_skips_tier2_safely` |
| `REQ-AGENT-027` | Concurrent Turn I/O, Non-Fatal Ticket Persistence, and Post-Response Trace Flush | `done` | `tests/agents/test_triage_concurrency.py::test_triage_parallel_io_and_nonfatal_upsert` |


---

## 3. Detailed Requirements

### REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
- **Statement**: The agent codebase MUST be structured under `src/ticket_router/agents/{triage_router,faq_resolver,human_queue}/` and `src/ticket_router/mcp/{ticket_db,order_lookup}/`. Runtimes MUST map to: `TriageRouterAgent` on GEAP Agent Runtime (SA `sa-triage@`), `FaqResolverAgent` on GKE Autopilot Deployment with HPA and Workload Identity (K8s SA `ksa-faq` bound to `sa-faq@`), `HumanQueueAgent` on Cloud Run (SA `sa-queue@`), and MCP servers on Cloud Run (`sa-order-mcp@`, `sa-ticket-mcp@`). ALL infrastructure (SAs, IAM roles, Cloud Run services, GKE Autopilot cluster + Deployment/HPA/KSA binding, Agent Runtime deployment config, Pub/Sub topics) MUST be declared in Terraform under `deploy/terraform/single-project/modules/*`; imperative scripts are permitted only for artifact builds and model/agent deployment steps that the Terraform provider cannot express, with each exception explicitly documented.
- **Rationale**: Deconstructs monolithic routing into isolated, least-privilege services adhering to TDD §3.5 and GCP security boundaries, with declarative infrastructure reproducibility.
- **Acceptance**: `tests/agents/test_package_layout.py::test_directory_structure`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
- **Statement**: `TriageRouterAgent` MUST evaluate inbound messages sequentially through: (1) Tier 0 deterministic safety, abuse, and keyword regex rules; (2) Tier 1 `router-s1` encoder via `:rawPredict` evaluating `{escalate, sentiment, urgency, intent}`; and (3) Tier 2 `gemini-3.8-flash` invoked ONLY when Tier 1 confidence lands in the uncertainty band ($\tau_{\text{low}} \le p_{\text{esc}} < \tau_{\text{esc}}$) or predicts `intent="other"`. Standard operating thresholds MUST be loaded from configuration: $\tau_{\text{esc}} = 0.50$, $\tau_{\text{low}} = 0.15$, $\tau_{\text{intent}} = 0.70$. Tier 0, Model Armor, and degraded fallback escalations emit canonical `predicted_intent="other"` with the specific trigger preserved in `reason` and `details["category"]`.
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
- **Statement**: `FaqResolverAgent` MUST execute a bounded ReAct loop constrained to $\le 6$ tool calls, $\le 2$ retrieval passes, $\le 1$ answer regeneration, and a 20-second wall-clock budget. Document retrieval MUST query Agent Search `support-kb-search` using production configuration `intent_soft_noisy_0.2` (`relevanceThreshold=LOWEST`, query cleaning, soft intent boost 0.2), followed by cross-encoder candidate scoring against the `router-s1` Vertex Prediction endpoint (`task: "rerank"`). If order-specific information is required (`ORD-...` identifier, canonical order/shipping/refund intents `order_status`, `shipping_policy`, `returns_refunds`, or order/purchase history keywords including `order`, `orders`, `recent`, `purchase`, `purchases`, `bought`, `shipment`, `shipments`, `history`), unverified sessions MUST receive a sign-in prompt without invoking `order-lookup-mcp`, while verified sessions MUST query `order-lookup-mcp` via `get_order(order_id)` when an `ORD-...` ID is present or `list_orders(limit=5)` when no explicit order ID was supplied. Answer synthesis (`prompts/faq_answer_v2.yaml`, `faq_answer_version: "v2"`) MUST treat `Order Context` as the authenticated customer's authoritative live order data from the order database—directly presenting verified `order_id`, `status`, `items`, `tracking_number`, and `promised_delivery` details (or stating clearly if no matching orders were found) alongside $\ge 1$ verbatim policy citation from `Retrieved Documents`, and MUST NEVER claim it lacks access to personal order history when `Order Context` is populated.
- **Rationale**: Guarantees bounded execution time and prevents infinite reasoning loops while applying validated top-k retrieval, reranking models, and natural order lookup for signed-in customers without requiring memorized order numbers (TDD §3.3, §3.5, §6.3, specs/rag-retrieval.md).
- **Acceptance**: `tests/agents/test_faq_resolver.py::test_bounded_react_and_rerank`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-008: Mandatory Grounded Citations with Non-Hallucination Fallback
- **Statement**: Every answer synthesized by `FaqResolverAgent` MUST include $\ge 1$ verbatim citation quotation matching text in the retrieved `faq-articles` documents. Citation validation and search snippet ingestion MUST normalize inline HTML highlight tags (e.g. `<b>`, `<strong>`, `<em>`, `<i>`, `<mark>`, `<span>`) and HTML entities (e.g. `&nbsp;`, `&amp;`, `&#39;`) from Agent Search snippets while preserving strict alphanumeric substring matching. If citation validation fails, the agent MUST attempt exactly one answer regeneration with explicit grounding constraints. If validation fails a second time, the agent MUST NOT return an ungrounded or speculative response to the customer; it MUST update ticket status to `ESCALATED`, assign queue `human-standard` with reason `uncited_answer`, and return a calming handoff message.
- **Rationale**: Prevents hallucinated retail policies, tolerates Discovery Engine snippet highlight markup, and enforces the zero-unverified-answer invariant (TDD §3.3, §6.4).
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
  2. `order-lookup-mcp`: `get_order(order_id) -> {order_id, status, items, tracking_number, promised_delivery, delivery_address_redacted}`, `list_orders(limit=5) -> {orders: [{order_id, status, items, tracking_number, promised_delivery, delivery_address_redacted}]}`.
  Agents reach the database exclusively through MCP servers over Cloud Run HTTPS with OIDC auth (`roles/run.invoker`); agents (`sa-triage@`, `sa-faq@`) MUST NOT hold Cloud SQL client permissions or Postgres database roles. Dedicated Cloud SQL IAM database roles are held strictly by MCP and queue service accounts: `orders_ro` held by `sa-order-mcp@` (read-only `orders_mock` with Postgres RLS), `ticket_rw` held by `sa-ticket-mcp@` (read-write `tickets`, `ticket_events`; NO access to `redaction_maps`), and `queue_rw` held by `sa-queue@` (`tickets`, `ticket_events`, decrypt `redaction_maps`). Customer ID MUST be extracted from verified session claims (`X-Customer-ID` + `X-Customer-Verified: true`), never passed as an untrusted tool parameter.
- **Rationale**: Prevents BOLA/IDOR vulnerabilities (TDD §8.4; AGENTS.md rule 7) and enforces strict least-privilege database isolation without direct agent-to-DB coupling.
- **Acceptance**: `tests/mcp/test_tool_contracts.py::test_schemas_and_roles`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies
- **Statement**: Outbound client calls to external dependencies (`router-s1` endpoint, Agent Search, and MCP services) MUST sit behind in-memory circuit breakers configured for: 3 consecutive failures $\to$ `OPEN` state for 30 s base cooldown, exponential doubling up to 300 s cap, and $\pm 20\%$ random jitter. In `HALF_OPEN` state, 1 canary probe is permitted, requiring 2 consecutive successes to transition to `CLOSED`. When `router-s1` is open/unhealthy, triage MUST fall back to Tier 2 `gemini-3.8-flash`. When Agent Search is open/unhealthy or when an A2A handoff to `FaqResolverAgent` times out or raises an exception, resolution MUST immediately degrade to human escalation (`human-standard`, `degraded=True`) with a calming notification.
- **Rationale**: Prevents cascading failures and protects user experience during infrastructure outages (TDD §3.4, §10.4; AGENTS.md rule 5).
- **Acceptance**: `tests/common/test_resilience.py::test_circuit_breakers_and_fallbacks`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-012: Execution Budgets, Service Timeouts, and Latency SLAs
- **Statement**: Service calls MUST enforce strict timeout budgets and connection pooling:
  - Clients MUST reuse connections via persistent pooled keep-alive HTTPS sessions (`requests.Session` with `HTTPAdapter(pool_connections, pool_maxsize, max_retries=0)`). Unpooled HTTPS/urllib connections incur ~185 ms p95 TLS handshake overhead. Persistent keep-alive connections achieve shared endpoint latency of p50 96 ms / p95 134 ms, and dedicated endpoint latency of p50 54 ms / p95 77 ms / p99 135 ms.
  - `TriageRouterAgent` overall turn budget: $\le 3,000\text{ ms}$ for Tier 0/1 decisions. Tier 1 `router-s1` endpoint call HTTP timeout: 500 ms (protecting 150 ms model budget); Tier 2 Flash routing is bounded by the hedged-call deadline in `REQ-AGENT-025` (hedge after 3.5 s, hard deadline 9 s).
  - `FaqResolverAgent` overall turn budget: $\le 20,000\text{ ms}$ (and `HttpA2AClient` default timeout `20.0\text{ s}`). Agent Search query timeout: 800 ms; Laya rerank call HTTP timeout: 600 ms (protecting 200 ms model budget); `order-lookup-mcp` HTTP timeout: 5,000 ms (accommodating `min_instance_count = 0` Cloud Run + Cloud SQL IAM cold starts while preserving $< 300\text{ ms}$ warm p95); `ticket-db-mcp` HTTP timeout: 3,000 ms (scale-to-zero cold starts; warm p95 $< 400\text{ ms}$, failures are non-fatal per `REQ-AGENT-027`); answer synthesis bounded by the hedged-call deadline in `REQ-AGENT-025` (hedge after 7 s, hard deadline 15 s). FastAPI `/resolve` executes `handle_a2a_request` via `starlette.concurrency.run_in_threadpool` so synchronous downstream calls do not block the event loop or starve GKE `/healthz` probes.
- **Rationale**: Enforces customer-facing latency SLAs and eliminates TLS connection setup overhead across repeated agent requests (TDD §3.5, §10.1).
- **Acceptance**: `tests/common/test_resilience.py::test_latency_budgets_and_timeouts`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-013: Centralized Model Configuration Resolution
- **Statement**: Agents and helper services MUST load all model IDs, endpoint URLs, model serving locations (e.g. `global` for `gemini-3.8-flash` in `GeminiAnswerClient` and `router_tier2` in `Tier2GeminiInvoker`), and generation parameters dynamically from `src/ticket_router/config` using `models.get(role)`, `models.model_id(role)`, or `models.model_location(role)` (resolving roles `answer`, `router_tier2`, `tier3_multihop`). The model's registry `location` MUST take strict precedence over `GOOGLE_CLOUD_LOCATION` / `Settings.location` for every Gemini caller, including the Tier 2 router (`router_tier2` → `global`); `Settings.location` is only the fallback when a role has no configured location in the registry. Hardcoded model identifier strings and hardcoded location literals are strictly prohibited in application source code.
- **Rationale**: Decouples model version upgrades from application deployments and complies with AGENTS.md Non-negotiable 2.
- **Acceptance**: `tests/agents/test_tier2_location.py::test_tier2_invoker_location_precedence`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-014: Observability: Distributed Tracing & PII-Free Structured Logging
- **Statement**: All agent and MCP components MUST propagate W3C `traceparent` headers across Gateway, Agent Engine (`TriageRouterAgent`), A2A (`FaqResolverAgent` on GKE Autopilot), ReAct reasoning steps, Cloud Run MCP/queue requests (`ticket-db-mcp`, `order-lookup-mcp`, `human-queue`), and Pub/Sub events, exporting spans to Google Cloud Trace when `ENABLE_CLOUD_TRACE` is enabled:
  1. `ticket_router.common.observability.configure_tracing(service_name, project_id)` MUST lazily import `opentelemetry-sdk` and `opentelemetry-exporter-gcp-trace` (`CloudTraceSpanExporter`) so offline/CI `[dev]` environments without the exporter package continue to run hermetically with `NoOpSpan`, while production runtimes (`[agents]` / `PINNED_REQUIREMENTS`) register a `BatchSpanProcessor` (or `SimpleSpanProcessor` on flush) and force `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT` so ADK/GenAI auto-instrumentation never exports raw prompts or responses.
  2. `start_span(name, attributes, traceparent)` MUST sanitize all span attributes via `sanitize_log_dict`, extract remote parent `SpanContext` (`is_remote=True`) when a valid W3C `traceparent` is supplied, and bind the active span's formatted `traceparent` into a `contextvars.ContextVar` (`get_current_traceparent()`) so outbound HTTP clients (`HttpA2AClient`, `HttpTicketDbMcpClient`, `HttpOrderLookupClient`) and `A2AHandoffPayload.traceparent` link child spans under the same 128-bit `traceId`. `serve.py` MUST configure tracing and attach HTTP middleware extracting inbound `traceparent` / `x-cloud-trace-context` headers on non-health routes.
  3. Terraform (`modules/agents-iam` and `project_services`) MUST enable `cloudtrace.googleapis.com` and `telemetry.googleapis.com` and grant least-privilege `roles/cloudtrace.agent` to `sa-triage`, `sa-faq`, `sa-ticket-mcp`, `sa-order-mcp`, `sa-queue`, and the Vertex AI Reasoning Engine service agent (`service-<NUM>@gcp-sa-aiplatform-re.iam.gserviceaccount.com`).
  4. Logs MUST be emitted as structured JSON containing `ticket_id`, `session_id`, `decision`, `tier`, `queue`, `degraded`, and `latency_ms`. Customer message bodies, raw queries, unmasked PII, and credentials MUST NEVER be written to logs or span attributes.
- **Rationale**: Guarantees end-to-end multi-runtime trace correlation in Cloud Trace (`Agent Engine -> GKE -> Cloud Run`) while strictly enforcing C3 PII compliance and offline CI isolation (TDD §3.1, §8; AGENTS.md rule 3).
- **Acceptance**: `tests/common/test_observability.py::test_otel_spans_and_pii_free_logs`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-015: Test Strategy: Hermetic Unit Tests, Schema Contracts, and E2E Smoke
- **Statement**: The agent test suite MUST provide: (1) hermetic unit tests with fakes (`FakeRouterEndpoint`, `FakeAgentSearch`, `FakeMcpClient`) requiring zero network or cloud credentials; (2) contract tests validating MCP request/response payloads against their formal JSON schemas; and (3) an end-to-end integration smoke test verifying the complete routing and answering pipeline across simulated tickets.
- **Rationale**: Ensures rapid local developer test cycles, catches schema regressions, and verifies multi-agent wiring before deployment (TDD §7.1, §12.1).
- **Acceptance**: `tests/agents/test_agent_smoke.py::test_e2e_triage_smoke`
- **Status**: done
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
- **Statement**: TriageRouterAgent is deployed to Vertex AI Agent Engine / Reasoning Engine via the Vertex AI SDK (`scripts/deploy_agents/deploy_triage_agent_runtime.py`) or Cloud Run fallback. The deployment packaging script MUST isolate execution environments by setting `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, and `PYTHONPATH=/code/src:src:.` in-process and in container `env_vars` before importing or serializing agent closures, preventing local workstation project names from baking into the pickled artifact, stage both `src/ticket_router` and `prompts` via `extra_packages`, normalize `staging_bucket` to `gs://<bucket>` for `vertexai.init`, and pin runtime dependencies (`cloudpickle==3.0.0`, `pydantic>=2.0.0`, `google-adk>=0.1.0`). `TriageRouterAdkAgent` MUST preserve lazy `_router` re-initialization across `cloudpickle` deserialization, and `agent_tuning.py` MUST resolve `agent_tuning.yaml` and `prompts/tier2_router_*.yaml` cleanly across source checkouts, container builds (`services/Dockerfile`), and installed/unpickled Agent Engine runtimes. The Reasoning Engine service agent MUST be granted `roles/storage.objectViewer` on the staging bucket.
- **Rationale**: Vertex AI Reasoning Engine manages serverless session state and Python code execution natively; isolation prevents deployment failure across dev/prod environments.
- **Acceptance**: `tests/infra/test_agent_infra_static.py::test_triage_deployment_script`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-018: GKE Autopilot Workload Identity & HPA for FaqResolverAgent
- **Statement**: FaqResolverAgent is deployed to a private GKE Autopilot cluster (`support-agents`) in `us-central1` on the `REGULAR` release channel, hosted on a dedicated VPC `support-vpc` and subnet `support-us-central1` (`10.10.0.0/20`, pods `10.20.0.0/14`, services `10.24.0.0/20`) with Private Google Access enabled. The deployment MUST use Kubernetes Workload Identity binding `ksa-faq` in namespace `faq-resolver` to `sa-faq@<project>.iam.gserviceaccount.com`. Pod scaling MUST be managed by HorizontalPodAutoscaler (`faq-resolver-hpa`) targeting 60% CPU utilization with minReplicas 1 and maxReplicas 10. Manifests MUST be applied by the approval-gated CD pipeline (`cicd/cloudbuild-deploy.yaml`, REQ-CD-005). Cloud NAT (`support-vpc-router`/`support-vpc-nat`) provides egress because Private Google Access does not cover regional endpoints such as `modelarmor.us-central1.rep.googleapis.com`, which FaqResolver calls to screen answers (REQ-MA-004; decision 2026-10-28).
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

### REQ-AGENT-021: GKE Node Artifact Registry Pull Access and Least-Privilege Node Identity
- **Statement**: GKE Autopilot worker nodes executing containerized agent workloads (such as `FaqResolverAgent`) MUST possess `roles/artifactregistry.reader` permission on the serving Artifact Registry repository (`serving`). In the initial deployment, this is granted to the default Compute Engine service account (`data.google_compute_default_service_account`) to avoid cluster recreation. On the next scheduled cluster rebuild, a dedicated least-privilege node service account (`gke-nodes`) MUST be provisioned with strictly scoped roles: `logging.logWriter`, `monitoring.metricWriter`, `monitoring.viewer`, `stackdriver.resourceMetadata.writer`, and `artifactregistry.reader`.
- **Rationale**: Autopilot nodes require image pull authentication to retrieve private images (`agents:<tag>`) without granting unnecessary broad project permissions.
- **Acceptance**: `tests/infra/test_agent_infra_static.py::test_gke_node_artifact_registry_reader`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-022: Out-of-Scope Query Refusal via Tier-2 System Prompt
- **Statement**: Unrelated or off-topic queries outside Global Retail Hub customer support (e.g., stock prices, weather, recipes, general trivia, coding/homework) MUST NOT escalate to `human-standard` or invoke `FaqResolverAgent`. Specifically: (1) Versioned Tier-2 prompt configs (`prompts/tier2_router_*.yaml`) MUST support an optional `system_instruction` field loaded via `ticket_router.config.agent_tuning.get_tier2_system_instruction()` and passed via `GenerateContentConfig(system_instruction=...)` in `invoke_tier2_gemini` when present (preserving exact call signatures for legacy `v1`/`v2` templates without `system_instruction`). (2) `prompts/tier2_router_v3.yaml` (active in `agent_tuning.yaml`) MUST define the Global Retail Hub support domain boundary in `system_instruction`, use the canonical 5-intent vocabulary (`order_status`, `shipping_policy`, `returns_refunds`, `account_payment`, `other`), and emit `out_of_scope: bool` (`true` with `intent: "other", escalate: false` for unrelated non-support queries; `false` with `escalate: true, intent: "other"` for in-domain support issues lacking a self-service FAQ such as app crashes, estate transfers, or vague support cries). (3) `RouterPrediction` MUST carry `out_of_scope: bool = False`. (4) `TriagePolicy.evaluate` MUST enforce the Add-Only Escalation Invariant (`REQ-AGENT-003`: `final_escalate = rules OR tier1 OR tier2` ALWAYS supersedes `out_of_scope`); only when `final_escalate` is `False` and `t2_pred.out_of_scope` is `True` does `TriagePolicy` emit `decision="reject_oos"`, `tier="tier2_flash"`, `queue="none"`, `predicted_intent="other"`, `escalate=False`, `reason="tier2_out_of_scope"`. (5) For `decision == "reject_oos"`, `TriageRouterAgent.triage` MUST render a deterministic scope-boundary message (`render_out_of_scope_refusal(ticket_id)`), persist the ticket via `ticket-db-mcp` with `status="resolved_auto"` and `assigned_queue=None` so it never enters `HumanQueueAgent` open queues, skip `FaqResolverAgent` A2A handoff, and return the boundary refusal via `TriageRouterAdkAgent` / `extract_answer_from_result`.
- **Rationale**: Calm out-of-distribution queries predict `intent="other"` or low confidence in Tier 1 (`router-s1`) and fall through to Tier 2 (`gemini-3.8-flash`). Refusing unrelated queries deterministically at Tier 2 prevents human support queue bloat and avoids unnecessary RAG/rerank calls while strictly preserving safety and anger/abuse escalation invariants (decision 2026-10-28).
- **Acceptance**: `tests/agents/test_out_of_scope.py::test_out_of_scope_refusal_end_to_end`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-023: Gemini Enterprise Web App Front-Door and ADK Agent Registration
- **Statement**: The system MUST expose an authenticated Gemini Enterprise Web App front-door (`support-assistant-app` in `global`, `appType = "APP_TYPE_INTRANET"`, `SEARCH_TIER_ENTERPRISE` + `SEARCH_ADD_ON_LLM`) registered with `TriageRouterAgent` on Vertex AI Agent Engine (`projects/620485621180/locations/us-central1/reasoningEngines/8384262420809383936`) while preserving cost isolation on the RAG corpus search engine (`support-kb-search` MUST remain on `SEARCH_TIER_STANDARD` with `search_add_ons = []`):
  1. **Terraform Module (`deploy/terraform/single-project/modules/ge-app`)**: MUST provision dedicated empty `NO_CONTENT` backing data store `support-ge-default-ds` in `global`, grant the Discovery Engine service agent (`service-<NUM>@gcp-sa-discoveryengine.iam.gserviceaccount.com`) `roles/aiplatform.user` so Gemini Enterprise can invoke Vertex AI Reasoning Engines, provision Secret Manager secret shell `ge-oauth-client-credentials` (holding out-of-state Web OAuth 2.0 `{"client_id", "client_secret"}` JSON for Gemini Enterprise end-user OAuth), and declare an importable `google_discovery_engine_search_engine.support_assistant_app` resource (`count`-gated via `var.manage_ge_search_engine`) so the created engine is tracked in Terraform state with zero drift.
  2. **Provider Gap Automation (`scripts/deploy_agents/provision_ge_app.py`)**: Because pinned `hashicorp/google` `v6.50.0` lacks `aclConfig`, `LicenseConfig` / `UserStore` license allocation, `app_type = "APP_TYPE_INTRANET"`, `authorizations`, and `assistants/default_assistant/agents` resources, an idempotent CLI (`--ensure-acl`, `--ensure-license`, `--ensure-engine`, `--ensure-oauth`, `--register-agent`, `--verify`, `--dry-run`) MUST configure location `aclConfig` (`idpType = "GSUITE"`), ensure an active Gemini Enterprise `LicenseConfig` (`free_trial_gemini`) with auto-registration on `default_user_store` and seat assignment for the operator principal, create/verify the `APP_TYPE_INTRANET` engine `support-assistant-app`, create/update project-level Discovery Engine `Authorization` `support-customer-oauth` (`serverSideOauth2` reading credentials from Secret Manager `ge-oauth-client-credentials` with `clientSecret` strictly redacted from all stdout/summaries, gracefully skipping when no secret version exists yet), register/update `TriageRouterAgent` (`adkAgentDefinition.provisionedReasoningEngine.reasoningEngine` + `authorizationConfig.toolAuthorizations` when OAuth authorization is active) with starter prompts and org sharing, and resolve `widgetConfigs/default_search_widget_config` to output the direct Gemini Enterprise chat URL.
- **Rationale**: Delivers the TDD §3.1 Gemini Enterprise front-door web experience, end-user Google OAuth 2.0 consent (`REQ-IDENT-009`), and ADK session routing without upgrading `support-kb-search` off `SEARCH_TIER_STANDARD`, while honoring the Terraform-first rule and documenting the exact `v6.50.0` provider boundary (`REQ-AGENT-001`).
- **Acceptance**: `tests/infra/test_ge_app_and_trace.py::test_ge_app_infra_and_registration_contract`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-024: Conversational Greeting and Pleasantry Handling
- **Statement**: Standalone conversational greetings, capability openers, and polite thanks/closings with no support issue, safety/legal trigger, abuse, or human-agent request (e.g., `"hey"`, `"hi"`, `"hello"`, `"good morning"`, `"hey how can you help"`, `"what can you help me with?"`, `"thanks"`, `"thank you"`) MUST NOT escalate to human queues or emit `#TICK-...` 2-hour SLA acknowledgements:
  1. **Tier 0 Deterministic Rules (`evaluate_tier0_rules` in `policy.py`)**: Evaluated strictly AFTER emergency/legal (`_EMERGENCY_REGEX`), abuse (`_ABUSE_REGEX`), and explicit human-agent (`_HUMAN_REQUEST_REGEX`) rules so `REQ-AGENT-003` add-only escalation ALWAYS takes precedence, anchored greeting and pleasantry patterns MUST emit `Tier0Result(matched=True, escalate=False, category="greeting" | "pleasantry", reason="tier0_conversational_greeting" | "tier0_conversational_pleasantry")`, which `TriagePolicy.evaluate` maps to canonical `RouterDecision(decision="auto_rag", tier="tier0_rules", queue="none", predicted_intent="other", escalate=False, details={"category": ...})`. Vague distress cries (`"not working ?"`, `"help me"`, `"can you help ?"`) and greetings combined with actual support inquiries (`"hey where is my order"`) MUST NOT match standalone greeting short-circuiting.
  2. **Tier 2 Alignment (`prompts/tier2_router_v4.yaml`)**: Standalone polite greetings, thanks, or capability openers that reach Tier 2 MUST classify as `intent: "other", escalate: false, out_of_scope: false` (while vague distress cries stay `escalate: true`).
  3. **Conversational Reply & Ticket Auto-Close (`TriageRouterAgent.triage`, `templates.py`, `ticket_db/server.py`)**: When `decision == "auto_rag"` and `decision.details.get("category") in ("greeting", "pleasantry")`, `TriageRouterAgent` MUST return a deterministic friendly welcome/capability prompt or polite closing (`render_conversational_reply`) via `handoff_response={"status": "AUTO_RESOLVED", "route": "resolved", "answer": ...}` without invoking `FaqResolverAgent` A2A handoff, and `ticket-db-mcp` MUST persist the row with `status="resolved_auto"` and `assigned_queue=None` so it never enters `HumanQueueAgent` open queues.
- **Rationale**: Prevents chat openers like `"hey"` in Gemini Enterprise from creating spurious open human support tickets with 2-hour wait notifications while preserving canonical routing taxonomy (`auto_rag` / `other` / `none`) and sub-second conversational responsiveness.
- **Acceptance**: `tests/agents/test_greetings.py::test_conversational_greetings_do_not_escalate`
- **Status**: done
- **Owner**: agent-service



### REQ-AGENT-025: Gemini Call Deadlines, Hedged Requests, and Router Thinking Cap
- **Statement**: Every live `gemini-3.8-flash` call on the customer path (Tier 2 routing in `invoke_tier2_gemini` and FAQ answer synthesis in `GeminiAnswerClient.generate_answer`) MUST run through a shared hedged-call helper `ticket_router.common.hedging.hedged_call(fn, hedge_after_s, deadline_s, max_attempts=2)`:
  1. **Hedging**: start attempt 1; if it has not completed after `hedge_after_s`, start one identical attempt 2 in parallel and return whichever succeeds first. If an attempt fails with an exception before the hedge point, the hedge starts immediately. At most `max_attempts` calls are in flight, and losing or stale attempts are abandoned with `executor.shutdown(wait=False, cancel_futures=True)` so a hung call never blocks the turn.
  2. **Deadline**: if no attempt succeeds within `deadline_s`, raise `ServiceTimeoutError`. Tier 2 then follows the existing degraded fallback hierarchy (`REQ-AGENT-011`: human escalation, `degraded=True`), and answer synthesis follows the existing non-hallucination escalation path (`REQ-AGENT-008`), so no ungrounded answer is ever returned.
  3. **Budgets** (constants in `ticket_router.common.resilience`): `TIER2_HEDGE_AFTER_S = 3.5`, `TIER2_DEADLINE_S = 9.0`, `ANSWER_HEDGE_AFTER_S = 7.0`, `ANSWER_DEADLINE_S = 15.0`, so answer synthesis plus retrieval stays inside the 20 s A2A budget.
  4. **Router thinking cap**: `models.yaml` `router_tier2` MUST set `thinking_level: "LOW"`, and `invoke_tier2_gemini` MUST pass `ThinkingConfig(thinking_level=model_thinking_level("router_tier2"))` alongside the system instruction (`MINIMAL` is rejected by `gemini-3.8-flash` with HTTP 400, probed 2026-10-29).
  5. **Observability**: each hedged call MUST record span attributes `hedge.attempts`, `hedge.winner` (1 or 2), and `hedge.timed_out` (bool), with no prompt or response content.
- **Rationale**: The live latency baseline (`evals/results/latency/run_002_baseline`, 160 turns) showed `gemini-3.8-flash` latency is bimodal: Tier 2 routing p50 3.8 s / p90 24.9 s / max 35 s and answer synthesis p50 3.9 s / p90 28 s / max 43 s. 7.5% of turns hit the 20 s A2A timeout and returned a false human-escalation ack for simple FAQ questions. A single hedge at roughly the warm p95 bounds the tail at the cost of ~10% extra model calls, and LOW thinking cut router p50 from 2.49 s to 1.87 s with identical decisions in a 12-call probe (decision 2026-10-29).
- **Acceptance**: `tests/common/test_hedging.py::test_hedged_gemini_calls_bound_tail_latency`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-026: Deterministic Order-Lookup Intent Rule and Pleasantry Coverage
- **Statement**: Common calm order-lookup and closing messages MUST NOT fall through to Tier 2:
  1. **Order-lookup rule (post-Tier-1)**: in `TriagePolicy.evaluate`, only AFTER Tier 0, Model Armor ingress checks, and a successful Tier 1 prediction that is NOT a definitive escalation (`escalate_prob < tau_esc`, sentiment not `angry`, urgency not `immediate`), a message that fully matches the anchored `_ORDER_LOOKUP_REGEX` MUST return `RouterDecision(decision="auto_rag", tier="tier1_router_s1", queue="none", predicted_intent="order_status", escalate=False, reason="tier1_order_lookup_rule", details={"category": "order_lookup", ...s1 probabilities})` instead of invoking Tier 2. The regex covers calm own-order history and order-ID lookups only (e.g. "what are my orders recently ?", "what did I buy recently?", "show my recent purchases", "can you list my orders from the last month", "my order history", "where is ORD-10042?", "status of ORD-10001", "track ORD-10151") and MUST NOT match messages containing complaints, refunds/returns, cancellation, damage, or distress words. If Tier 1 is unavailable (breaker open or error), the rule MUST NOT apply and Tier 2 runs as before, so anger detection is never skipped (`REQ-AGENT-003`).
  2. **Pleasantry coverage (Tier 0)**: `_PLEASANTRY_REGEX` MUST also match standalone closings such as "have a (great|good|nice|wonderful) day", "appreciate (it|your help|that)", "cheers", "ok thanks", "that's (helpful|great|perfect), thanks", "bye"/"goodbye", which return `REQ-AGENT-024` pleasantry handling.
  3. Both rules MUST produce zero matches on any `escalate` or `reject_oos` gold case across `evals/data/routing_v2.1.0/{dev,heldout,regression}.jsonl` and `evals/data/agent_e2e_v1/{dev,test}.jsonl`.
- **Rationale**: In the latency baseline, Tier 2 decided 39% of turns. Every "my recent orders" phrasing, unknown order IDs, and closings like "appreciate your help" took 3–35 s in Tier 2; signed-out users waited up to 30 s for a sign-in prompt, and one closing escalated to a human after 24.7 s. Applying the rule only after a non-escalating Tier 1 prediction keeps the angry/urgent invariant intact (decision 2026-10-29).
- **Acceptance**: `tests/agents/test_order_lookup_rule.py::test_order_lookup_rule_skips_tier2_safely`
- **Status**: done
- **Owner**: agent-service

### REQ-AGENT-027: Concurrent Turn I/O, Non-Fatal Ticket Persistence, and Post-Response Trace Flush
- **Statement**: `TriageRouterAgent.triage` MUST minimize serial I/O without changing routing semantics:
  1. **Concurrent identity resolution**: `resolve_identity(...)` (the `ticket-db-mcp` `resolve_customer` call for OAuth turns) MUST run concurrently with `TriagePolicy.evaluate(...)`, propagating `contextvars` (trace context) into worker threads; the policy result does not depend on identity.
  2. **Concurrent ticket upsert**: on the `auto_rag` A2A path, `upsert_ticket` MUST run concurrently with the `FaqResolverAgent` handoff; both complete before the turn returns.
  3. **Non-fatal persistence**: an `upsert_ticket` failure or timeout MUST NOT fail the customer turn (previously a cold `ticket-db-mcp` surfaced streamed error 498). The agent MUST retry once, then log a PII-free `ticket_upsert_failed` warning and still return the decision response. `TICKET_DB_MCP_TIMEOUT_S` is raised to 3.0 s for scale-to-zero cold starts.
  4. **Post-response trace flush**: `TriageRouterAdkAgent._run_async_impl` MUST yield the response event BEFORE calling `flush_traces(...)`, so time-to-first-event does not include span export.
- **Rationale**: The baseline measured `resolve_customer` ~230 ms and `upsert_ticket` 250–370 ms running serially on every FAQ/order turn, about 0.9 s of fixed Agent Engine overhead per turn (partly the synchronous trace flush before the response), and a cold-start 498 error on the first greeting after idle (decision 2026-10-29).
- **Acceptance**: `tests/agents/test_triage_concurrency.py::test_triage_parallel_io_and_nonfatal_upsert`
- **Status**: done
- **Owner**: agent-service
