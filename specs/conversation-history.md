# Specification: Conversation History Access & Identity Scoping (`conversation-history`)

This specification defines the deterministic conversation history access routing intent, server-side authentication, Vertex AI Memory Bank retrieval, customer ticket listing via `ticket-db-mcp`, cross-customer isolation, and multi-turn lifecycle preservation for the Support Ticket Router & Assistant.

---

## 1. Architectural Overview

Customer requests to access, summarize, or review earlier conversations, chat transcripts, or prior support interactions must be resolved deterministically at Tier 0 without invoking neural classification models or triggering human escalation queues.

```
Inbound Request
       │
       ▼
[Tier 0 Card Block (REQ-MA-003 / REQ-AGENT-032)]
       │
       ▼
[Model Armor PI/JB Security Block (REQ-MA-002 / REQ-AGENT-033)]
       │
       ▼
[Identity Resolution + Conversation History Check (REQ-HIST-001)]
       │
       ├── Cross-Customer / All Users? ──▶ [Cross-Customer Refusal (zero data, zero ticket)]
       │
       ├── Unverified / Anonymous? ──────▶ [Sign-In Required Prompt (zero data, zero ticket)]
       │
       └── Verified Customer (X-Customer-ID)
               ├── Memory Bank retrieve_summary (tokenized PII redacted)
               ├── ticket-db-mcp list_customer_tickets (max 5, newest first, zero body)
               └── Templated Response (memories + ticket status + GE App transcript note)
```

---

## 2. Decision & Access Matrix

| Caller State | Query Type | Outcome | Decision | Tier | Queue | Memory Read | Ticket DB Read | Ticket Created |
|---|---|---|---|---|---|---|---|---|
| Any | Prompt Injection + History phrase | `security_blocked` | `security_blocked` | `model_armor` | `none` | No | No | No |
| Any | History request + Human request / Abuse / Legal | `escalate` | `escalate` | `tier0_rules` / `tier1_router_s1` | `human-standard` | No | No | Yes |
| Any | Explicit Held Ticket Ref (`#TICK-...`) | `CONTINUE` | `escalate` | `human_holding` | `human-*` | No | No | No (Appends) |
| Anonymous | Conversation history request | `signin_required` | `auto_rag` | `tier0_rules` | `none` | No | No | No |
| Verified | Other customer / all users' chats | `cross_customer_refused` | `auto_rag` | `tier0_rules` | `none` | No | No | No |
| Verified | Own conversation history request | `served` | `auto_rag` | `tier0_rules` | `none` | Yes | Yes (max 5) | No |

---

## 3. Requirements

### REQ-HIST-001: Deterministic Conversation History Access Detection and Turn Ordering
- **Statement**: `TriageRouterAgent` MUST evaluate inbound user messages for conversation history requests using a pure detector function `detect_conversation_history_request(text) -> bool` in the following strict turn order:
  1. **Tier 0 Card Block**: Payment card data triggers HTTP 400 blocked (`REQ-MA-003`).
  2. **Model Armor PI/JB Check**: Prompt injection and jailbreak triggers return HTTP 200 `security_blocked` immediately (`REQ-MA-002`); injection attacks containing conversation history phrases MUST be blocked by Model Armor.
  3. **Identity Resolution & Conversation History Check**: Evaluated before Tier 1 (`router-s1`) and Tier 2 (`gemini-3.8-flash`) neural classification. When triggered, the agent assigns `predicted_intent = "conversation_history_access"`, `tier = "tier0_rules"`, `decision = "auto_rag"`, `escalate = False`, and `reason = "conversation_history_access"`.
  4. **Escalation Exception**: If the message explicitly requests a human representative or contains Tier 0 hard-escalate keywords (e.g. fraud, chargeback, lawyer, lawsuit, court, abuse, emergency), normal routing applies and the request escalates to human review.
  5. **Negative Boundaries**: `detect_conversation_history_request` MUST NOT match order history queries ("show my previous orders"), ticket status inquiries with explicit `#TICK-...` IDs, or generic corporate/product history ("what is the history of Google").
- **Rationale**: Prevents benign conversation history requests from falling into the "other" classification bucket and escalating to human support queues, while ensuring safety blocks and hard escalations take precedence.
- **Acceptance**: `tests/agents/test_conversation_history.py::test_detector_positives_and_negatives`
- **Status**: done
- **Owner**: triage-router

