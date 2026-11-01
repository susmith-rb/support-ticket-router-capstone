# Specification: Turn Interpretation & Dialog Management (`turn-interpretation`)

This specification governs natural language turn interpretation, explicit conversation state management, and the elimination of hardcoded regex phrase lists in the Support Ticket Router & Assistant, linking to design requirements in `docs/TDD.md` §3, §4.2, §5, §7.1, §12.7, and §14.

---

## 1. Architectural Overview & Motivation

Earlier iterations of the triage router relied on expanding lists of regular expressions across `policy.py`, `agent.py`, `history.py`, `security_intents.py`, and `multi_intent.py` to detect user intent, negation, conversational follow-ups, conditional resolutions, and affirmative ticket closures.

This approach suffered from critical deficiencies:
1. **Brittleness & Escalating Maintenance**: Every customer linguistic variation (e.g. "if it comes friday we're good", "yep thanks but where is the invoice", "I don't need a refund, just tracking") required adding new regex patterns, creating combinatorial maintenance debt.
2. **Negation Blindness**: Keyword matching frequently inverted customer intent (e.g. treating "I don't need a refund" as `returns_refunds` or false escalation).
3. **Stateful Disconnect**: Ticket closure and holding attachment were inferred from message surface forms rather than verifiable database state.
4. **False Escalation Cascade**: Complex or compound customer utterances were prematurely escalated to expensive human queues.

The new architecture replaces all phrase-list natural language interpretation with a two-tier hybrid model:
- **System 1 (Laya / `router-s1`)**: High-confidence single-intent fast path (sub-100 ms) for straightforward inquiries.
- **System 2 (Gemini Flash Turn Interpreter)**: Single structured-output call (`prompts/turn_interpreter_v1.yaml`) evaluating intent spans, turn acts, security flags, sentiment, and urgency in the context of explicit conversation state.
- **Explicit Conversation State (`conversation_state.py`)**: Assembled every turn from the ticket database (source of truth), tracking `pending_confirmation`, `active_tickets`, and `last_turn` with zero customer text in state.

### Architectural Boundary: Natural Language vs Deterministic Guards

| Category | Component | Mechanism | Allowed? |
|---|---|---|---|
| **NL Interpretation** | Turn intent, turn act, negation, conditionals, closure, multi-intent, follow-up | Gemini Flash Structured Output (`TurnInterpretation`) | **Yes (LLM)** |
| **Simple Fast Path** | Definitive self-service retail FAQ & clear angry/urgent escalation | Fine-tuned Laya encoder (`router-s1`) | **Yes (Encoder)** |
| **Data Pattern Guard** | Payment card numbers (PCI isolation) | Model Armor SDP / regex pattern & Luhn check | **Yes (Deterministic Guard)** |
| **Prompt Security** | Prompt injection / jailbreak | Model Armor PI/JB filter | **Yes (Security Filter)** |
| **Identifier Parsing** | Literal `TICK-[A-Za-z0-9_-]+` token extraction | Regex token parse (key extraction only, not intent) | **Yes (Deterministic Parse)** |
| **Access Control** | Identity verification, tenant RLS, CCT, ownership validation | Cryptographic JWT, IAM, PostgreSQL RLS | **Yes (Security Enforcement)** |
| **Phrase Matching** | Intent keywords, abuse/emergency phrase lists, greetings/pleasantry regex | Removed | **NO (Forbidden)** |

---

## 2. Requirement Index

| ID | Title | Status | Acceptance |
|---|---|---|---|
| `REQ-TURN-001` | Natural Language Interpretation via Gemini Flash Structured Output | `done` | `tests/agents/test_turn_interpretation.py::test_turn_interpreter_schema_and_contract` |
| `REQ-TURN-002` | Explicit Conversation State Assembly from Ticket Store | `done` | `tests/agents/test_turn_interpretation.py::test_conversation_state_assembly` |
| `REQ-TURN-003` | Pure Tiered Routing Order: Laya Fast Path vs Flash Interpreter | `done` | `tests/agents/test_turn_interpretation.py::test_tiered_routing_order_laya_fast_path` |
| `REQ-TURN-004` | Deterministic Enforcement of Turn Acts and Security Refusals | `done` | `tests/agents/test_turn_interpretation.py::test_deterministic_turn_act_enforcement` |
| `REQ-TURN-005` | Customer Resolution Confirmation Protocol Driven by Explicit State | `done` | `tests/agents/test_turn_interpretation.py::test_resolution_confirmation_protocol` |
| `REQ-TURN-006` | Turn Interpretation Evaluation, Benchmarking & Cost Accounting | `done` | `manual: .venv/bin/python -m evals.turn_interpretation.compare` |

