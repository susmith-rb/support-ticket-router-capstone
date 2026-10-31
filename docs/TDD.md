# Support Ticket Router & Assistant — Technical Design Doc

- **Author:** Susmith Barigidad (Cloud AI FDE)
- **Last Updated:** Oct 31, 2026
- **Status:** Implemented & Live-Verified (Target Architecture Reference)
- **Customer:** Global Retail Hub (simulated international e-commerce)
- **Code Repository:** [https://github.com/cloud-ai-fde/susmith-capstone-support-ticket-router](https://github.com/cloud-ai-fde/susmith-capstone-support-ticket-router)
- **Full Detailed Spec (20 Sections):** [docs/TDD_DETAILED.md](TDD_DETAILED.md)

---

## 1. Executive Summary

Global Retail Hub operates ~1,200 Tier-1 **human support reps** across 24×7 shifts (steady-state capacity ~266k tickets/day at a 6.5-minute average handle time, ~89k per 8-hour shift; peak holiday surges reach 2×–4× steady volume). In the legacy human-only state, every inbound chat, email, and social message lands in a single undifferentiated queue worked first-in-first-out (FIFO). Note: throughout this document, **"AI agent"** refers strictly to an autonomous ADK software agent, while **"human support rep"** refers to a human specialist. **CSAT** is defined as the percentage of 4–5 ratings on a 1–5 post-interaction customer survey (0–100 scale; Gemini Enterprise thumbs up/down serves as the online directional proxy).

| Baseline Dimension | Legacy Human-Only FIFO Baseline (Illustrative) | Why It Matters |
| :--- | :--- | :--- |
| **Median first-response time (FRT), all channels** | 4 h 10 m steady-state (12+ h during seasonal surges) | CSAT drops ~15 pts when FRT exceeds 1 h *(illustrative industry benchmark)* |
| **Angry/urgent tickets waiting in the general FIFO queue** | 38% wait > 8 h; mean 6.5 h | Highest-churn cohort receives the slowest response |
| **Share of volume in top-8 factual FAQ intents** *(shipping, returns, refund status, order status, password, promo code, payment failed, cancel request)* | 62% *(measured on the `AmazonHelp` slice of `twcs.csv`)* | Deflectable with a cited, policy-grounded answer |
| **Blended human rep cost per ticket** | \$4.80 *(AHT 6.5 min across ~266k tickets/day capacity)* | Primary ROI denominator for safe deflection |
| **Prior naive LLM-only chatbot pilot** *(separate pilot baseline)* | Hard-failed on shared GenAI `HTTP 429` quota bursts (~11k tickets dropped in one peak event, illustrative) | Quota independence and zero-drop fallback are business requirements |
| **Cross-session context carry-over** | None; returning customers re-explain prior issues | Inflates AHT and repeat-contact frustration |

**Solution.** A sentiment- and intent-aware support router and grounded FAQ assistant built with **Google ADK** across a heterogeneous three-runtime topology — **Vertex AI Agent Engine** (`TriageRouterAgent` on SPIFFE Agent Identity, with a warm **Cloud Run** fallback host `triage-fallback`), **GKE Autopilot** (`FaqResolverAgent` on GKE Agent Identity SPIFFE), and **Cloud Run** (`human-queue` Human Handoff Service, `human-review-ui` behind Cloud Run IAP, `ticket-db-mcp`, and `order-lookup-mcp`) — governed by **Agent Gateway (`tr-egress`) / Agent Registry** and surfaced in **Gemini Enterprise (`support-assistant-app`)**. A deterministic-first router (`Tier 0` rules → `Tier 1` fine-tuned `router-s1` encoder → `Tier 2` `gemini-3.8-flash` → narrow `Tier 3` `gemini-3.1-pro-preview`) classifies each Model Armor–tokenized message (`support-router-lean`); payment card numbers are rejected at ingress (`HTTP 400`) to stay out of PCI-DSS scope. Angry or urgent tickets invoke the shared deterministic `HumanHandoff` module, bypassing automated policy generation to `human-urgent` / `human-standard` with a non-LLM calming acknowledgement and holding the session (`0` model calls on follow-ups) until a human rep resolves or returns the ticket to automation (`/reopen`). Neutral factual inquiries retrieve from **Agent Search (`support-kb-search`)**, rerank via the co-located `router-s1` choice head, verify order ownership via KMS-signed **Customer Context Tokens (CCT)** + PostgreSQL RLS, and enforce **100% verbatim citation precision**. Operational state lives in **Cloud SQL PostgreSQL 16** (with BigQuery analytics and Redis semantic caching as `(Stretch)` extensions), active dialogue in **Vertex AI Sessions**, and PII-free durable preferences in **Vertex AI Memory Bank**.

### 1.1 North-Star Business Outcomes

- **Immediate Priority for Angry & Urgent Customers:** Every angry or urgent customer bypasses automated answers directly into a priority human queue (`human-urgent` 15-min SLA target, `human-standard` 2-hr SLA target) with an immediate ticket reference and gaming-resistant within-lane ordering based on verified account impact.
- **Lower Cost per Resolved Inquiry via Grounded Deflection:** Safely deflect > 40% of total inbound volume away from the \$4.80/ticket human FIFO queue while guaranteeing zero uncited or fabricated policy claims.
- **Zero Dropped Customer Inquiries:** Maintain continuous intake across model `429` bursts, endpoint redeployments, or database restarts via per-dependency circuit breakers, fail-open persistence, warm Cloud Run runtime fallback, and replayable Pub/Sub queues.
- **Zero PII or Cross-Customer Data Exposure:** Keep raw PII out of LLM prompts, logs, and Memory Bank via ingress tokenization + KMS envelope-encrypted redaction maps, and enforce cryptographic end-to-end customer isolation from OAuth/CIAM login down to PostgreSQL Row-Level Security.

---

## 2. System Architecture

```mermaid
flowchart LR
  subgraph Surfaces["Surfaces"]
    GE["Gemini Enterprise App (support-assistant-app: customer chat + OAuth)"]
    API["Ticket Ingest API /v1/tickets (CRM webhooks, web chat, email)"]
    UI["human-review-ui (Cloud Run + direct IAP for human support reps)"]
  end
  subgraph Edge["Edge, Identity & Governance"]
    CIAM["Identity Platform (CIAM) + Google OAuth 2.0"]
    GW["Agent Gateway tr-egress (AGENT_TO_ANYWHERE: IAP V2 + IAM v3 UAP ENFORCE)"]
    REG["Agent Registry (faq-resolver A2A card + core-gapi MCP/REST catalog)"]
    MA["Model Armor support-router-lean (PI/JB, URI, RAI, Cloud SDP support_router_inspect)"]
    KMS["Cloud KMS (ES256 customer-context-signer + redaction-map-wrapper)"]
  end
  subgraph Compute["Heterogeneous 3-Runtime Compute Plane (us-central1)"]
    subgraph Runtime["Vertex AI Agent Engine (Front-Door AI Agent)"]
      TR["TriageRouterAgent (SPIFFE Agent Identity + shared HumanHandoff)"]
      SES["Vertex AI Sessions (active dialogue turns)"]
      MEM["Vertex AI Memory Bank (user_id=customer_id, 180d TTL, PII-free)"]
    end
    subgraph GKE["GKE Autopilot support-agents (Kubernetes AI Agent)"]
      FR["FaqResolverAgent (ksa-faq-agent SPIFFE + HPA 1-10 + shared HumanHandoff)"]
    end
    subgraph CRun["Cloud Run v2 (Serverless Services, MCP Boundaries & Warm Fallback)"]
      TFB["triage-fallback (warm min=1 ADK fallback host)"]
      HQ["human-queue (Non-LLM Human Handoff Service: Pub/Sub push, KMS re-ID, /reopen)"]
      OL["order-lookup-mcp (CCT_MODE=require, ALLOWED_CALLER_SPIFFE_IDS, orders_ro)"]
      TK["ticket-db-mcp (ticket_rw: tickets, ticket_events, customer_identities)"]
    end
  end
  subgraph Models["Model Serving Tier"]
    S1["router-s1-dedicated (Co-located Laya route + rerank on GPU, :rawPredict)"]
    GF["Gemini 3.8 Flash (global: Tier-2 uncertain router + grounded RAG synthesis)"]
    GP["Gemini 3.1 Pro Preview (global: narrow Tier-3 multihop fallback)"]
  end
  subgraph Data["Data, Search & Messaging"]
    AS["Agent Search support-kb-search (global SEARCH_TIER_STANDARD: faq-articles + resolved-threads)"]
    SQL["Cloud SQL PostgreSQL 16 ticket-store (IAM DB auth, Postgres RLS, KMS redaction_maps)"]
    PS["Pub/Sub human-queue + human-queue-dlq (push to human-queue)"]
    BQ["BigQuery support_analytics + Memorystore Semantic Cache (Stretch)"]
  end
  subgraph Ops["Observability & CI/CD"]
    OT["OpenTelemetry -> Cloud Trace + PII-Free Cloud Logging + Monitoring"]
    CB["Cloud Build CI (unit >=80% cov, SDD traceability, retrieval gate) + Approval-Gated CD"]
  end
  GE --> CIAM --> TR
  API --> CIAM
  UI --> HQ
  TR --> GW
  GW -.-> REG
  TR --> MA
  FR --> MA
  TR --> KMS
  HQ --> KMS
  TR -- "A2A + ES256 CCT" --> FR
  TR -. "failover_triage.sh" .-> TFB
  TR -- ":rawPredict (route)" --> S1
  FR -- ":rawPredict (rerank)" --> S1
  TR -- "Tier 2 / Tier 3" --> GF
  TR -. "Tier 3 (<0.70 / multihop)" .-> GP
  FR --> GF
  FR --> AS
  FR -- "MCP + CCT" --> OL
  TR -- "MCP" --> TK
  FR -- "MCP" --> TK
  HQ --> SQL
  OL --> SQL
  TK --> SQL
  TR --> SES
  TR --> MEM
  TR -- "HumanHandoff" --> PS
  FR -- "HumanHandoff" --> PS
  PS -- "OIDC push" --> HQ
  SQL -.-> BQ
  TR -. OTel .-> OT
  FR -. OTel .-> OT
  CB -- "parallel rollout + smoke + rollback" --> TR
  CB --> FR
  CB --> CRun
```

### 2.1 GCP Project Structure & Regional Topology

- **Project:** `susmith-fde-ticket-router` (`620485621180`, Argolis org `349636765856`). Infrastructure is codified across two Terraform roots (`deploy/terraform/single-project` on Google provider `6.50` and `deploy/terraform/agent-gateway` on `7.46`) backed by versioned GCS state (`gs://susmith-fde-ticket-router-tfstate`).
- **`us-central1` (co-located compute, security & transactional data plane):**
  - **Heterogeneous 3-Runtime ADK Topology:** **Vertex AI Agent Engine** (`TriageRouterAgent`, engine `2170218524805431296` running on SPIFFE Agent Identity `ticketRouterTriageRuntime`), **GKE Autopilot** (`FaqResolverAgent` on private cluster `support-agents` via dedicated KSA `ksa-faq-agent` SPIFFE Agent Identity `ticketRouterAgentInference`, HPA 1–10 pods, Cloud NAT for regional `*.rep.googleapis.com` egress), and **Cloud Run v2** (`human-queue` Human Handoff Service, `human-review-ui` behind direct Cloud Run IAP, warm `triage-fallback` with `min_instance_count=1`, and MCP boundaries `order-lookup-mcp` + `ticket-db-mcp`).
  - **Model Serving (`router-s1-dedicated`):** Dedicated Vertex AI custom container (`serving/laya/`) co-locating both the fine-tuned `router-s1` v2 classifier (`task: "route"`, $T=1.18$) and the fine-tuned Laya v2 choice reranker (`task: "rerank"`) in `< 2.5 GB` VRAM (`1× NVIDIA T4` on `n1-standard-4` in the sandbox; multi-zone `2× L4` on `g2-standard-4` at production Tier M+).
  - **Operational State & Governance:** **Cloud SQL PostgreSQL 16** (`ticket-store`, `ENTERPRISE`, `db-custom-1-3840`, `ZONAL` in sandbox with `REGIONAL` synchronous HA as the documented production upgrade path; IAM DB authentication, automated backups + PITR, empty `authorized_networks`, 10s connector timeout + fail-open persistence), **Vertex AI Sessions** + **Memory Bank** (`180`-day TTL), **Agent Gateway** (`tr-egress`, `AGENT_TO_ANYWHERE` with IAP V2 + IAM v3 UAP `ENFORCE`), **Model Armor** (`support-router-lean` + Cloud SDP `support_router_inspect`), **Cloud KMS** (`ticket-router-identity` keyring), **Pub/Sub** (`human-queue` + `human-queue-dlq`), Artifact Registry (`serving`), and Cloud Build CI/CD.
- **`global`:** **Gemini Enterprise** web app (`support-assistant-app`, registered agent `11280911196073273342` + OAuth authorization `support-customer-oauth`), **Agent Search** (`support-kb-search` on `SEARCH_TIER_STANDARD` over `faq-articles` + `resolved-threads`; retained in `global` after empirical `us` multi-region evaluation showed a `-2.7 pp` `recall@5` regression vs. `global`, with US/EU regional stores scheduled for Phase 3), **Agent Registry**, and Gemini `global` serving endpoints (`gemini-3.8-flash`, `gemini-3.1-pro-preview`).
- **Stretch Extensions (`(Stretch)`):** **BigQuery** (`support_analytics` append-only event/eval sink) and **Memorystore Redis** semantic FAQ cache (explicitly marked `(Stretch)` so core runtime paths stay zero-stale-policy by default).

### 2.2 Architecture Principles

1. **Deterministic-First Tiered Routing.** Rules (`Tier 0`) → calibrated encoder (`Tier 1`) → `gemini-3.8-flash` (`Tier 2`) → narrow `gemini-3.1-pro-preview` (`Tier 3`); each tier handles only what the preceding tier cannot decide within its calibrated confidence band.
2. **Asymmetric, Monotonic Escalation Safety.** Any tier may *add* an escalation (`final.escalate = T0 ∨ T1 ∨ T2 ∨ T3`), and nothing downstream can clear it. Once escalated, multi-turn holding keeps follow-ups attached to the human ticket with `0` model calls until a human support rep resolves or reopens it.
3. **Managed Google Cloud Primitives over DIY.** Vertex AI Agent Engine, Gemini Enterprise, Vertex AI Sessions & Memory Bank, Agent Search, Agent Gateway & Registry, Model Armor + Cloud SDP, Cloud KMS, and Cloud Run IAP; custom Python code is strictly scoped to routing policy, bounded RAG validation, shared `HumanHandoff`, and narrow MCP boundaries.
4. **No Single Point of Failure (`Degraded ≠ Down`).** Every external call sits behind a bounded timeout and circuit breaker (`3` consecutive failures → `30 s` open ×2 cap `300 s` ±20% jitter), Cloud SQL persistence fails open, `triage-fallback` stays warm on Cloud Run, and the terminal fallback is always human escalation with a calming acknowledgement.
5. **PII Minimization & Cryptographic Customer Isolation.** Card data (`C4`) is rejected at ingress (`HTTP 400`); residual PII (`C3`) is tokenized before reaching any model, log, or Memory Bank, sealed in `redaction_maps` via Cloud KMS envelope encryption, and accessible only to `queue_rw` for human review; customer identity is cryptographically signed via KMS ES256 **Customer Context Tokens (CCT)** and enforced down to PostgreSQL Row-Level Security (RLS).
6. **Spec-Driven & Measurable.** Every requirement (`REQ-*`), model ID (`models.yaml`), prompt (`prompts/`), threshold (`agent_tuning.yaml`), corpus manifest, and Terraform module is versioned and gated in CI/CD.

---

## 3. Technical Components

### 3.1 Data Flow & PII Lifecycle

```mermaid
flowchart TD
  subgraph Offline["Offline: Corpus to Index & Checkpoints (Jetski Subagents, Zero Argolis Spend)"]
    K["Kaggle twcs.csv (2.8M tweets)"] --> F["Filter: AmazonHelp English (en) threads (81,103 reconstructed threads)"]
    F --> D1["Dedupe (exact + MinHash >= 0.85), strip rep signatures (^AB), URLs, DM boilerplate"]
    D1 --> P1["PII scrubber (order regex before Luhn card check + DLP): PII ABSENT from here on"]
    P1 --> L["Teacher labeling: Gemini 3.1 Pro (3x vote) -> 6,446 labeled threads (v2)"]
    L --> FAQ["FAQ synthesis + regrouping -> 50 curated v2 articles (single-sourced SIMULATED_POLICY + lint)"]
    FAQ --> DS1["Agent Search faq-articles (1 article = 1 chunk, status=active, versioned)"]
    P1 --> DS2["Agent Search resolved-threads (81,103 structured threads, secondary context)"]
    L --> FT["Fine-tune + calibrate router-s1 v2 (T=1.18) + Laya v2 choice reranker (1,323 graded queries)"]
  end
  subgraph Online["Online: Live Ticket Path"]
    M["Inbound customer turn"] --> IG["Model Armor support-router-lean + Cloud SDP support_router_inspect"]
    IG -- "CREDIT_CARD_NUMBER (C4)" --> BLK["HTTP 400 deterministic security refusal (0 DB/KMS/LLM writes)"]
    IG -- "Clean / C3 tokenized ([EMAIL_1], [PHONE_1])" --> T0["Session Hold Check (0 LLM calls if open human-*) -> Tier 0 Rules"]
    T0 --> T1["Tier 1 router-s1-dedicated (:rawPredict, <100ms)"]
    T1 -- "Uncertain / other" --> T2["Tier 2 Gemini 3.8 Flash -> narrow Tier 3 Gemini 3.1 Pro (<0.70 / multihop)"]
    T1 -- "Confident" --> DEC["Validated RouterDecision"]
    T2 --> DEC
    DEC --> DB["Cloud SQL tickets (tokenized body_redacted) + KMS-sealed redaction_maps (queue_rw only)"]
    DEC -- "escalate=true" --> ESC["Shared HumanHandoff -> Pub/Sub human-queue -> human-queue service + non-LLM calming ack"]
    DEC -- "auto_rag" --> RAG["FaqResolverAgent -> Agent Search + Laya rerank + CCT order-lookup-mcp -> 100% verbatim cited answer"]
    DEC --> MB["Vertex AI Memory Bank (user_id=customer_id, tokenized-only generate, 180d TTL)"]
  end
```

### 3.2 Sequence: Angry Escalation, Multi-Turn Hold & Return-to-Automation

```mermaid
sequenceDiagram
  participant C as Customer (GE App / API)
  participant TR as TriageRouterAgent (Agent Engine)
  participant MA as Model Armor (Cloud SDP)
  participant S1 as router-s1-dedicated
  participant TK as ticket-db-mcp (Cloud SQL)
  participant PS as Pub/Sub -> human-queue
  participant Rep as Human Support Rep (human-review-ui / IAP)
  C->>TR: Turn 1: "Where is my order #12345?? Third time asking. Ridiculous."
  TR->>MA: sanitizeUserPrompt (PI/JB + Cloud SDP inspect)
  MA-->>TR: Clean PI/JB; tokenized text (C4 card absent)
  TR->>S1: :rawPredict (task="route", timeout 500ms)
  S1-->>TR: p_esc=0.94 >= tau_esc(0.50), sentiment=angry, intent=order_status
  TR->>TK: upsert_ticket(status=open, assigned_queue=human-urgent, canonical_ticket_uuid)
  TR->>PS: HumanHandoff.escalate -> publish ticket.escalated/v1 -> queued_for_review
  TR-->>C: Deterministic non-LLM calming ack (#TICK-..., 15-min SLA ETA)
  C->>TR: Turn 2: "Still waiting, any update?"
  TR->>TK: get_active_session_ticket (1.5s fail-open) -> active human-urgent hold
  TR->>TK: append_customer_followup (0 calls to S1, Gemini, or FaqResolver)
  TR-->>C: Deterministic hold ack ("already with a support specialist (#TICK-...)")
  Rep->>PS: POST /tickets/{id}/reopen (IAP JWT -> X-Rep-Id) -> status=returned_to_automation
  C->>TR: Turn 3: "What is standard ground shipping speed?"
  TR->>TR: Hold released -> normal tiered routing -> grounded FAQ answer
```

### 3.3 Sequence: Grounded Factual & Order-Lookup Path with Cryptographic CCT

```mermaid
sequenceDiagram
  participant C as Customer (OAuth / CIAM)
  participant TR as TriageRouterAgent (SPIFFE)
  participant S1 as router-s1-dedicated
  participant KMS as Cloud KMS (ES256)
  participant FR as FaqResolverAgent (GKE SPIFFE)
  participant AS as Agent Search (global)
  participant OL as order-lookup-mcp (RLS)
  participant GF as Gemini 3.8 Flash + Model Armor
  C->>TR: "What is the status of order ORD-10001 and return window?"
  TR->>TR: Verify JWT/OAuth -> resolve customer_id=cust_1001 + 300ms Memory Bank retrieve
  TR->>S1: :rawPredict (task="route") -> escalate=false, intent=order_status
  TR->>KMS: asymmetricSign ES256 Customer Context Token (sub=cust_1001, 5m TTL)
  TR->>FR: A2A /resolve (OIDC via Agent Gateway tr-egress + X-Customer-Context-Token)
  FR->>AS: search(support-kb-search, soft intent boost 0.2, status="active", top_10)
  FR->>S1: :rawPredict (task="rerank", top 5 candidates)
  FR->>OL: get_order(ORD-10001) + SPIFFE Bearer + ES256 CCT
  OL->>OL: Check ALLOWED_CALLER_SPIFFE_IDS + KMS verify CCT + Postgres set_config('app.customer_id','cust_1001')
  OL-->>FR: Verified order record (uniform 404 not_found if other customer)
  FR->>GF: generate_answer (hedged at 4s, deadline 15s) + Model Armor sanitizeModelResponse
  FR->>FR: CitationValidator (100% verbatim normalized substring; static passage fallback on LLM timeout)
  FR-->>TR: Grounded answer + pinned citations (doc_id@version)
  TR-->>C: Restores turn-local customer PII tokens in-memory -> renders cited answer
```

### 3.4 Sequence: Circuit Breaker & Degraded Path

```mermaid
sequenceDiagram
  participant TR as TriageRouterAgent
  participant FR as FaqResolverAgent
  participant CB as CircuitBreaker(agent_search)
  participant AS as Agent Search
  participant HH as Shared HumanHandoff (DB + Pub/Sub)
  TR->>FR: Factual inquiry
  FR->>CB: call_with_timeout(search)
  CB->>AS: search -> 3 consecutive 429/5xx/timeouts
  Note over CB: State -> OPEN (30s base, x2 backoff cap 300s, +-20% jitter)
  CB-->>FR: Short-circuit in O(1) (< 1 ms breaker decision overhead)
  FR->>HH: escalate(queue=human-standard, degraded=true, reason=search_failure)
  FR-->>TR: Graceful calming handoff reply (0 uncited claims, 0 HTTP 5xx to caller)
  Note over CB: After jittered window -> HALF_OPEN (1 canary probe; 2 consecutive successes -> CLOSED)
```

### 3.5 Components & Runtime Boundaries

| Component | Type | Runtime | Model | Data / Tool Access | Identity & Security Boundary | Loop & Latency Guards |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`TriageRouterAgent`** *(Front-Door AI Agent + Shared `HumanHandoff`)* | **AI Agent** *(ADK `BaseAgent`)* | **Vertex AI Agent Engine** (`2170218524805431296`, published to GE App `11280911196073273342`) | **Tier 1:** `router-s1`<br>**Tier 2:** `gemini-3.8-flash`<br>**Tier 3:** `gemini-3.1-pro-preview` | `router-s1-dedicated`, `ticket-db-mcp`, `FaqResolver` (A2A), Pub/Sub `human-queue`, Vertex Sessions & Memory Bank, Model Armor, Cloud KMS | **SPIFFE Agent Identity** (`principal://agents.global.../2170218524805431296`) with custom role `ticketRouterTriageRuntime` + mTLS + Agent Gateway `tr-egress` (`ENFORCE`) | ≤ 1 Tier-2 + ≤ 1 Tier-3 call; 3.5s hedge / 9s T2 deadline; 2.5s T3 deadline; 20s A2A cap |
| **`triage-fallback`** *(Warm Standby Runtime)* | **AI Agent Host** | **Cloud Run v2** (`min_instance_count=1`) | Same `root_agent` & `env_contract.py` | Same downstream MCP, `router-s1`, A2A, and Pub/Sub tools (in-memory session fallback) | `sa-triage@` with strict IAM invokers (`sa-cd-deployer`, Discovery Engine SA); `scripts/failover_triage.sh` | Zero cold start (`min=1`); explicit session/memory degradation tags |
| **`FaqResolverAgent`** *(Bounded ReAct AI Agent + Shared `HumanHandoff`)* | **AI Agent** | **GKE Autopilot** (`support-agents`, HPA 1–10 pods, private nodes + Cloud NAT) | `gemini-3.8-flash` (`thinking_level=LOW`) + static grounded passage fallback | Agent Search (`support-kb-search`), `router-s1-dedicated` (`rerank`), `order-lookup-mcp`, `ticket-db-mcp`, Pub/Sub `human-queue`, Model Armor egress | **GKE Agent Identity SPIFFE** (`ksa-faq-agent` → `spiffe://agents.global...`, custom role `ticketRouterAgentInference`; rollback toggle to WI `ksa-faq`) | ≤ 6 tool calls, ≤ 2 retrieval passes, ≤ 1 regen, 4s hedge / 15s answer cap, **20s wall budget** |
| **`human-queue`** *(Human Handoff & Queue Service)* | **Non-LLM Service** *(FastAPI)* | **Cloud Run v2** | **None** *(strictly deterministic)* | Cloud SQL (`queue_rw` role: `tickets`, `ticket_events`, KMS-encrypted `redaction_maps`), Pub/Sub push `/pubsub/escalations`, `/reopen` | `sa-queue@`; `run.invoker` restricted to `sa-review-ui@` and `sa-queue-push@`; sole principal holding KMS `cryptoKeyDecrypter` on `redaction-map-wrapper` | Deterministic CRUD; rejects body-supplied `operator_id`; immutable audit events |
| **`human-review-ui`** *(Rep Triage Console)* | **Web Service** *(FastAPI)* | **Cloud Run v2** (`iap_enabled = true`) | **None** | Calls `human-queue` (`/queue`, `/claim`, `/reidentify`, `/resolve`, `/reopen`) | Direct **Cloud Run IAP** (`roles/iap.httpsResourceAccessor`); verifies IAP JWT email → propagates `X-Rep-Id` as `sa-review-ui@` | Zero direct DB access; rep identity bound to IAP assertion |
| **`order-lookup-mcp` & `ticket-db-mcp`** | **MCP Tool Servers** | **Cloud Run v2** | **None** | Cloud SQL `ticket-store` via Auth Proxy (10s connect timeout → fast `503`) | `sa-order-mcp@` (`ALLOWED_CALLER_SPIFFE_IDS` + KMS CCT `require` + `SELECT`-only `orders_ro` RLS) · `sa-ticket-mcp@` (`ticket_rw`, **denied** `redaction_maps`) | Uniform `404 not_found` on BOLA probes; `3.0s` / `5.0s` client timeouts |

**Orchestration Pattern:** Deterministic **router-dispatcher with A2A handoff and shared `HumanHandoff` library**, never an unconstrained LLM planner. `TriageRouterAgent` evaluates a typed `TriagePolicy` state machine; angry/urgent turns invoke `ticket_router.handoff.HumanHandoff` directly in-process without calling Agent Search or `FaqResolverAgent`. Only `FaqResolverAgent` executes a bounded ReAct synthesis loop, falling back to `HumanHandoff` if citations fail validation.

### 3.6 Tiered System-1 / System-2 Routing (Centerpiece)

**Why a fine-tuned System-1 encoder (`router-s1`) ahead of Gemini Flash?**

Three engineering and commercial reasons, ordered by operational impact:

1. **Determinism & Auditable Safety (Primary):** A discriminative bidirectional encoder (`ModernBERT-large` + Laya typed-decision head, ~421M params) produces a reproducible probability distribution in a single forward pass with zero token-sampling variance, zero prompt-drift risk, and an enforceable `add-only` escalation threshold (`p_esc ≥ τ_esc`). Because retail CSAT is governed by multi-hour human queue wait times rather than a 1–3 s routing delta, **routing determinism and invariant auditability**—not raw millisecond speed—are the primary reason to place an encoder in front of an LLM.
2. **Quota & Outage Independence:** `router-s1-dedicated` runs on dedicated GPU capacity isolated from shared Vertex Gemini QPM/TPM pools. During a regional Gemini `HTTP 429` burst—the exact failure mode that dropped ~11k tickets in the customer's prior chatbot pilot—`Tier 0 + Tier 1` continue classifying ~88% of traffic normally while uncertain turns fail safe to `human-standard`.
3. **Sub-100ms Tail Latency & High-Volume Unit Economics:** Through persistent keep-alive connection pooling, `router-s1-dedicated` achieves **54 ms p50 / 77 ms p95** over the network (vs. `1.7–7.4 s` p50 and bimodal `13–33 s` tails observed on unhedged `gemini-3.8-flash` calls). Economically, the encoder carries a fixed GPU floor that trades off against per-call LLM token spend (`~$0.00068`/call on `gemini-3.8-flash` including the 12.4% Tier-2 fallback share):
   - **Sandbox single-replica floor (`1× NVIDIA T4` on `n1-standard-4`, ~$310–$390/mo):** breaks even at **~0.55M–0.70M tickets/month** vs. `gemini-3.8-flash` routing.
   - **Production HA floor (`2× NVIDIA L4` across 2 zones on `g2-standard-4`, ~$1,240/mo):** breaks even at **~2.1M tickets/month** vs. `gemini-3.8-flash` once the 12.4% Tier-2 fallback spend is included (~5M/month vs. `gemini-3.1-flash-lite`). Below break-even, `ROUTER_TIER1_ENABLED=false` cleanly disables the GPU floor while preserving identical policy contracts.

| Tier | Component | Traffic Share (`routing_v2.1.0` / `v2`) | Latency (Measured) | Routing Policy & Safety Invariants |
| :--- | :--- | :--- | :--- | :--- |
| **Ingress Guard** | **Model Armor `support-router-lean` + Session Hold** | 100% pre-screen | **~90 ms p50 / 363 ms p95** *(in-region)* | Blocks `CREDIT_CARD_NUMBER` (`C4`) with `HTTP 400` (`0` storage/LLM calls); holds follow-ups on active `human-*` tickets (`0` model calls); tokenizes `C3` PII (`EMAIL`, `PHONE`, `SSN`, `IBAN`, `ADDRESS`) via Cloud SDP `support_router_inspect`; flags PI/JB `suspect=true`. |
| **Tier 0** | **Deterministic Rules & Regex** | ~4% decided directly | **< 2 ms** | Hard-escalate safety/legal/abuse/human-request lexicons (`chargeback`, `lawyer`, `stolen`, `fraud`), deterministic conversational greetings/pleasantries (`REQ-AGENT-024`), and `[ORDER]` extraction. **Add-only escalation invariant (`REQ-AGENT-003`).** |
| **Tier 1 (System 1)** | **`router-s1-dedicated` Encoder** (`Laya` ~421M params, `:rawPredict`) | **~60%–87.6% decided here** *(corpus-dependent)* | **54 ms p50 / 77 ms p95** *(pooled RPC; 43 ms in-process)* | Calibrated forward pass ($T=1.18$). Escalates if `p_esc ≥ τ_esc (0.50)`; routes to `auto_rag` if `p_esc < τ_low (0.15)` and `max(p_intent) ≥ τ_intent (0.70)` (plus post-Tier-1 authenticated order-lookup rule `REQ-AGENT-026`). |
| **Tier 2 (System 2)** | **Gemini 3.8 Flash** (`tier2_router_v5`, `thinking_level=LOW`, hedged at 3.5s) | **~12.4%–36%** *(uncertainty band / `other` / OOD)* | **~1.7–3.8 s p50** *(hedged, 9s cap)* | Invoked only when Tier 1 falls in `0.15 ≤ p_esc < 0.50`, predicts `intent="other"`, or misses entities. Emits canonical 5-intent schema or `reject_oos` (`REQ-AGENT-022`) for calm non-retail queries; escalation remains strictly add-only. |
| **Tier 3 (Multihop)** | **Gemini 3.1 Pro Preview** (`REQ-AGENT-002` narrow fallback + offline teacher) | **≤ 3%** *(low-conf `<0.70` / `needs_multihop` T2)* | **≤ 2.5 s** *(bounded single call)* | Budgeted tie-breaker when Tier 2 returns `< 0.70` confidence or `needs_multihop` on non-escalated turns; degrades safely to Tier 2 or `human-standard` on timeout. Also used offline (`3×` vote) for corpus teacher labeling. |

**Gaming-Resistant Priority Within Human Lanes (`REQ-AGENT-005`):** Every angry, urgent, or abusive ticket bypasses automated answers to a human queue (`100%` bypass invariant), preventing frustrated customers from arguing with a bot. Within `human-urgent`, however, queue rank is computed from **verified account/order signals** (`VerifiedFacts`: past-due delivery, payment/fraud flag, repeat contact count) rather than profanity or ALL-CAPS volume—so shouting `"URGENT!!!"` on a routine question cannot jump ahead of a calm customer whose medical shipment is overdue.

**Measured Model & Routing Benchmarks:**
- **`router-s1` v2 Held-Out Test (`n=646` teacher-labeled AmazonHelp threads):** Sentiment accuracy **80.2%** (`macro-F1 0.780` vs. `61.6%` zero-shot), 5-way intent accuracy **85.8%** (`macro-F1 0.819`, 15-bin `ECE = 0.0526` vs. `0.234` uncalibrated), urgency within-one **98.5%**, and **96.7% Tier 0+1 upper-bound recall** on the strict `angry ∪ immediate` set (`122/646`, assuming teacher-equivalent Tier 2 at `human 34.7% / Tier 2 12.4% / auto 52.9%`; broad `escalate` bound `91.4%`).
- **Four-Arm Router Cascade Ablation (`evals/results/routing/ablation_ece_summary.md`, `n=300` cross-family Claude consensus):** Neither `Rules-Only` (`18.9%` strict recall) nor standalone `Laya-Only` (`87.8%` strict recall at `77 ms` p95, `$0.27/1k`) satisfies the `≥ 0.98` strict safety hard gate, whereas `Gemini-Only` (`100.0%` strict recall) incurs `3,850 ms` p95 latency, `$1.68/1k` routing spend, and `100%` exposure to regional Gemini `429` outages. The **Hybrid Cascade (`Tier 0 + Tier 1 Laya + Tier 2 Gemini`)** reaches **`98.89%` (`89/90`)** strict recall and **`81.52%` overall escalation recall** (exceeding standalone Gemini's `79.35%` via add-only escalation) while deflecting **`67.0%`** of traffic off Tier-2 LLM calls (`$0.82/1k`, 15-bin Tier-1 deciding intent `ECE = 0.0694`).
- **Production Routing Harness (`routing_v2.1.0`, `n=519` English `dev` + `n=150` `regression`, `v4` prompt + `τ_esc=0.50`):** **100% taxonomy validity**, **0 prompt-injection violations**, **0 priority-gaming violations**, intent accuracy **86.51%** (`macro-F1 0.8500`), escalation recall **94.42%** with escalation precision **67.9%** (`routing_correctness = 73.80%`, `+23.32 pp` over `v1` baseline, McNemar $p < 10^{-6}$).

### 3.7 End-to-End Execution Loop

1. **Phase 1 — Identity, Ingress Guard & Hold Check (`TriageRouterAgent`):** Verify caller identity (`customer_id_token` Firebase/CIAM JWT or Gemini Enterprise Google OAuth `support-customer-oauth` → `customer_identities` lookup; anonymous callers map to `anon:<session_id>`). Run Model Armor `support-router-lean` (`sanitizeUserPrompt` with Cloud SDP `support_router_inspect`): if `CREDIT_CARD_NUMBER` is detected, immediately return `HTTP 400` (`card_data_blocked`) with zero DB/KMS/model writes. Check `ticket-db-mcp` (`1.5 s` fail-open) for an active `human-*` session hold; if held, append the tokenized follow-up and return a deterministic hold ack (`0` model calls).
2. **Phase 2 — Concurrent Memory Read & Tiered Triage:** Retrieve top-5 customer memories from Vertex AI Memory Bank (`scope={"user_id": customer_id}`, `300 ms` fail-open timeout) concurrently with `Tier 0 → Tier 1 → conditional Tier 2 → narrow Tier 3` routing. Seal any `C3` PII token mappings in `redaction_maps` using Cloud KMS envelope encryption (`redaction-map-wrapper`) and upsert the tokenized ticket (`canonical_ticket_uuid`) via `ticket-db-mcp` (`10 s` connect timeout, fail-open).
3. **Phase 3a — Shared Deterministic Escalation (`HumanHandoff`):** When `escalate=True`, `HumanHandoff` persists queue assignment (`human-urgent` 15-min SLA or `human-standard` 2-hr SLA), publishes `ticket.escalated/v1` to Pub/Sub `human-queue` (consumed via OIDC push by Cloud Run `human-queue` to log `queued_for_review`), and returns a templated non-LLM calming acknowledgement (`#TICK-...` + SLA ETA). Human support reps review, unmask `C3` PII (audited under `queue_rw`), resolve, or `/reopen` (`returned_to_automation`) via IAP-protected `human-review-ui`.
4. **Phase 3b — Grounded RAG & Verified Order Resolution (`FaqResolverAgent`):** Mint a 5-minute KMS-signed ES256 **Customer Context Token (CCT)** and dispatch via A2A through Agent Gateway `tr-egress` (`ENFORCE`). `FaqResolverAgent` queries Agent Search (`support-kb-search` in `global`, soft intent boost `0.2`, `status="active"`), reranks the top 5 via `router-s1-dedicated` (`task: "rerank"`), optionally invokes `order-lookup-mcp` (`ALLOWED_CALLER_SPIFFE_IDS` + CCT signature check + Postgres RLS `app.customer_id`), generates a structured `AgentAnswer` (`gemini-3.8-flash`, hedged at `4 s` / `15 s` cap, with static grounded passage fallback `REQ-AGENT-028` if Gemini stalls), screens output via Model Armor `sanitizeModelResponse`, and enforces **100% verbatim normalized substring citations** (escalating via `HumanHandoff` if ungrounded).
5. **Phase 4 — Turn-Local Token Restoration, Memory Generation & Telemetry:** Restore turn-local customer PII tokens in-memory only for the caller's response (persisted rows and logs stay tokenized), trigger non-blocking Vertex AI Memory Bank `memories.generate` (`180`-day TTL, tokenized text only) for verified customers, and flush W3C-correlated OpenTelemetry spans to Cloud Trace.

### 3.8 Context, State & Memory Separation

| Storage Layer | Scope | Retained Content | Strictly Excluded | Retention & Access Policy |
| :--- | :--- | :--- | :--- | :--- |
| **Vertex AI Sessions** | Active Conversation | Redacted/tokenized dialogue turns, ADK event stream, transient `support-customer-oauth` / `customer_id_token` exchange | Authoritative ticket queue state (lives in Cloud SQL per ADR-5), raw `C3`/`C4` PII | Managed session lifecycle (`24 h` idle TTL target) |
| **Vertex AI Memory Bank** | Verified Customer (`user_id = customer_id`) | Synthesized durable customer preferences & prior issue context generated strictly from tokenized text (`body_to_persist` + decision summary) | Anonymous turns (`anon:*`), card-blocked turns (`C4`), raw PII, order numbers, or injection-flagged inputs | `15,552,000 s` (`180` days) configured on engine `2170218524805431296`; `memories.delete` erasure support |
| **Cloud SQL `tickets`, `ticket_events`, `customers`, `orders_mock`** | Operational System of Record | `ticket_uuid` (`uuid5`), `session_id`, verified `customer_id`, tokenized `body_redacted`, `RouterDecision` JSON, queue/hold/reopen lifecycle events, cited answers, seeded orders | Raw `C3` PII values and `C4` card numbers | `2`-year operational retention; `ticket_rw` (`ticket-db-mcp`), `orders_ro` + RLS (`order-lookup-mcp`), `analyst_ro` (human SQL Studio, excludes `redaction_maps`) |
| **Cloud SQL `redaction_maps`** | `C3` Re-Identification Vault | Envelope-encrypted JSON (`kms_encrypted_dek` wrapped by Cloud KMS `redaction-map-wrapper` + AES-GCM ciphertext of `{token -> original_pii}`) | `C4` payment card numbers (blocked at ingress) | `30`-day TTL purge; `ticket_rw` **denied** at Postgres grant layer; `queue_rw` (`human-queue`) decrypts only for human rep review with append-only audit event |
| **BigQuery & Memorystore Semantic Cache** | `(Stretch)` Extensions | `(Stretch)` Append-only analytics/eval warehouse (`400 d`) and `(intent, embedding, faq_version)` FAQ response cache (`7 d`) | Raw PII and customer-/order-specific responses | Documented `(Stretch)` Phase-2/scale extensions |

---

## 4. Tooling, Open Standards, & External Integrations

| Standard / Managed Service | Architectural Role | Why Chosen Over DIY / Alternatives |
| :--- | :--- | :--- |
| **Google ADK (`google-adk`)** | Agent runtime contract (`TriageRouterAdkAgent`), session integration, deterministic tool/handoff wrappers | Native deployment to Vertex AI Agent Engine and Gemini Enterprise App while running identically on Cloud Run (`triage-fallback`) |
| **A2A Protocol (`/.well-known/agent-card.json`)** | Cross-runtime handoff (`TriageRouterAgent` on Agent Engine → `FaqResolverAgent` on GKE Autopilot) | Decouples managed front-door orchestration from Kubernetes RAG/GPU compute; carries W3C `traceparent` + KMS-signed CCT |
| **Model Context Protocol (MCP) / Narrow Tool Servers** | `ticket-db-mcp` (`ticket_rw`) and `order-lookup-mcp` (`orders_ro` + RLS) on Cloud Run | Enforces physical IAM + Postgres role separation so `FaqResolverAgent` cannot mutate tickets or read `redaction_maps`, and LLM tool arguments never carry `customer_id` |
| **Agent Gateway (`tr-egress`) & Agent Registry** | Outbound governance (`AGENT_TO_ANYWHERE`) with IAP V2 + IAM v3 Universal Access Policy (`ENFORCE`) over registered `faq-resolver` A2A card & `core-gapi` | Cryptographically restricts `TriageRouterAgent` SPIFFE principal to explicitly allow-listed downstream endpoints and APIs |
| **Model Armor + Cloud SDP + Cloud KMS** | `support-router-lean` prompt/response screening, `support_router_inspect` PII offsets, ES256 CCT signing, and `redaction-map-wrapper` envelope encryption | Managed regional security guardrails + hardware-backed cryptographic signing/encryption with zero static keys in containers |
| **OpenTelemetry → Cloud Trace & Cloud Logging** | W3C `traceparent` spans across Agent Engine, Agent Gateway, Cloud Run, and GKE (`NO_CONTENT` GenAI privacy mode) via mTLS GAPIC exporter | End-to-end latency attribution and PII-free structured decision logging across heterogeneous runtimes |
| **Pub/Sub (`human-queue` + `human-queue-dlq`)** | Asynchronous escalation event bus (`ticket.escalated/v1`) with authenticated OIDC push to `human-queue` and 5-attempt DLQ | Decouples human queue intake and future CRM webhooks (`CrmAdapter`) from synchronous customer turns |

---

## 5. Security, Zero-Trust Identity & Data Protection

### 5.1 Dual-Identity Zero-Trust Matrix (Actor Workload SPIFFE + Subject Context Token)

Every hop carries two distinct cryptographic identities: the **Actor** (which workload is calling) and the **Subject** (which end customer or human support rep the call is acting on behalf of). Static service-account keys and over-broad `roles/aiplatform.user` / `roles/editor` bindings are banned by CI static tests (`tests/infra/test_agent_infra_static.py`).

| Hop / Boundary | Actor Identity (Workload) | Subject Identity (User / Rep) | Enforcement & Least-Privilege Scope |
| :--- | :--- | :--- | :--- |
| **Customer → Gemini Enterprise / Ingest** | Discovery Engine service agent (`aiplatform.reasoningEngines.query` on `2170218524805431296`) | **Google OAuth 2.0** (`support-customer-oauth`) or **Identity Platform (CIAM)** JWT (`customer_id_token`); unauthenticated → `anon:<session_id>` | Server-side JWT/OAuth verification + `customer_identities(issuer, subject → customer_id)` lookup (`REQ-IDENT-001..009`); architectural AST guard bans caller-asserted `user_id` (`test_no_caller_asserted_identity.py`). |
| **`TriageRouterAgent` → Google APIs & Egress** | **Vertex AI SPIFFE Agent Identity** (`principal://agents.global.org-349636765856.../2170218524805431296`) + certificate-bound mTLS (`*.mtls.googleapis.com`) | Session-scoped verified `customer_id` (`scope={"user_id": customer_id}` on Memory Bank) | Custom enumerated role `ticketRouterTriageRuntime` (Gemini, `router-s1-dedicated`, Model Armor, Memory Bank, KMS `signerVerifier`/`cryptoKeyEncrypter`); **Agent Gateway `tr-egress`** (`AGENT_TO_ANYWHERE`, IAP V2 + IAM v3 UAP `ENFORCE`) restricts outbound traffic to registered `faq-resolver` & `core-gapi` targets. |
| **`TriageRouterAgent` → `FaqResolverAgent` (A2A)** | Target-scoped OIDC Bearer token (`sa-triage` impersonation solely for OIDC minting) routed via **Agent Gateway `tr-egress`** | **KMS-signed ES256 Customer Context Token (CCT)** (`X-Customer-Context-Token`, 5-min TTL, `jti`, `sub=customer_id`, `customer-context-signer`) | `FaqResolverAgent` verifies Google OIDC audience + caller allow-list and forwards the signed CCT untouched (cannot forge or swap `customer_id`). |
| **`FaqResolverAgent` → `order-lookup-mcp` → Cloud SQL RLS** | **GKE Agent Identity SPIFFE** (`ksa-faq-agent` → `spiffe://agents.global.org-349636765856.../ns/faq-resolver/sa/ksa-faq-agent`, custom role `ticketRouterAgentInference`) | **ES256 CCT (`CCT_MODE=require`)** → `SELECT set_config('app.customer_id', $1, true)` | `order-lookup-mcp` enforces `sub ∈ ALLOWED_CALLER_SPIFFE_IDS` before verifying the KMS ES256 signature (`KmsPublicKeyResolver`), rejects `anon:*` (`403`), and queries as `SELECT`-only `orders_ro` under **PostgreSQL Row-Level Security (`order_customer_isolation`)**; cross-customer probes return byte-identical `404 not_found`. |
| **Human Support Rep → `human-review-ui` → `human-queue`** | **Cloud Run Direct IAP** (`iap_enabled = true`) → `sa-review-ui@` (`roles/run.invoker` on `human-queue`) | IAP-verified workforce email assertion → `X-Rep-Id` header (body `operator_id` rejected `400`) | `human-queue` (`sa-queue@`, DB role `queue_rw`) is the **sole principal** with `roles/cloudkms.cryptoKeyDecrypter` on `redaction-map-wrapper` and `SELECT` on `redaction_maps`; every unmasking/claim/reopen writes an immutable `ticket_events` audit record. |
| **Cloud Build CI / Approval-Gated CD** | `ticket-router-build@` (CI) · `sa-cd-deployer@` (CD, manual approval gate) | Human approver (`roles/cloudbuild.builds.approver`) | Scoped deploy roles only (zero `roles/editor`); Terraform runs exclusively on `laya-tune` VM (`scripts/tf_vm.sh`). |

### 5.2 Data Classification, Privacy & PCI Scope Avoidance

| Data Class | Examples | Storage Location | Technical Controls | Retention & TTL |
| :--- | :--- | :--- | :--- | :--- |
| **C1 Public** | 50 curated v2 FAQ articles, PII-scrubbed `AmazonHelp` threads | Agent Search (`global`), GCS (`gs://...-laya`) | Encryption at rest, SHA-256 content-hashed manifests, numeric policy lint | Versioned releases |
| **C2 Internal** | Tokenized tickets (`body_redacted`), `RouterDecision`, eval scorecards, OTel traces | Cloud SQL (`ticket-store`), Cloud Trace/Logging, BigQuery `(Stretch)` | TLS 1.2+ / mTLS, IAM DB auth (`ticket_rw`, `analyst_ro`), `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT` | `2`-year ticket retention (`400 d` audit sink `(Stretch)`) |
| **C3 Confidential** | Reversible PII token maps (`[EMAIL_1]`, `[PHONE_1]`, `[SSN_1]`, `[ADDRESS_1]`), Memory Bank items | Cloud SQL `redaction_maps`; Vertex AI Memory Bank (tokenized-only) | **Cloud KMS envelope encryption** (`redaction-map-wrapper` wraps per-row AES-GCM DEK); `ticket_rw` & `analyst_ro` denied access; `queue_rw` audited unmasking | `30`-day TTL on `redaction_maps`; `180`-day TTL on Memory Bank |
| **C4 Restricted** | Payment card numbers (`CREDIT_CARD_NUMBER`) | **Never stored anywhere** | Detected at ingress by Model Armor + Cloud SDP (`support_router_inspect`) → **immediate `HTTP 400`** deterministic security guidance (`card_data_blocked`) before DB, KMS, Memory Bank, or LLM calls | **0 s** (keeps system out of PCI-DSS scope by construction) |

---

## 6. RAG & Data Engineering

### 6.1 Corpus & Leakage-Safe Cleaning Pipeline

**Source Corpus:** Kaggle *Customer Support on Twitter* (`twcs.csv`, ~2.8M tweets), filtered to English (`en`) `AmazonHelp` retail threads (`81,103` reconstructed threads; multilingual deferred to Phase 2):

| Pipeline Stage | Mechanism | Transformation & Verified Output |
| :--- | :--- | :--- |
| **1. Thread Reconstruction** | Graph walk (`in_response_to_tweet_id`) | Reconstructs `81,103` multi-turn customer–brand threads; extracts first customer turns and resolution indicators. |
| **2. Deduplication** | Exact normalized hash + MinHash (`Jaccard ≥ 0.85`) | Removes copy-paste macro templates and enforces zero SHA-256 contamination across train/DEV/TEST splits. |
| **3. Noise & English Filter** | Regex + Language ID (`en`) | Strips rep signatures (`^AB`), tracking URLs, and *"please DM us"* boilerplate while preserving sentiment emojis. |
| **4. PII Scrubbing** | `[ORDER]`-first regex + Luhn checksum + DLP | Extracts Amazon `3-7-7` order IDs before Luhn-checked card scrubbing (`REQ-ROUTER-S1-002`) so order numbers never false-trigger card masks; downstream artifacts are 100% PII-free. |
| **5. Teacher & Reranker Labeling** | `gemini-3.1-pro-preview` (`3×` vote) + Jetski Pro/Flash shards | `6,446` soft-labeled threads (`labels/v2`) for `router-s1` + `1,323` leakage-safe graded query–doc pairs (`REQ-RAG-RETR-016`) for Laya v2 reranker training. |

### 6.2 Dual-Store Architecture & Versioned Corpus Lifecycle

| Data Store (`support-kb-search`, `global`) | Trust Level | Ingestion, Chunking & Policy Governance | Runtime Retrieval & Citation Role |
| :--- | :--- | :--- | :--- |
| **`faq-articles`** *(`50` curated v2 articles, Release 5)* | **Authoritative (Citable: Yes)** | Regrouped from 56 v1 themes (`rag/corpus_v2/`), augmented with 109 leakage-safe paraphrases, governed by single-sourced `SIMULATED_POLICY` + numeric-fact CI lint.<br>**1 article = 1 chunk** (`200–600` tok); published idempotently via `corpus.publish_faq` (`reconciliationMode=FULL`, retired tombstones excluded). | **Primary grounding source.** Every automated policy answer must carry **≥ 1 verbatim normalized substring citation** (`doc_id@version`) from an `active` article here. |
| **`resolved-threads`** *(`81,103` threads)* | **Secondary Context (Citable: No)** | Structured thread documents (`content_config = NO_CONTENT`), thread-aware turn boundaries (`≤ 512` tok). | **Auxiliary context & gap mining.** Cannot ground a customer-facing policy claim without an accompanying `faq-articles` citation. |

### 6.3 Production Retrieval, Reranking & Measured Ablations

- **Engine & Tier Selection:** Agent Search `support-kb-search` runs in `global` on **`SEARCH_TIER_STANDARD` (`~$1.50/1k` queries, no LLM add-on)** — saving ~81% vs. Enterprise+LLM (`$8/1k`) with **identical `recall@5 = 0.9128`** (`n=218` C2 DEV consensus) because answer synthesis runs in `FaqResolverAgent`. Moving to `us` multi-region was empirically tested and rejected after measuring a `-2.7 pp` `recall@5` drop (`-11.3 pp` on `order_status`).
- **Soft Intent Boost (`intent_soft_noisy_0.2`):** Hard boolean `intent_family` filtering was rejected after router misclassifications / missing metadata collapsed slice recall; production uses `relevanceThreshold=LOWEST` + query cleaning + soft `conditionBoostSpecs` (`boost=0.2` on predicted `intent_family`) + `status="active"` filter, fetching `page_size=10` in ~`190 ms` p50 / `251 ms` p95.
- **Co-Located `router-s1` Laya v2 Choice Reranker:** Managed `semantic-ranker-512` degraded `recall@5` (`0.594`), and Gemini query rewriting added `23 s` p95 latency. Instead, our fine-tuned **Laya v2 choice reranker** (co-located on `router-s1-dedicated`, `~35 ms` p95 on T4 GPU vs. `333 ms` CPU MiniLM cross-encoder) reorders the top 5 candidates, lifting audited held-out **TEST `MRR@10` from `0.784 → 0.838`** and **graded `nDCG@5` from `0.695 → 0.735`** (`recall@5 = 0.907`).
- **100% Verbatim Citation Hard Gate & Static Passage Fallback:** `CitationValidator` strips HTML highlight tags (`<b>`, `&nbsp;`) and verifies exact normalized substring presence in the retrieved `faq-articles` text. If `gemini-3.8-flash` times out under tail load, `passage_selector.py` (`REQ-AGENT-028`) deterministically extracts a cited passage when reranker confidence `≥ 0.50`; otherwise `FaqResolverAgent` escalates via `HumanHandoff` (**0 uncited policy replies ever reach a customer**).

---

## 7. Testing, Evaluation & CI/CD

### 7.1 Tiered Verification Cadence (Continuous CI/CD vs. Scheduled vs. Milestone)

To keep developer feedback fast while maintaining rigorous production gates, tests and evaluations are separated by execution cadence:

| Cadence Tier | Trigger | Scope & Tooling | Blocking Gate |
| :--- | :--- | :--- | :--- |
| **1. Continuous PR / Push CI** (`cicd/cloudbuild.yaml`) | Every push / PR (~2–3 min) | `960+` offline `pytest` unit/property/contract/security tests (`--cov=src/ticket_router --cov-branch --cov-fail-under=80`), `test_spec_traceability.py`, `test_no_hardcoded_models.py`, `ruff`, `20`-case hermetic `evals/smoke_gate.py` (`< 1 s`), and **change-aware** `scripts/ci_changes.py` (runs Terraform `fmt/validate/tflint` or live `n=218` C2 retrieval gate `~$0.45` **only** when matching SHA-256 content fingerprints change). | **100% test pass**, **≥ 80% line+branch coverage**, `recall@5 ≥ 0.90` & `MRR@10 ≥ 0.75` on retrieval changes. |
| **2. Approval-Gated CD** (`cicd/cloudbuild-deploy.yaml`) | Human approval on `main` (~6.5 min) | Verifies green CI for exact commit SHA → builds cached `agents:<sha>` image → deploys **Cloud Run (`5` services) ∥ GKE Autopilot ∥ Agent Engine** (change-aware `ae_fingerprint.py`) in parallel → runs live warm-up + gating smoke (`cloud_run_smoke` 9/9, `e2e_identity_check` 5/5 incl. BOLA/RLS, `model_armor_smoke` 5/5). | Records `gs://.../cd/last_good.json` on pass; **automatic rollback** of Cloud Run & GKE on any smoke failure. |
| **3. Scheduled / Pre-Promotion Eval** (`(Stretch)` cron paused in sandbox) | On-demand / pre-release | Deployed `DEV` end-to-end agent eval (`evals/agent_e2e.py`, `n=534`, `< $5` cost cap), `routing_v2.1.0` paired comparison (`evals/routing/`), `k=3` multi-pass LLM-judge scoring with Krippendorff's $\alpha$, and live trace-attributed latency benchmark (`evals/latency/`, `32` cases × `5` reps). | Zero hard-gate violations; paired bootstrap & McNemar significance check on coordinate-ascent changes. |
| **4. One-Time / Milestone Verification** | Pre-panel / major release | **Single pre-registered held-out `TEST` E2E run** (`n=534`, audited in `evals/results/test_runs.log`), live **Chaos Game Day** (`chaos/`: `gemini_429_exhaustion`, `cloud_sql_restart`, `router_s1_outage` with guaranteed `finally:` restoration), and `Locust` load (`33 msg/s` sustained + `100–333 msg/s` spike `(Stretch)`). | All hard safety gates pass on `TEST`; `0` dropped tickets & `100%` post-chaos service restoration. |

### 7.2 Evaluation Criteria, Hard Safety Gates & Guardrails

**Benchmark Construction (`evals/data/agent_e2e_v1/`, `534` DEV / `534` TEST, 0 text leakage):** Combines `150/150` `router_golden_v1` threads, `218/218` C2 multi-label retrieval queries, `80/80` adversarial tone-gaming variants (`20` seeds × `4` tones per split: calm, fake-urgent, angry, abusive), and `86/86` BOLA/identity isolation probes.

| Pillar / Gate | Method | Enforced Bar | Guardrail & Rationale |
| :--- | :--- | :--- | :--- |
| **Strict Angry/Urgent Escalation Recall & Add-Only Invariant** | Deterministic + `hypothesis` property | **System Recall ≥ 0.98** *(95% Wilson LB reported)*<br>**100% Add-Only Invariant** | Every strict angry/urgent/abusive turn bypasses automated policy generation to a human queue (`REQ-AGENT-003`). |
| **Escalation Precision & Queue-Bloat Guardrail** | Statistical (`routing_v2.1.0` + E2E) | **Escalation Precision ≥ 0.60** *(DEV `v4`: `0.679`)* + **OOS Refusal (`reject_oos`)** | Prevents trivial *"escalate everything"* gaming from passing the `≥ 0.98` recall bar; calm non-retail queries refuse in-place (`queue="none"`). |
| **Priority Gaming Resistance** | Deterministic (`tone_suite`) | **0 Priority-Gaming Violations** | Fake urgency or profanity on low-impact issues cannot gain `P1` / `human-urgent` rank ahead of verified past-due/fraud impact (`REQ-AGENT-005`). |
| **Cross-Customer BOLA / RLS & Card Guard** | Deterministic (`bola_suite`) | **0 BOLA Violations** · **100% C4 Card `HTTP 400`** | Enforces ES256 CCT + Postgres RLS (`app.customer_id`), byte-identical `404` on foreign orders, and zero PII/card leakage. |
| **Router Intent Accuracy & Calibration** | Statistical (`routing_v2.1.0` / `v2`) | **Macro-F1 ≥ 0.85** *(`v4` DEV: `0.8500`)*<br>**15-bin ECE ≤ 0.06** *(`v2`: `0.053`)* | Validates 5-way canonical taxonomy (`100%` valid labels) and temperature-scaled encoder probabilities ($T=1.18$). |
| **RAG Retrieval & Citation Verbatim Precision** | Deterministic | **Recall@5 ≥ 0.90, MRR@10 ≥ 0.75**<br>**100% Verbatim Citation Precision (Hard Gate)** | User-facing answers enforce **100% exact normalized substring match** against retrieved `faq-articles` (`CitationValidator` drops unverifiable quotes or escalates; raw pre-filter rate tracked diagnostically). |
| **Groundedness, Helpfulness & Calming Ack Tone** | Calibrated LLM Judge (`k=3` passes) | **Groundedness ≥ 4.0–4.5 / 5.0**<br>**Helpfulness & Ack ≥ 4.0 / 5.0** *(Anchor $\kappa = 0.924 \ge 0.70$)* | Calibrated against `20` gold-scored anchors (`95.0%` exact match, Cohen's $\kappa = 0.924$) with Krippendorff's $\alpha$ across `k=3` passes (`DEV` $\alpha = 0.7486$: `4.232 / 4.354 / 4.968`; `TEST` $\alpha = 0.6704$, `87.2%` unanimous: **`4.576`** at $\alpha = 0.9357$ / **`4.470`** / **`4.990`**). |
| **Live Per-Path Latency & Unit Cost** | Cloud Trace + Token Counters | **Tier-1 RPC p95 < 100 ms** *(`77 ms`)*<br>**Esc E2E p95 ≤ 3.0 s** *(`1.15–1.20 s`)* · **FAQ E2E p95 ≤ 20.0 s** *(`7.06–7.53 s`)* · **≤ \$0.01/tkt** *(`\$0.00071`)* | Verified across `1,762` consecutive live turns on `2170218524805431296` (`0` errors, `100%` Cloud Logging correlation; `run_004_final`: `160/160` turns, `100.0%` decision match, `0%` errors). |

#### Measured End-to-End Deployed Scorecards (`DEV` Hill-Climb & Single Pre-Registered `TEST`)

| Deployed Run (`2170218524805431296`) | Cases | Strict Escalation Recall (95% Wilson LB) | BOLA | Priority Gaming | Fast-Path Esc p95 | LLM / FAQ p95 | Verbatim Citation Rate (`Hit@5`) | Judge Ground / Help / Ack ($k=3\ \alpha$) | Unit Cost | Hard Gates |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`DEV` Attempt `000` (`tier2_router_v4`)** | `534` | `0.9647` (`82/85`, LB `0.9013`) | `0` | `0` | `1.223 s` | `6.190 s` | `0.8478` (`0.6750`) | `4.263 / 4.207 / 4.989` ($\alpha=0.7251$) | `\$0.00072` | **4/5** *(recall `<0.98`)* |
| **`DEV` Attempt `001` (`tier2_router_v5`, Frozen)** | `534` | **`0.9882`** (`84/85`, LB `0.9363`) | **`0`** | **`0`** | **`1.195 s`** | **`7.532 s`** | **`0.8690`** (`0.6667`) | **`4.232 / 4.354 / 4.968`** ($\alpha=0.7486$) | **`\$0.00072`** | **PASS (`5/5`)** |
| **Pre-Registered `TEST` (`run_test_deployed_20261031_071519`)** | `534` | `0.9368` (`89/95`, LB `0.8690`) | **`0`** | **`0`** | **`1.145 s`** | **`7.058 s`** | **`0.8955`** (`0.7436`) | **`4.576 / 4.470 / 4.990`** ($\alpha=0.6704$) | **`\$0.00071`** | **4/5** *(single blind run)* |

> **Methodological Discipline on Blind `TEST` (`REQ-EVAL-009`):** Per pre-registration in `evals/results/test_runs.log`, `TEST` was executed **exactly once** after freezing `tier2_router_v5` on `DEV` (`5/5` hard gates PASS) and was **not** re-tuned post-hoc. Root-cause inspection of all `6` blind `TEST` strict misses (`89/95`) shows **`0` misses in `tier2_flash` (`tier2_router_v5` caught 100% of strict cases it received)** — all `6/6` short-circuited at `tier1_router_s1` (`p_esc < τ_low = 0.15`, `p_intent ≥ 0.70`): `3` polite-tone physical/medical hazards (`ADV-024-calm` expired infant formula, `ADV-025-calm` overdue glucose strips, `ADV-039-calm` swollen laptop battery) absent from `AmazonHelp` Twitter training data and Tier-0 emergency regexes, and `3` cold rhetorical accusations without profanity (`ADV-025/031/037-angry`). Phase-2 remediation expands Tier-0 physical/medical hazard lexicons and lowers `τ_low` (`0.15 → 0.05`) / retrains `router-s1` v3 on polite-hazard + rhetorical-anger hard negatives. Note also that the ~10% uncited answers in `agent_e2e_v1` come from non-English (`ja`/`de`) tweets retained in `router_golden_v1`/`retrieval_v1` where `FaqResolverAgent` verifies English KB quotes server-side before returning localized prose (`100%` English verbatim match).

### 7.3 Anchored 5 / 3 / 1 LLM-as-a-Judge Rubrics (`k=3` Majority + Krippendorff's $\alpha$)

**Helpfulness & Empathy (customer-facing FAQ & order answers)**
- **5:** Directly answers the question in ≤ 120 words, acknowledges the customer's situation in one sentence without over-apologizing, gives the concrete next step (policy window, order status, or sign-in instruction), and offers human help when relevant.
- **3:** Factually accurate answer but generic tone, verbose policy dump, or omits a clear next step.
- **1:** Factually wrong, evasive, tone-deaf, or asks the customer to repeat details already supplied.

**Groundedness (citation-backed policy & order answers)**
- **5:** Every factual claim (windows, fees, eligibility, order status) is directly supported by a cited `faq-articles` chunk or verified `Order Context`; zero extrapolated policy.
- **3:** Core answer is supported, but a minor qualifier or secondary detail lacks explicit support in the cited passage.
- **1:** Any fabricated policy rule, contradicted timeframe/fee, or quote absent from its cited document.

**Calming Acknowledgement Tone (escalation & session-hold path)**
- **5:** Acknowledges the customer's concern calmly, states clearly that a **human support rep** has the request with `#TICK-...` reference and SLA ETA (`15 min` urgent / `2 h` standard), makes zero outcome/refund promises, uses no exclamation marks or hollow filler, ≤ 60 words.
- **3:** Polite and safe, but generic or missing either the `#TICK-...` reference or SLA timeframe.
- **1:** Defensive, dismissive, promises an unauthorized refund/action, or tells an angry customer to read the FAQ.

---

## 8. Observability, Resilience & Audit

| Pillar | Tooling & Implementation | Live Signals & Actionable Thresholds |
| :--- | :--- | :--- |
| **End-to-End Distributed Tracing** | **OpenTelemetry → Cloud Trace** *(mTLS GAPIC exporter + W3C `traceparent`)* | Correlates `36+` PII-free spans per turn across Agent Engine (`triage_router_turn`, `resolve_customer`, `model_armor`, `router_s1`), Cloud Run (`ticket-db-mcp`, `order-lookup-mcp`), and GKE (`faq_resolver.search`, `rerank`, `generate_content`), with `NO_CONTENT` GenAI privacy enforcement. |
| **PII-Free Structured Decision Logs** | **Cloud Logging** *(JSON payloads)* | Emits `session_id`, `ticket_id`, `decision`, `deciding_tier`, `assigned_queue`, `degraded`, `traceparent`, and latency breakdowns; **never** logs raw customer prompts or `C3`/`C4` values. |
| **Chaos & Failure Verification** | **Live Chaos Suite (`chaos/`) & Failover Runbook** | Verified against live infrastructure across **Gemini `429` exhaustion** (degrades cleanly to `human-standard` calming ack), **Cloud SQL restart** (`ticket-db-mcp` fast `503` in `10 s` + agent fail-open persistence), **`router-s1` outage** (`Tier 2` Flash fallback with `degraded=true`), and warm `triage-fallback` switchover (`scripts/failover_triage.sh`). |
| **Drift, SLOs & Compliance Audit** | **Cloud Monitoring + `ticket_events` + Cloud Audit Logs** | Tracks tier-hit shares, breaker state transitions, DLQ depth (`alert ≥ 1`), retrieval `recall@5` non-regression, IAP Data Access logs on `tr-egress`, and immutable `ticket_events` rows (`created`, `queued_for_review`, `claimed`, `pii_reidentified`, `returned_to_automation`, `resolved`). *(BigQuery 400-day sink & automated shadow-eval cron marked `(Stretch)`.)* |

---

## 9. Agentic Engineering & Spec-Driven Development (SDD)

To build, verify, and evolve this system within an accelerated delivery window while maintaining strict engineering rigor, development follows a **Multi-Model Conductor–Worker Orchestration** model paired with **Spec-Driven Development (SDD)** enforced in CI.

### 9.1 Multi-Model Orchestration ("Elephant and Goldfish" Pattern)

We separate long-horizon architectural state from bounded execution by routing development tasks across model tiers based on cognitive complexity:

```mermaid
flowchart LR
  H["Human Engineer (@susmith)"] --> C["Conductor: Claude Opus (Architecture, Plans, Diff Review, Git Commits)"]
  C -- "Bounded Implementation & Test Tasks (ASSIGNED_FILES_EXCLUSIVE)" --> F["Workers: Gemini 3.8 Flash (High Thinking)"]
  C -- "Complex Reasoning, Multi-Hop Audits & Disagreement Adjudication" --> A["Specialist Subagents: Gemini Pro (Argon)"]
  F --> V["Verification Gate: pytest + test_spec_traceability.py + ruff"]
  A --> V
  V --> C
```

| Role | Model Tier | Responsibilities & Guardrails |
| :--- | :--- | :--- |
| **Conductor (Orchestrator)** | **Claude Opus** | Holds full-repo architectural context; authors staged execution plans (`docs/plans/PLAN.md`, `PLAN2.md`–`PLAN8.md`); assigns exclusive file boundaries (`ASSIGNED_FILES_EXCLUSIVE`) so parallel workers never collide; audits worker diffs against `specs/`; re-runs `pytest` and recomputes metrics from raw files before committing (**workers never commit**). |
| **Fast Execution Workers** | **Gemini 3.8 Flash** *(High Thinking)* | Executes bounded implementation slices, unit/property test suites, Terraform modules, and parallel offline data/evaluation shards (e.g., FAQ synthesis, 600-query blind retrieval labeling with Cohen's $\kappa = 0.84$, disambiguation mining). |
| **Complex Reasoning Subagents** | **Gemini Pro (Argon)** | Invoked for high-complexity reasoning tasks: architectural trade-off verification, adjudicating inter-annotator disagreements (`labels_c2.jsonl`), and grading hard-negative reranker training sets (`1,323` graded queries across 8 parallel shards). |
| **Offline Cost & Quota Isolation** | **Jetski Agent Runtime vs. Argolis Vertex** | All offline LLM workloads (FAQ synthesis, theme regrouping, eval dataset labeling, and LLM judging) run on Jetski subagents (`REQ-BILLING-001`), reserving the customer's Argolis GCP quota strictly for live `Agent Search` queries and runtime serving. |

### 9.2 Spec-Driven Development (SDD) Framework (`specs/`)

Specifications live directly in the repository under [`specs/`](../specs/README.md) as the single source of truth for both human engineers and coding agents (`AGENTS.md`):

1. **Four-Step SDD Workflow (`Read Spec → Declare REQ IDs → Implement & Test → Conductor Review`):**
   - Every change starts by reading and updating the normative domain specification in `specs/` (`platform.md`, `rag-corpus.md`, `rag-retrieval.md`, `infra-agent-search.md`, `router-s1.md`, `billing-and-models.md`, `model-serving.md`, `ci-cd.md`).
   - Every requirement has a unique ID (`REQ-<AREA>-<NNN>`) with a normative `Statement`, `Rationale` (linked to TDD sections), executable `Acceptance` check, `Status` (`open | partial | done | deferred`), and `Owner`.
   - The spec update, `# REQ-...` annotated `pytest` tests, and implementation code **must land in the same commit**.
2. **Automated Bidirectional Traceability (`tests/test_spec_traceability.py`):**
   - Executed on every local run and as a blocking step in Cloud Build (`cicd/cloudbuild.yaml`).
   - Asserts that all `REQ-*` IDs (86 across 15+ specs as of 2026-10-28) are unique, every `done` requirement is verified by at least one `# REQ-...` test in `tests/**` (or an explicit `manual:` operational runbook), and zero tests reference non-existent requirement IDs.
3. **Continuous Harness Feedback Loop (`AGENTS.md` & `docs/LESSONS.md`):**
   - Repeated agent mistakes are codified into automated lint/test guards—such as `tests/test_no_hardcoded_models.py` (enforcing that all model IDs come from `src/ticket_router/config/models.yaml`), `tests/infra/test_cicd_static.py` (preventing unescaped bash parameter expansions in Cloud Build YAML), and `scripts/tf_vm.sh` (enforcing VM-only Terraform execution against remote GCS state).

4. **How changes ship (verification beyond tests):**
   - **CI** (`cicd/cloudbuild.yaml`) runs on every push to `main`: unit/resilience tests, spec traceability, ruff, Terraform static checks, and the golden retrieval eval gate.
   - **CD** (`cicd/cloudbuild-deploy.yaml`, `specs/cd-pipeline.md`) is **human-approved**: after approval it requires green CI for the same commit, builds one `agents:<sha>` image, deploys Cloud Run, GKE, and Agent Engine **in parallel**, then gates on live smoke (Cloud Run MCP checks, identity/BOLA e2e, Model Armor), recording `last_good.json` or rolling back. Terraform (infra only) runs on the Argolis VM, never from CI/CD.
   - **Runtime guardrails** verified live after each deploy: Model Armor screens inbound prompts (TriageRouter) and generated answers (FaqResolver; GKE egress via Cloud NAT because Private Google Access does not cover regional `*.rep.googleapis.com` endpoints).