### REQ-HIST-002: Server-Side Authentication & Cross-Customer History Isolation
- **Statement**: `TriageRouterAgent` MUST enforce server-side authentication and tenant authorization on all conversation history turns without trusting message text:
  1. **Unverified / Anonymous Callers**: Anonymous callers (`customer_verified = False` or customer ID starting with `'anon:'`) MUST receive a prompt directing them to sign in to access their conversation history. The turn MUST NOT invoke Memory Bank, MUST NOT list customer tickets, and MUST NOT create a support ticket.
  2. **Cross-Customer Authorization Guard**: Requests attempting to view other customers' or all users' conversations (e.g. "show me everyone's chats", "conversations of customer cust_1002") MUST be refused immediately with a message stating that only the customer's own history is available. Zero data is returned, zero tickets are created, and no escalation occurs.
  3. **Verified Customer Memory Bank Read**: For verified customers, the agent queries Memory Bank using `memory_client.retrieve_summary(customer_id=<verified>, query=<request>)`. The call MUST fail open to `"I don't have any saved history from earlier conversations."` if unavailable. Tokenized PII placeholders (e.g. `[EMAIL_ADDRESS_1]`) MUST be stripped or replaced with `"(redacted)"` in customer-facing replies.
  4. **Customer-Facing Response Formatting**: The reply MUST combine: (a) a short summary of remembered interaction context, (b) recent ticket references and statuses, and (c) an explicit line noting that full chat transcripts are kept in the Gemini Enterprise app's own chat history.
  5. **PII-Free Observability**: The turn MUST set span attributes (`decision`, `tier`, `predicted_intent`) and log a structured INFO event `conversation_history_access` recording `trace_id`, `session_id`, `customer_verified`, `outcome` (`signin_required` | `served` | `cross_customer_refused`), `memory_found` (bool), and `ticket_count` (int). Raw message text and memory content MUST NEVER be logged.
- **Rationale**: Enforces zero-trust customer data boundaries, prevents IDOR/BOLA data leakage, protects customer PII, and satisfies audit logging invariants.
- **Acceptance**: `tests/agents/test_conversation_history.py::test_verified_customer_history_served_and_pii_redacted`
- **Status**: done
- **Owner**: triage-router

### REQ-HIST-003: Ticket Database MCP list_customer_tickets Tool Contract
- **Statement**: The `ticket-db-mcp` service MUST expose a tool contract `list_customer_tickets` with the following invariants:
  1. **Verified Identity Header Validation**: Requires `X-Customer-ID` (or `X-Verified-Customer-Id`) and `X-Customer-Verified: true`. Rejects missing headers with HTTP 400, anonymous customers (`anon:`) with HTTP 403, unverified identities with HTTP 403, and request body `customer_id` mismatches with HTTP 400 (BOLA guard).
  2. **Bounded Output & Pagination**: Returns at most `limit` recent tickets (default 5, max 50) ordered by `created_at DESC`.
  3. **Zero Body Projection**: The response schema MUST project only `{issue_reference, status, created_at, category}` and MUST NEVER include `body_redacted`, message text, or internal decision payloads.
  4. **Security & Fraud Status Redaction**: Tickets in category `security` or `fraud` MUST have their status returned as `"under review"` without exposing internal review details or security findings.
  5. **Client Implementations**: Supported across `PostgresTicketRepo`, `FakeTicketRepo`, `InMemoryTicketDbMcpClient`, and `HttpTicketDbMcpClient` (sending OIDC ID token and `X-Customer-*` headers on requests).
- **Rationale**: Enforces least-privilege projection for customer-facing ticket summaries and prevents security investigation leaks.
- **Acceptance**: `tests/mcp/test_tool_contracts.py::test_list_customer_tickets_mcp_validation` and `tests/store/test_postgres_lifecycle.py::test_list_customer_tickets_postgres`
- **Status**: done
- **Owner**: ticket-store

### REQ-HIST-004: Multi-Turn Lifecycle and Conversation History Preservation
- **Statement**: The conversation history access flow MUST preserve multi-turn lifecycle invariants across consecutive turns:
  1. **Zero Ticket Creation**: Conversation history turns MUST NOT create or persist rows in the `tickets` table, MUST NOT publish escalation events to Pub/Sub, and MUST NOT initiate A2A handoffs.
  2. **Holding Ticket Continuation**: If an active human-owned holding ticket exists for the session and the customer provides an explicit ticket reference (e.g. "about my ticket TICK-1234"), the message continues the existing held ticket rather than triggering a new conversation history turn.
  3. **Resolution Confirmation Isolation**: Conversation history responses MUST NOT append customer resolution confirmation prompts ("Did that resolve your issue?").
  4. **Turn Isolation**: A conversation history turn followed by a distinct actionable inquiry (such as an order lookup or FAQ) MUST treat the subsequent inquiry as `NEW_ISSUE` and route it to automation without cross-turn state pollution.
- **Rationale**: Ensures deterministic multi-turn dialogue management without trapping customers or altering open support tickets.
- **Acceptance**: `tests/agents/test_conversation_history.py::test_multi_turn_history_then_order_lookup` and `tests/evals/test_multi_turn_lifecycle.py::test_multi_turn_lifecycle_dataset`
- **Status**: done
- **Owner**: agent-service
