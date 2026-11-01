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
- **Statement**: Messages attempting cross-customer data access (`cross_customer_data_request`) or authorization bypass (`authorization_bypass_attempt`) (superseded in part by `REQ-TURN-001` and `REQ-TURN-004` in `specs/turn-interpretation.md`) MUST be detected via Gemini Flash Turn Interpreter security flags rather than regex phrase lists, evaluated AFTER Model Armor payment card and prompt injection checks, and BEFORE A2A handoffs, Memory Bank, or MCP tool calls. When triggered, the router MUST return `decision="auto_rag"`, `tier="turn_interpreter"`, `queue="none"`, `escalate=False`, emit a structured `security_refusal` WARNING log without leaking message content, return a neutral canned refusal, and perform zero ticket persistence or MCP tool invocations.
- **Rationale**: TDD §3 and §7.1. Prevents authorization bypass attempts and cross-customer snooping from consuming human queue bandwidth or creating unnecessary support tickets.
- **Acceptance**: `tests/agents/test_turn_interpretation.py::test_deterministic_turn_act_enforcement`
- **Status**: done
- **Owner**: agent-service

### REQ-SAFE-003: Confidence Calibration and Non-Escalating Degradation Fallback
- **Statement**: Triage router evaluates Laya System-1 fast path and Gemini Flash Turn Interpreter (`router_tier2`, `prompts/turn_interpreter_v1.yaml`). Tier 3 Pro is retired as redundant (`REQ-TURN-003`). On a self-service intent (`order_status`, `shipping_policy`, `returns_refunds`, `account_payment`), Laya decides alone when $p_{\text{intent}} \ge 0.70$ and ($p_{\text{esc}} < 0.15$ or ($p_{\text{esc}} \ge 0.50$ with angry/immediate)) in the absence of active tickets or pending confirmation. When the Flash Turn Interpreter fails or times out but Laya produced a usable self-service prediction ($p_{\text{intent}} \ge 0.55$) without escalation signals, the router MUST NOT escalate; it routes `auto_rag` with `degraded=True`, `reason="turn_interpreter_unavailable_used_laya"`. Total degradation (Laya and Flash down) still goes to human queue (`REQ-AGENT-011`).
- **Rationale**: TDD §3 and §4.2. Prevents catastrophic cascading queue overload during LLM degradation while guaranteeing human handoff when genuine escalation triggers are present.
- **Acceptance**: `tests/agents/test_turn_interpretation.py::test_tiered_routing_order_laya_fast_path`
- **Status**: done
- **Owner**: agent-service

### REQ-SAFE-004: Turn-Level Intent Isolation, Per-Ticket References, and Deterministic Resolution Workflow
- **Statement**: Customer-facing ticket references MUST be derived from the ticket's canonical UUID (e.g. `TICK-<first 8 hex of canonical UUID>`) and persisted in `issue_reference`, ensuring per-ticket isolation. The router evaluates turn dialog acts via the Gemini Flash turn interpreter (`REQ-TURN-001`) in the context of explicit `ConversationState` (`REQ-TURN-002`) rather than regex phrase lists:
  1. **Conditional Resolution**: Statements with conditional phrasing (`turn_act == "conditional_resolution"`, e.g. "if it arrives tomorrow then we are good") MUST NOT close the ticket or escalate; the router MUST acknowledge calmly with `decision="auto_rag"`, leaving existing tickets open.
  2. **Unconditional Resolution on Human Tickets**: Reports that an item arrived or issue resolved on a human-owned ticket (`turn_act == "resolution_report"` or `"confirmation_yes"`) MUST append a customer follow-up note to the ticket without closing it, allowing human representatives to confirm resolution.
  3. **Resolution on Bot Tickets**: Auto-resolved bot tickets record `record_resolution_confirmation_requested` using the ticket's canonical UUID (avoiding 404s). Verified customers confirming resolution (`turn_act == "confirmation_yes"` with `pending_confirmation`) transition the ticket to `resolved_by_customer`; unverified customers receive a sign-in prompt (HTTP 403).
- **Rationale**: TDD §4.2 and §7.1. Eliminates ticket reference collisions across turns, prevents premature ticket closure on conditional statements, and protects human-owned tickets from automated closure.
- **Acceptance**: `tests/agents/test_turn_interpretation.py::test_deterministic_turn_act_enforcement`
- **Status**: done
- **Owner**: agent-service

### REQ-SAFE-005: Multi-Intent Sentence Deconstruction and Dual-Pipeline Grounded Resolution
- **Statement**: The triage router MUST detect compound customer messages with multiple distinct intents (e.g. package tracking + return policy) via the Gemini Flash turn interpreter (`REQ-TURN-001`), capturing structured sub-question intent spans (`intents: [{intent, text_span}]`) rather than heuristic keyword regexes, and forward them in `A2AHandoffPayload.sub_questions`. `FaqResolverAgent` MUST process all sub-questions by querying order status (for verified customers) and searching knowledge base articles in parallel or sequence, synthesizing a grounded response with mandatory verbatim citations covering all customer inquiries without premature escalation.
- **Rationale**: TDD §3 and §6. Resolves multi-intent customer inquiries in a single automated interaction without frustrating customers or overburdening support staff.
- **Acceptance**: `tests/agents/test_turn_interpretation.py::test_deterministic_turn_act_enforcement`
- **Status**: done
- **Owner**: agent-service

### REQ-SAFE-006: Staged Triage Pipeline and Golden Replay Verification Invariant
- **Statement**: Any refactoring of `TriageRouterAgent.triage()`, `TriagePolicy.evaluate()`, or `FaqResolverAgent._resolve_impl()` MUST preserve 100% byte-for-byte behavioral equivalence across all routing decisions, queues, customer-facing text, security refusals, and trace attributes. Equivalence MUST be verified against a comprehensive golden scenario replay harness (`tests/agents/test_triage_golden_replay.py` and `tests/agents/golden/triage_golden_v1.json`) containing at least 25 golden production turns covering: normal self-service intents, explicit escalations, Model Armor security triggers, payment card ingress blocks, conversation holding follow-ups, and multi-turn resolution flows.
- **Rationale**: TDD §3, §4.2, §14. Prevents subtle behavioral regressions during architectural pipeline decoupling.
- **Acceptance**: `tests/agents/test_triage_golden_replay.py` (27/27 scenarios exact match).
- **Status**: done
- **Owner**: agent-service