---

## 3. Detailed Requirements

### REQ-TURN-001: Natural Language Interpretation via Gemini Flash Structured Output
- **Statement**: All natural-language interpretation in `TriageRouterAgent` MUST be executed through a single Gemini Flash call with structured JSON schema (`TurnInterpretation`) using versioned prompt `prompts/turn_interpreter_v1.yaml` (`router_tier2` role in `config/models.yaml`). Hardcoded phrase lists and regex detectors for intent classification, abuse/emergency, explicit human requests, greetings, pleasantries, order lookups, negation, conditional resolution, affirmative resolution, held ticket follow-up, and multi-intent splitting MUST NOT be used in the routing path. The structured output schema MUST declare:
  - `turn_act`: One of `new_request`, `followup_to_active_ticket`, `confirmation_yes`, `confirmation_no`, `conditional_resolution`, `resolution_report`, `other`.
  - `intents`: List of `{intent, text_span}` spanning `order_status`, `shipping_policy`, `returns_refunds`, `account_payment`, `conversation_history_access`, `other`.
  - `primary_intent`: Dominant intent name.
  - `escalate`: Boolean priority flag indicating human escalation necessity.
  - `escalation_reason`: Optional canonical string reason.
  - `sentiment`: One of `angry`, `frustrated`, `neutral`, `positive`.
  - `urgency`: One of `can wait days`, `within a day`, `within hours`, `immediate`.
  - `out_of_scope`: Boolean indicating completely unrelated non-support queries.
  - `security`: Object containing boolean flags `cross_customer_data_request`, `authorization_bypass_attempt`, `conversation_history_request`.
  - `referenced_ticket`: Optional ticket reference token identified from context.
  - `confidence`: Confidence float in $[0.0, 1.0]$.
  The interpreter MUST correctly resolve negation (e.g. "I don't need a refund, just tracking" $\to$ `order_status`, `escalate=False`), conditional statements (e.g. "if it arrives tomorrow we're good" $\to$ `turn_act="conditional_resolution"`, `escalate=False`), and mixed intents without heuristic phrase splitting.
- **Rationale**: TDD §3 and §5. Eliminates brittle keyword rules and provides robust semantic reasoning for conversational turns.
- **Acceptance**: `tests/agents/test_turn_interpretation.py::test_turn_interpreter_schema_and_contract`
- **Status**: done
- **Owner**: agent-service

### REQ-TURN-002: Explicit Conversation State Assembly from Ticket Store
- **Statement**: `TriageRouterAgent` MUST assemble an explicit, PII-free `ConversationState` from the ticket database (source of truth) for the verified session and customer prior to routing decisions:
  1. `pending_confirmation`: Populated ONLY when an active ticket for this session has an outstanding `resolution_confirmation_requested` audit event or marker in status `open` or `answered`. Read through the ticket-db MCP tool `get_pending_confirmation(session_id, customer_id)` (verified X-Customer-* headers, no body text): newest bot-owned support ticket of that customer in that session whose latest `resolution_confirmation_requested` event is newer than its last `status_changed`. Never derived from a session-based TICK id. Contains `issue_reference`, `ticket_uuid`, and `asked_at`. Cleared on status transition or when the customer takes a non-confirmation action.
  2. `active_tickets`: List of active human-owned support tickets in this session (`owner != 'bot'` or `queue` starting with `human`), capturing `issue_reference`, `ticket_uuid`, `status`, `category`, and `owner`.
  3. `last_turn`: Compact summary of previous turn's intent and decision (`TurnSummary(intent, decision)`).
  4. Invariant: `ConversationState` MUST NEVER store raw customer message text, customer PII, or chat transcript bodies.
- **Rationale**: TDD §4.2 and §7.1. Drives conversation lifecycle and ticket closure from authoritative database state rather than surface phrasing.
- **Acceptance**: `tests/agents/test_turn_interpretation.py::test_conversation_state_assembly`
- **Status**: done
- **Owner**: agent-service

