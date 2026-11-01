# Ticket Lifecycle & Multi-Turn Continuation Specification

This specification governs the multi-turn ticket lifecycle, deterministic continuation policy, and customer resolution confirmation flow for the Support Ticket Router & Assistant, linking to design requirements in `docs/TDD.md` §3, §4.2, §7.1, and §12.7.

---

## 1. Architectural Overview

In multi-turn conversations, maintaining clean separation between distinct customer issues, preventing cross-turn security escalation holding, and supporting deterministic ticket closure are critical for conversation state integrity and agent safety.

### Decision & Continuation Matrix

| Scenario | Trigger / Intent | Prior Ticket Status | Outcome / Continuation | Target Tier |
|---|---|---|---|---|
| Prompt Injection | Model Armor PI/JB trigger | Any | `security_blocked` (refusal, zero ticket, no session hold) | `model_armor` |
| New Actionable Request | Order lookup / distinct FAQ | Open human ticket | `NEW_ISSUE` (routes to automation, keeps human ticket open) | `tier1_router_s1` / `a2a` |
| Explicit Ticket Ref | "ticket #TICK-1234" | Open human ticket | `CONTINUE` (appends follow-up to existing ticket + calming ack) | `human_holding` |
| Conversational Elaboration | "when will agent reply?" / same intent | Open human ticket | `CONTINUE` (appends follow-up to existing ticket + calming ack) | `human_holding` |
| Security Ticket Query | "what is status of #TICK-SEC" | Security ticket | `STATUS_INQUIRY` (safe status-only response, zero leak) | `ticket_status` |
| Affirmative Resolution | "yes that resolved it" | Bot auto-resolved | `RESOLVE` (transitions to `resolved_by_customer`) | `ticket_lifecycle` |
| Unverified Resolution | "yes that resolved it" | Bot auto-resolved (anon) | `DENIED` (prompts customer to sign in to close ticket) | `ticket_lifecycle` |

---

## 2. Requirements

### REQ-LIFE-001: Continuation Decision Policy
- **Statement**: `TriageRouterAgent` MUST evaluate turn-level security and deterministic issue continuation (`CONTINUE(ticket)` vs `NEW_ISSUE`) on every conversation turn in the following strict order:
  1. **Tier 0 Card Block**: Messages containing payment card numbers are blocked immediately with HTTP 400 (`REQ-MA-003`, `REQ-AGENT-032`).
  2. **Turn-Level Model Armor PI/JB Check**: Model Armor checks for prompt injection and jailbreak on every turn before holding or intent routing. If triggered, return safe refusal immediately with HTTP 200 `security_blocked`, create zero tickets, and NEVER hold the session (`REQ-MA-002`, `REQ-AGENT-033`).
  3. **Turn-Level Intent & Identity Resolution**: Policy evaluation (`router-s1`) and customer identity resolution run concurrently on every turn (`REQ-AGENT-027`). Turn-level intent classification ALWAYS runs before holding continuation check so intent and threats are accurately recorded on every turn.
  4. **Deterministic Continuation Evaluation**:
     - Continue only if the message contains an explicit ticket reference (e.g. `#TICK-...` matching an active ticket) OR conversational follow-up / related intent (same intent family, `followup`, or `other`).
     - Any different actionable request (e.g. `order_status`, `returns_refunds`, `shipping_policy`, or distinct FAQ) MUST be treated as `NEW_ISSUE` and routed to automation, leaving existing tickets untouched.
     - Security/fraud tickets MUST NEVER be continued without explicit ticket ID reference; when explicitly referenced, return a safe status-only response without leaking findings or prompt injection text.
     - Only human-owned ordinary tickets (`category not in ('security', 'fraud')` and `current_owner != 'bot'`) append follow-up audit events and return a calming acknowledgement (`human_holding`); all other requests route normally.
- **Rationale**: TDD §3, §4.2, and §7.1. Prevents prompt injection escalations from hijacking subsequent benign turns, and prevents customer inquiries from being swallowed by open human tickets.
- **Acceptance**: `tests/agents/test_ticket_lifecycle.py::test_reproduce_production_defect_security_escalation_captures_benign_order_lookup` and `tests/evals/test_multi_turn_lifecycle.py::test_multi_turn_lifecycle_dataset`
- **Status**: done
- **Owner**: agent-service

