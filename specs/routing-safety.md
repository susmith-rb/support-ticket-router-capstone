# Routing Safety & Lifecycle Assurance Specification

This specification governs routing safety, deterministic security intent refusals, non-escalating degradation fallbacks, multi-turn lifecycle state isolation, and multi-intent deconstruction for the Support Ticket Router & Assistant, linking to design requirements in `docs/TDD.md` §3, §4.2, §7.1, §12.7, and §14.

---

## 1. Architectural Overview

Production telemetry revealed several routing failure modes that compromise user trust and system resources:
1. **Unsafe OTel Attributes**: Potential PII leakage or uncalibrated decision tokens on traces.
2. **False Escalation of Security Probes**: Security probing messages (e.g. cross-customer data requests, authorization bypass attempts) slipping into generic classification and creating human tickets.
3. **Over-Escalation on Tier 3 Outages**: Tier 3 timeout or failure falling back to unconditional human escalation even when Tier 2 produced an actionable self-service prediction without escalation signals.
4. **Ticket ID Conflation & Premature Closure**: Deriving ticket IDs from session ID rather than canonical ticket UUIDs, causing 404s in confirmation recording and misrouting conditional statements ("if it arrives tomorrow...").
5. **Multi-Intent Ignorance**: Users asking multiple distinct sub-questions in a single prompt (e.g. delayed package + return policy) receiving either single-intent answers or premature human escalation.

---

## 2. Requirements

### REQ-SAFE-001: Safe Tier-2 and Final Routing Decision OpenTelemetry Attributes
- **Statement**: All spans recording Tier-2 predictions and final turn-level routing decisions MUST enforce strict allowlists and sanitizers for OpenTelemetry attributes. Span attributes `decision`, `tier`, `queue`, `predicted_intent`, `sentiment`, `urgency`, and `handoff_status` MUST be validated against safe frozenset enums. Floating-point probabilities MUST be clamped to [0.0, 1.0] and rounded to 4 decimals. Reason codes MUST be canonical prefixes without raw exception messages or user text. A serialized `router.final_decision` JSON payload MUST contain only safe allowlisted fields.
- **Rationale**: TDD §14. Prevents PII leakage, unbounded cardinality, and sensitive exception disclosures in distributed tracing while preserving observability for production analysis.
- **Acceptance**: `tests/agents/test_routing_safety.py::test_safe_otel_attributes_compliance`
- **Status**: done
- **Owner**: agent-service

### REQ-SAFE-002: Deterministic Tier-0 Security Intents and Authorization Bypass Refusal
- **Statement**: Messages attempting cross-customer data access (`cross_customer_data_request`) or authorization bypass (`authorization_bypass_attempt`) MUST be detected deterministically in Tier 0 rules AFTER Model Armor payment card and prompt injection checks, and BEFORE Tier 1/2/3, A2A handoffs, Memory Bank, or MCP tool calls. When triggered, the router MUST return `decision="auto_rag"`, `tier="tier0_rules"`, `queue="none"`, `escalate=False`, emit a structured `security_refusal` WARNING log without leaking message content, return a neutral canned refusal, and perform zero ticket persistence or MCP tool invocations.
- **Rationale**: TDD §3 and §7.1. Prevents authorization bypass attempts and cross-customer snooping from consuming human queue bandwidth or creating unnecessary support tickets.
- **Acceptance**: `tests/agents/test_routing_safety.py::test_deterministic_security_intents_refusal`
- **Status**: done
- **Owner**: agent-service

### REQ-SAFE-003: Confidence Calibration and Non-Escalating Tier-3 Degradation Fallback
- **Statement**: Tier 2 keeps prompt `tier2_router_v5` (no prompt change; router-s1 thresholds unchanged). On a self-service intent (`order_status`, `shipping_policy`, `returns_refunds`, `account_payment`) a bare Tier 2 `escalate` flag is honoured ONLY with an escalation signal (Tier 0 escalate, explicit human request, emergency/abuse/fraud/legal keywords, angry sentiment, high or immediate urgency) - `_has_escalation_signal` in policy.py. In-domain `other` (no self-service path, e.g. an app crash) keeps Tier 2's decision (REQ-AGENT-022). Tier 0, Tier 1, angry and immediate stay add-only (REQ-AGENT-003). When Tier 3 fails or times out but Tier 2 produced a usable self-service prediction (intent_prob >= 0.55) without escalation signals, the router MUST NOT escalate; it routes `auto_rag` with `degraded=True`, `reason="tier3_unavailable_used_tier2"`. Total degradation (Tier 1 and Tier 2 down) still goes to a human (REQ-AGENT-011). Measured on routing_v1 regression (n=150, paired, same cached Tier 2 outputs, 2026-11-01): 1 decision changed (a gold auto_rag case no longer escalated), 0 gold-escalate cases lost. A broader gate that also covered intent `other` lost 5 gold-escalate cases and was rejected.
- **Rationale**: TDD §3 and §4.2. Prevents catastrophic cascading queue overload during LLM degradation while guaranteeing human handoff when genuine escalation triggers are present.
- **Acceptance**: `tests/agents/test_routing_safety.py::test_non_escalating_tier3_degradation_fallback`
- **Status**: done
- **Owner**: agent-service

### REQ-SAFE-004: Turn-Level Intent Isolation, Per-Ticket References, and Deterministic Resolution Workflow
- **Statement**: In multi-turn sessions, customer-facing ticket references MUST be derived from the ticket's canonical UUID (e.g. `TICK-<first 8 hex of canonical UUID>`) and persisted in `issue_reference`, ensuring per-ticket isolation. The router MUST re-evaluate intent on every turn:
  1. **Conditional Resolution**: Statements with conditional phrasing (e.g. "if it arrives tomorrow then we are good") MUST NOT close the ticket or escalate; the router MUST acknowledge calmly with `decision="auto_rag"`, `tier="ticket_lifecycle"`, leaving existing tickets open.
  2. **Unconditional Resolution on Human Tickets**: Reports that an item arrived or issue resolved on a human-owned ticket MUST append a customer follow-up note to the ticket without closing it, allowing human representatives to confirm resolution.
  3. **Resolution on Bot Tickets**: Auto-resolved bot tickets record `record_resolution_confirmation_requested` using the ticket's canonical UUID (avoiding 404s). Verified customers confirming resolution transition the ticket to `resolved_by_customer`; unverified customers receive a sign-in prompt (HTTP 403).
- **Rationale**: TDD §4.2 and §7.1. Eliminates ticket reference collisions across turns, prevents premature ticket closure on conditional statements, and protects human-owned tickets from automated closure.
- **Acceptance**: `tests/agents/test_routing_safety.py::test_turn_level_intent_isolation_and_ticket_references`
- **Status**: done
- **Owner**: agent-service

### REQ-SAFE-005: Multi-Intent Sentence Deconstruction and Dual-Pipeline Grounded Resolution
- **Statement**: The triage router MUST detect and deconstruct compound messages with multiple distinct intents (e.g. package tracking + return policy) into structured sub-questions via `detect_sub_questions` and forward them in `A2AHandoffPayload.sub_questions`. `FaqResolverAgent` MUST process both sub-questions by querying order status (for verified customers) and searching knowledge base articles in parallel or sequence, synthesizing a grounded response with mandatory verbatim citations covering all customer inquiries without premature escalation.
- **Rationale**: TDD §3 and §6. Resolves multi-intent customer inquiries in a single automated interaction without frustrating customers or overburdening support staff.
- **Acceptance**: `tests/agents/test_routing_safety.py::test_multi_intent_deconstruction_and_dual_resolution`
- **Status**: done
- **Owner**: agent-service