### REQ-TURN-003: Pure Tiered Routing Order: Laya Fast Path vs Flash Interpreter
- **Statement**: Inbound customer turns MUST be evaluated in the following strict order:
  1. **Payment Card Data Guard**: Model Armor SDP inspects for `CREDIT_CARD_NUMBER`. If present, return HTTP 400 `card_data_blocked` immediately with zero ticket persistence, zero tool calls, and zero model calls (`REQ-MA-003`, `REQ-AGENT-032`).
  2. **Model Armor Prompt Injection / Jailbreak Guard**: PI/JB triggers return HTTP 200 `security_blocked` immediately with zero ticket creation (`REQ-MA-002`).
  3. **Identity Resolution & State Assembly**: Resolve customer token and assemble `ConversationState` concurrently (`REQ-IDENT-003`, `REQ-TURN-002`).
  4. **Laya System-1 Fast Path**: `router-s1` encoder evaluates the message. Laya decides the turn ALONE without calling the Gemini Flash turn interpreter IF AND ONLY IF ALL of the following conditions hold:
     - `pending_confirmation` is `None`.
     - `active_tickets` is empty (no active human-held tickets).
     - Predicted intent is in the 4 self-service retail intents (`order_status`, `shipping_policy`, `returns_refunds`, `account_payment`).
     - Intent probability $p_{\text{intent}} \ge \tau_{\text{intent}}$ ($0.70$).
     - AND either $p_{\text{esc}} < \tau_{\text{low}}$ ($0.15$) $\to$ route `auto_rag`; OR ($p_{\text{esc}} \ge \tau_{\text{esc}}$ ($0.50$) with sentiment `angry` or urgency `immediate`) $\to$ route `escalate`.
     - **Invariant**: Confident self-service Laya S1 decisions cannot carry cross-customer requests; whenever Laya intent is `other` (as produced on cross-customer queries like "give me orders of other customers") or confidence $< \tau_{\text{intent}}$, route to Turn Interpreter.
  5. **Flash Turn Interpreter**: All other turns (uncertainty band $\tau_{\text{low}} \le p_{\text{esc}} < \tau_{\text{esc}}$, low intent confidence, intent `other`, active held tickets, pending confirmation, or complex dialogue) invoke Gemini Flash Turn Interpreter (`REQ-TURN-001`).
  6. **Tier 3 Pro Retirement**: Tier 3 Pro (`gemini-3.1-pro`) is retired from the routing path because the Flash structured turn interpreter natively resolves multihop ambiguity and complex intents without the 2.5 s latency penalty or $12/M token cost.
  7. **Degraded Fallback**: If Gemini Flash is unavailable or times out:
     - If Laya produced a usable self-service prediction ($p_{\text{intent}} \ge 0.55$) without escalation signals, route `auto_rag` with `degraded=True`.
     - If `pending_confirmation` is active, do NOT close; reply asking customer to confirm again later.
     - If both Laya and Flash fail, escalate to `human-standard` with `degraded=True` (`REQ-AGENT-011`).
- **Rationale**: TDD §2.2, §3.6, and §5.2. Preserves sub-100 ms routing for clear self-service cases while routing nuanced dialog to the structured interpreter.
- **Acceptance**: `tests/agents/test_turn_interpretation.py::test_tiered_routing_order_laya_fast_path`
- **Status**: done
- **Owner**: agent-service