### REQ-LIFE-002: Customer Resolution Confirmation Flow & Transition Validation
- **Statement**: Auto-resolved bot-owned support tickets MUST append an explicit resolution confirmation question: `\n\nDid that resolve your issue? Reply yes to close ticket #TICK-..`. When asked, an audit event `resolution_confirmation_requested` is recorded on the ticket, establishing `pending_confirmation` in `ConversationState` (`REQ-TURN-002`, `REQ-TURN-005`). Customer affirmative confirmation (`turn_act == "confirmation_yes"` from Gemini Flash turn interpreter) invokes tool `update_ticket_status` on `ticket-db-mcp` with the following strict backend validation and authorization rules:
  1. **Pending Confirmation Marker**: Confirmation requires that `pending_confirmation` in `ConversationState` is present and matches the candidate ticket. If no confirmation was previously requested for that ticket (`pending_confirmation` is None), or if the ticket belongs to a different session or customer, the affirmative reply is not treated as a ticket closure and routes as a normal turn.
  2. **Verified Identity Header Validation**: `ticket-db-mcp` MUST NOT trust caller-supplied `customer_id` in request bodies over verified identity. The caller must supply `X-Customer-ID` (or `X-Verified-Customer-Id`) and `X-Customer-Verified: true`. A request body `customer_id` that mismatches the header is rejected with HTTP 400 (BOLA guard). Unverified customers or anonymous customers (`anon:`) are rejected with HTTP 403.
  3. **Actor Impersonation Guard**: Caller cannot pass representative or queue actors (`actor.startswith("rep:")` or `actor.startswith("queue:")`) on customer status updates; such attempts are rejected with HTTP 403 / PermissionError.
  4. **Backend Store Scoping & Non-Null Invariant**: In both PostgreSQL and Fake stores, `update_ticket_status` strictly requires non-null `customer_id` for customer status updates (`status in ("resolved_by_customer", "reopened")`). Callers cannot omit `customer_id` to bypass security/fraud, human ownership, or transition checks.
  5. **Allowed Transition Matrix**: Transitions must conform to `CUSTOMER_ALLOWED_TRANSITIONS` (`open` -> `resolved_by_customer`, `answered` -> `resolved_by_customer`, `resolved_by_customer` -> `reopened`).
  6. **Ownership Restriction**: Customer confirmation is allowed ONLY on bot-owned support tickets (`category not in ('security', 'fraud')` and `current_owner == 'bot'`). Human queue/agent tickets (`current_owner != 'bot'`) cannot be closed by customer confirmation and append follow-up instead.
  7. **Optimistic Concurrency**: Update requires matching `from_status` against the current database status.
  8. **Idempotency**: Repeated identical confirmation replies return success without error or duplicate state changes (`idempotent: True`).
  9. **Immutable Audit Event**: The status transition records a `status_changed` event in `ticket_events` capturing `from_status`, `to_status`, `actor`, and `reason` without persisting raw message text.
- **Rationale**: TDD §3 and §7.1. Empowers customers to confirm resolution while preventing unauthorized status alterations on human or security tickets, preventing BOLA, and avoiding cross-session ticket closure.
- **Acceptance**: `tests/agents/test_turn_interpretation.py::test_resolution_confirmation_protocol`
- **Status**: done
- **Owner**: agent-service

### REQ-LIFE-003: Multi-Turn Lifecycle Regression and Evaluation Harness
- **Statement**: The codebase MUST maintain a multi-turn regression suite in `tests/agents/test_ticket_lifecycle.py` covering all production lifecycle scenarios and an evaluation benchmark in `evals/data/multi_turn_lifecycle_v1.jsonl` executed by `tests/evals/test_multi_turn_lifecycle.py`. The eval harness asserts decision, tier, intent, and continuation invariants (`CONTINUE`, `NEW_ISSUE`, `STATUS_INQUIRY`, `RESOLVE`, `DENIED`) across all turns in each dialogue.
- **Rationale**: TDD §12.7 and §14. Prevents regressions in multi-turn holding, routing isolation, and lifecycle state management.
- **Acceptance**: `tests/evals/test_multi_turn_lifecycle.py::test_multi_turn_lifecycle_dataset`
- **Status**: done
- **Owner**: agent-evals