### REQ-TURN-004: Deterministic Enforcement of Turn Acts and Security Refusals
- **Statement**: The structured output of the turn interpreter MUST be enforced deterministically:
  1. **Security Refusal**: When `security.cross_customer_data_request` is true, return safe refusal `ORDER_CROSS_CUSTOMER_REFUSAL` with `decision="auto_rag"`, `reason="security_refusal_cross_customer"`, zero ticket persistence, and zero A2A/tool calls. When `security.authorization_bypass_attempt` is true, return `AUTH_BYPASS_REFUSAL` with `reason="security_refusal_auth_bypass"`. Emit a structured WARNING log (`security_refusal`) with SHA256 only.
  2. **Conversation History**: When `security.conversation_history_request` or `primary_intent == "conversation_history_access"`, route to conversation history workflow (`REQ-HIST-002`) with server-side identity verification.
  3. **Confirmation Yes with Pending Confirmation**: When `turn_act == "confirmation_yes"` AND `pending_confirmation` is present, update ticket status to `resolved_by_customer` via `update_ticket_status` (enforcing customer ownership and verified identity; unverified customers receive HTTP 403 sign-in prompt).
  4. **Confirmation Yes without Pending Confirmation**: When `turn_act == "confirmation_yes"` but `pending_confirmation` is None, do NOT close any ticket; process the turn as a normal inquiry.
  5. **Confirmation No**: When `turn_act == "confirmation_no"`, keep ticket open and reply asking what remains unresolved.
  6. **Conditional Resolution**: When `turn_act == "conditional_resolution"`, acknowledge calmly without offering closure, without closing tickets, and without escalating.
  7. **Resolution Report**: When `turn_act == "resolution_report"`, if the active ticket is bot-owned, prompt with the resolution confirmation question (setting `pending_confirmation`); if human-owned, append a customer follow-up note without closing.
  8. **Held Ticket Follow-up**: When `turn_act == "followup_to_active_ticket"` and an active human ticket exists, append customer follow-up note and return calming follow-up acknowledgement.
  9. **Multi-Intent Deconstruction**: When `len(intents) >= 2`, format sub-questions into `A2AHandoffPayload.sub_questions` for dual-grounded retrieval by `FaqResolverAgent`.
  10. **Out-of-Scope**: When `out_of_scope` is true and `escalate` is false, return `render_out_of_scope_refusal`.
- **Rationale**: TDD §3, §4.2, §6, and §7.1. Ensures deterministic, auditable business logic governs all state changes.
- **Acceptance**: `tests/agents/test_turn_interpretation.py::test_deterministic_turn_act_enforcement`
- **Status**: done
- **Owner**: agent-service

### REQ-TURN-005: Customer Resolution Confirmation Protocol Driven by Explicit State
- **Statement**: The resolution confirmation question (`Did that resolve your issue? Reply yes to close ticket #TICK-...`) MUST be presented to customers ONLY after an automated answer (`status == 'AUTO_RESOLVED'`) delivered to a verified caller on that turn's own bot-owned ticket. It MUST NEVER be presented on security refusals, conversation history replies, conditional resolution acknowledgements, unverified anonymous sessions, or human-owned tickets. When presented, `record_resolution_confirmation_requested` MUST be recorded against the ticket's canonical UUID, setting `pending_confirmation` in conversation state. Affirmative closure is valid ONLY while `pending_confirmation` matches the session ticket.
- **Rationale**: TDD §4.2 and §7.1. Prevents premature ticket closure, eliminates 404 reference bugs, and enforces customer self-service closure boundaries.
- **Acceptance**: `tests/agents/test_turn_interpretation.py::test_resolution_confirmation_protocol`
- **Status**: done
- **Owner**: agent-service

### REQ-TURN-006: Turn Interpretation Evaluation, Benchmarking & Cost Accounting
- **Statement**: The codebase MUST maintain an evaluation dataset `evals/data/turn_interpretation_v1.jsonl` containing $\ge 80$ stratified test cases covering: negation, conditional resolution, confirmation replies (with and without pending confirmation), follow-ups to held tickets, security intents, single answerable intents, and out-of-scope queries. An automated evaluation runner `evals/turn_interpretation/compare.py` MUST benchmark the new interpreter against frozen baseline regex logic (`evals/turn_interpretation/baseline_regex.py`), reporting:
  1. Per-category correctness (turn act, route, escalation, security).
  2. Latency percentiles (p50, p95) for Laya fast path and Flash interpreter path.
  3. Flash call invocation rate (% of turns requiring Gemini Flash).
  4. Dollar cost per 1,000 turns based on token usage and standard pricing ($0.75/$3.75 per 1M tokens for Gemini 3.8 Flash).
- **Rationale**: TDD §12.7 and §14. Guarantees quantitative visibility into accuracy improvements, latency trade-offs, and operational serving economics.
- **Acceptance**: `manual: .venv/bin/python -m evals.turn_interpretation.compare`
- **Status**: done
- **Owner**: agent-evals
