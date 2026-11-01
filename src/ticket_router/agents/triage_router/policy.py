"""Pure routing decision policy for TriageRouterAgent.

# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
# REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment
# REQ-TURN-001: Natural Language Interpretation via Gemini Flash Structured Output
# REQ-TURN-003: Pure Tiered Routing Order: Laya Fast Path vs Flash Interpreter
# REQ-TURN-004: Deterministic Enforcement of Turn Acts and Security Refusals
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from ticket_router.agents.triage_router.conversation_state import ConversationState
from ticket_router.agents.triage_router.security_intents import (
    AUTH_BYPASS_REFUSAL,
    ORDER_CROSS_CUSTOMER_REFUSAL,
)
from ticket_router.agents.triage_router.templates import (
    render_followup_calming_reply,
    render_security_blocked_reply,
)
from ticket_router.agents.triage_router.turn_interpreter import (
    AllowedSentiment,
    AllowedUrgency,
    IntentSpan,
    SecurityFlags,
    TurnInterpretation,
    TurnInterpreter,
    create_turn_interpreter,
)
from ticket_router.common.resilience import (
    CircuitBreakerError,
    ServiceTimeoutError,
)
from ticket_router.models.router_s1_client import RouterPrediction, RouterS1Client
from ticket_router.security.model_armor import (
    ModelArmorClientProtocol,
    ModelArmorVerdict,
)
from ticket_router.security.pii_tokens import tokenize_pii
from ticket_router.store.ids import legacy_session_issue_reference

logger = logging.getLogger(__name__)


class QueuePriority(str, Enum):
    """Operational queue priority and target routing queues (REQ-AGENT-004)."""
    P1_URGENT = "human-urgent"
    P2_STANDARD = "human-standard"
    P3_LOW = "human-low"


@dataclass(frozen=True)
class RouterThresholds:
    """Configurable routing policy thresholds (REQ-AGENT-002, REQ-EVAL-008).

    Operating points per specs/agent-service.md & agent_tuning.yaml:
    tau_esc: Definitive escalation floor
    tau_low: Auto-RAG uncertainty lower bound
    tau_intent: Intent confidence floor
    tau_usable_laya: S1 usable intent threshold during degraded fallback
    """
    tau_esc: float = 0.50
    tau_low: float = 0.15
    tau_intent: float = 0.70
    tau_usable_laya: float = 0.55

    @classmethod
    def from_tuning_config(cls) -> "RouterThresholds":
        """Load current thresholds from centralized agent tuning registry."""
        try:
            from ticket_router.config.agent_tuning import get_tuning_config
            cfg = get_tuning_config()
            return cls(
                tau_esc=cfg.tau_esc,
                tau_low=cfg.tau_low,
                tau_intent=cfg.tau_intent,
                tau_usable_laya=cfg.tau_usable_laya,
            )
        except Exception:
            return cls()


@dataclass(frozen=True)
class VerifiedFacts:
    """Objective, verified customer and account facts from backend stores (REQ-AGENT-004).

    Queue priority is derived strictly from these verified facts, preventing
    manipulative customer anger or shouting from gaming priority SLA.
    """
    vip_customer: bool = False
    order_value: float = 0.0
    delivery_delay_hours: float = 0.0
    stranded_return: bool = False
    repeat_contact: bool = False


@dataclass(frozen=True)
class Tier0Result:
    """Legacy deterministic regex rule evaluation result (kept for test compatibility)."""
    matched: bool = False
    escalate: bool = False
    reason: Optional[str] = None
    category: Optional[str] = None
    sentiment: str = "neutral"
    urgency: str = "normal"


@dataclass(frozen=True)
class RouterDecision:
    """Typed decision returned by TriageRouter policy evaluation."""
    ticket_id: str
    session_id: str
    decision: str  # "auto_rag", "escalate", "reject_oos", "blocked", "security_blocked"
    tier: str      # "ingress_guard", "model_armor", "tier1_router_s1", "tier2_flash", "ticket_lifecycle", "fallback_degraded"
    queue: str     # "none", "human-urgent", "human-standard", "human-low"
    predicted_intent: str
    escalate: bool
    sentiment: str
    urgency: str
    reason: str
    degraded: bool = False
    calming_ack: Optional[str] = None
    latency_ms: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)
    customer_verified: bool = False
    identity_issuer: Optional[str] = None
    identity_reason: str = "missing_token"
    tokenized_text: Optional[str] = None
    pii_mapping: Dict[str, str] = field(default_factory=dict, repr=False)


def _extract_sdp_findings(verdict: Optional[ModelArmorVerdict]) -> List[Any]:
    """Extract raw SDP findings list from ModelArmorVerdict if present."""
    if verdict is None:
        return []
    if verdict.raw_filter_results:
        sdp_data = (
            verdict.raw_filter_results.get("sdpFilterResult")
            or verdict.raw_filter_results.get("sdp_filter_result")
            or {}
        )
        inspect_res = (
            sdp_data.get("inspectResult")
            or sdp_data.get("inspect_result")
            or sdp_data.get("sdpInspectionResult")
            or sdp_data.get("sdp_inspection_result")
            or {}
        )
        findings = inspect_res.get("findings")
        if findings and isinstance(findings, list):
            return findings
    if verdict.sdp_findings:
        return list(verdict.sdp_findings)
    return []


BLOCKED_SDP_INFO_TYPES = frozenset({"CREDIT_CARD_NUMBER"})


def _has_dict_location(finding: Any) -> bool:
    """Return True if the finding has a dict-based location (e.g. codepointRange)."""
    if isinstance(finding, dict):
        loc = finding.get("location")
        return isinstance(loc, dict)
    loc = getattr(finding, "location", None)
    return isinstance(loc, dict)


def _extract_blocked_card_info_types(verdict: Optional[ModelArmorVerdict]) -> List[str]:
    """Extract blocked card info types from findings that have dict locations (TDD §4.2 C4)."""
    if verdict is None:
        return []
    blocked: List[str] = []
    for f in _extract_sdp_findings(verdict):
        if not _has_dict_location(f):
            continue
        if isinstance(f, dict):
            raw = f.get("infoType") or f.get("info_type")
        else:
            raw = getattr(f, "info_type", None) or getattr(f, "infoType", None)
        if raw:
            norm = str(raw).strip().upper()
            if norm in BLOCKED_SDP_INFO_TYPES and norm not in blocked:
                blocked.append(norm)
    return blocked


def _summarize_s1_prediction(s1_pred: Optional[RouterPrediction]) -> Optional[Dict[str, Any]]:
    """Summarize router-s1 prediction into safe, PII-free numeric/enum metadata (REQ-AGENT-038)."""
    if s1_pred is None:
        return None
    return {
        "escalate": bool(s1_pred.escalate),
        "escalate_prob": round(float(s1_pred.escalate_prob), 4),
        "intent": str(s1_pred.intent),
        "intent_prob": round(float(s1_pred.intent_prob), 4),
        "sentiment": str(s1_pred.sentiment),
        "urgency": str(s1_pred.urgency),
    }


_SELF_SERVICE_INTENTS = (
    "order_status",
    "orders",
    "shipping_policy",
    "shipping",
    "shipping_inquiry",
    "returns_refunds",
    "returns",
    "account_payment",
    "account",
    "billing",
)


def evaluate_gaming_resistant_priority(
    facts: Optional[VerifiedFacts] = None,
    tier0: Optional[Tier0Result] = None,
    sentiment: str = "neutral",
    urgency: str = "normal",
) -> QueuePriority:
    """Assign human review queue priority based strictly on verified business facts (REQ-AGENT-004)."""
    if tier0 and tier0.category in ("emergency", "legal"):
        return QueuePriority.P1_URGENT

    if facts:
        if facts.order_value >= 100.0:
            return QueuePriority.P1_URGENT
        if facts.delivery_delay_hours >= 24.0:
            return QueuePriority.P1_URGENT
        if facts.stranded_return or facts.vip_customer:
            return QueuePriority.P1_URGENT
        if facts.repeat_contact:
            return QueuePriority.P1_URGENT

    return QueuePriority.P2_STANDARD


class TriagePolicy:
    """Orchestrates Model Armor, Tier 1 router-s1, and Gemini Flash Turn Interpreter.

    Guarantees:
    - Add-only escalation: final.escalate = s1_escalate or interpreter.escalate (REQ-AGENT-003).
    - Sub-100 ms routing for definitive self-service cases via Laya Fast Path (REQ-TURN-003).
    - All complex dialogue, negation, held tickets, and ambiguous intent routed to Turn Interpreter (REQ-TURN-001).
    - Gaming-resistant queue priority based on verified facts (REQ-AGENT-004).
    - Fallback degradation when Turn Interpreter or Laya times out/fails (REQ-TURN-003, REQ-AGENT-011).
    """

    def __init__(
        self,
        thresholds: Optional[RouterThresholds] = None,
        s1_client: Optional[RouterS1Client] = None,
        tier2_invoker: Optional[Callable[[str], RouterPrediction]] = None,
        turn_interpreter: Optional[TurnInterpreter] = None,
        model_armor_client: Optional[ModelArmorClientProtocol] = None,
        prompt_safety_delegated_to_gateway: Optional[bool] = None,
    ) -> None:
        self.thresholds = thresholds or RouterThresholds()
        self.s1_client = s1_client
        self.tier2_invoker = tier2_invoker
        self.model_armor_client = model_armor_client
        self.prompt_safety_delegated_to_gateway = prompt_safety_delegated_to_gateway
        self._custom_turn_interpreter = turn_interpreter is not None or tier2_invoker is not None

        if turn_interpreter is not None:
            self.turn_interpreter = turn_interpreter
        elif self.tier2_invoker is not None:
            # Adapt legacy tier2_invoker (Callable[[str], RouterPrediction]) to TurnInterpretation
            def _adapted_ti_invoker(
                text_input: str,
                conversation_state: Optional[ConversationState] = None,
                recent_intents: Optional[List[str]] = None,
            ) -> TurnInterpretation:
                if conversation_state and len(conversation_state.active_tickets) > 0:
                    return TurnInterpretation(
                        turn_act="followup_to_active_ticket",
                        intents=[IntentSpan(intent="followup", text_span=text_input)],
                        primary_intent="followup",
                        escalate=True,
                        escalation_reason="human_ticket_followup",
                        sentiment="neutral",
                        urgency="normal",
                        out_of_scope=False,
                        confidence=1.0,
                        latency_ms=0.0,
                    )
                invoker_fn = self.tier2_invoker if callable(self.tier2_invoker) else getattr(self.tier2_invoker, "invoke", None)
                pred = invoker_fn(text_input)
                if isinstance(pred, TurnInterpretation):
                    return pred
                act = "new_request"
                pred_turn_act = getattr(pred, "turn_act", None)
                if isinstance(pred_turn_act, str) and pred_turn_act:
                    act = pred_turn_act
                elif getattr(pred, "intent", None) == "confirmation":
                    act = "confirmation_yes"
                elif getattr(pred, "intent", None) == "status_inquiry":
                    act = "resolution_report"
                sec = SecurityFlags(
                    cross_customer_data_request=bool(getattr(pred, "intent", None) == "cross_customer_data_request"),
                    authorization_bypass_attempt=bool(getattr(pred, "intent", None) == "authorization_bypass_attempt"),
                    conversation_history_request=bool(getattr(pred, "intent", None) == "conversation_history_access"),
                )
                p_intent = str(getattr(pred, "intent", "other"))
                intent_val = p_intent
                sent = str(getattr(pred, "sentiment", "neutral")).lower()
                sent_enum: AllowedSentiment = sent if sent in ("angry", "frustrated", "neutral", "positive") else "neutral"  # type: ignore[assignment]
                urg = str(getattr(pred, "urgency", "within a day")).lower()
                urg_enum: AllowedUrgency = urg if urg in ("can wait days", "within a day", "within hours", "immediate") else "within a day"  # type: ignore[assignment]

                raw_details = dict(getattr(pred, "raw", None) or {})
                # REQ-SAFE-002: on a self-service intent a bare Tier 2 escalate flag is honoured only with a real escalation signal
                t2_self_service = p_intent in (
                    "order_status",
                    "shipping_policy",
                    "returns_refunds",
                    "account_payment",
                )
                is_human_req = any(p in text_input.lower() for p in [
                    "speak to a human", "talk to a human", "human representative",
                    "agent please", "representative please", "speak to an agent",
                    "talk to an agent", "live agent",
                ])
                has_signal = (
                    sent_enum == "angry"
                    or urg_enum == "immediate"
                    or is_human_req
                    or any(w in text_input.lower() for w in [
                        "lawyer", "lawsuit", "police", "kill", "die", "emergency",
                        "fraud", "stolen", "chargeback",
                    ])
                )
                t2_esc = bool(getattr(pred, "escalate", False)) or is_human_req
                if is_human_req:
                    raw_details["category"] = "human_request"
                if t2_self_service and not has_signal:
                    t2_esc = False

                pred_esc_reason = getattr(pred, "escalation_reason", None)
                esc_reason = (
                    str(pred_esc_reason)
                    if isinstance(pred_esc_reason, str) and pred_esc_reason
                    else ("tier2_flash_escalation" if t2_esc else None)
                )

                return TurnInterpretation(
                    turn_act=act,
                    intents=[IntentSpan(intent=intent_val, text_span=text_input)],
                    primary_intent=intent_val,
                    escalate=t2_esc,
                    escalation_reason=esc_reason,
                    sentiment=sent_enum,
                    urgency=urg_enum,
                    out_of_scope=bool(getattr(pred, "out_of_scope", False)),
                    security=sec,
                    confidence=float(getattr(pred, "intent_prob", 1.0)),
                    latency_ms=float(getattr(pred, "latency_ms", 0.0)),
                    details=raw_details,
                )

            self.turn_interpreter = TurnInterpreter(invoker=_adapted_ti_invoker)
        else:
            self.turn_interpreter = create_turn_interpreter()

    def _is_prompt_safety_delegated(self) -> bool:
        """Check if prompt safety checks (PI/JB, URI, RAI) are delegated to Agent Gateway ingress."""
        if self.prompt_safety_delegated_to_gateway is not None:
            return self.prompt_safety_delegated_to_gateway
        from ticket_router.security.model_armor import is_prompt_safety_delegated_to_gateway

        return is_prompt_safety_delegated_to_gateway()

    def _call_s1(self, text: str) -> Tuple[Optional[RouterPrediction], Optional[str]]:
        """Execute router-s1 encoder call with error handling."""
        if self.s1_client is None:
            return None, None
        try:
            return self.s1_client.route(text), None
        except CircuitBreakerError as cb_exc:
            return None, f"circuit_breaker_open: {cb_exc}"
        except Exception as exc:
            return None, f"endpoint_error: {exc}"

    def _run_model_armor(
        self, text: str
    ) -> Tuple[Optional[ModelArmorVerdict], Optional[str], Optional[str], Dict[str, str]]:
        """Execute Model Armor sanitization and PII tokenization safely."""
        if self.model_armor_client is None:
            return None, None, None, {}

        try:
            ma_verdict = self.model_armor_client.sanitize_user_prompt(text)
        except Exception as exc:
            logger.warning("Model Armor prompt sanitization failed: %s", exc)
            degraded = ModelArmorVerdict(
                is_clean=False,
                blocked=False,
                action="DEGRADE",
                degraded=True,
                reason=f"Model Armor call failed ({type(exc).__name__})",
                latency_ms=0.0,
            )
            return degraded, None, None, {}

        sanitized_text: Optional[str] = None
        tokenized_text: Optional[str] = None
        pii_mapping: Dict[str, str] = {}

        if ma_verdict is not None:
            blocked_types = _extract_blocked_card_info_types(ma_verdict)
            if blocked_types:
                return ma_verdict, None, None, {}

            try:
                findings = _extract_sdp_findings(ma_verdict)
                if findings:
                    tok_text, tok_map = tokenize_pii(text, findings)
                    if tok_map or tok_text != text:
                        tokenized_text = tok_text
                        pii_mapping = tok_map
                        sanitized_text = tokenized_text
                    elif ma_verdict.sanitized_text:
                        sanitized_text = ma_verdict.sanitized_text
                elif ma_verdict.sanitized_text:
                    sanitized_text = ma_verdict.sanitized_text
            except Exception as exc:
                logger.warning("PII tokenization failed on Model Armor findings: %s", exc)

        return ma_verdict, sanitized_text, tokenized_text, pii_mapping

    def evaluate(
        self,
        text: str,
        session_id: str = "default_session",
        customer_id: Optional[str] = None,
        verified_facts: Optional[VerifiedFacts] = None,
        ticket_id: Optional[str] = None,
        precomputed_ma: Optional[
            Tuple[Optional[ModelArmorVerdict], Optional[str], Optional[str], Dict[str, str]]
        ] = None,
        conversation_state: Optional[ConversationState] = None,
        recent_intents: Optional[List[str]] = None,
    ) -> RouterDecision:
        """Execute tiered routing policy on inbound message text (REQ-TURN-003)."""
        t_id = ticket_id or legacy_session_issue_reference(session_id)
        s1_summary: Optional[Dict[str, Any]] = None
        tokenized_text: Optional[str] = None
        pii_mapping: Dict[str, str] = {}
        prompt_safety_shadow: Optional[Dict[str, Any]] = None

        # ---------------------------------------------------------------------
        # Tier 1 & Model Armor: Execution (REQ-MA-003, REQ-MA-005)
        # ---------------------------------------------------------------------
        ma_verdict: Optional[ModelArmorVerdict] = None
        s1_pred: Optional[RouterPrediction] = None
        s1_error: Optional[str] = None
        sanitized_text: Optional[str] = None
        tokenized_text: Optional[str] = None
        pii_mapping: Dict[str, str] = {}
        prompt_safety_shadow: Optional[Dict[str, Any]] = None

        if precomputed_ma is not None:
            ma_verdict, sanitized_text, tokenized_text, pii_mapping = precomputed_ma
            s1_pred, s1_error = self._call_s1(text)
        elif self.model_armor_client is not None:
            if self._custom_turn_interpreter:
                # Sequential guard for custom/mock turn interpreter tests (REQ-TURN-003)
                ma_verdict, sanitized_text, tokenized_text, pii_mapping = self._run_model_armor(text)
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    ma_future = executor.submit(self._run_model_armor, text)
                    s1_future = executor.submit(self._call_s1, text)
                    ma_verdict, sanitized_text, tokenized_text, pii_mapping = ma_future.result()
                    s1_pred, s1_error = s1_future.result()
        else:
            s1_pred, s1_error = self._call_s1(text)

        ma_details: Dict[str, Any] = {}
        if ma_verdict is not None:
            ma_details = ma_verdict.to_summary_dict()

            # 0. Payment Card Data -> Ingress Guard Hard Block (TDD §4.2 C4, REQ-MA-007)
            blocked_types = _extract_blocked_card_info_types(ma_verdict)
            if blocked_types:
                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="blocked",
                    tier="ingress_guard",
                    queue="none",
                    predicted_intent="other",
                    escalate=False,
                    sentiment="neutral",
                    urgency="normal",
                    reason="card_data_blocked",
                    degraded=False,
                    details={"blocked_info_types": blocked_types},
                    tokenized_text=None,
                    pii_mapping={},
                )

            delegated = self._is_prompt_safety_delegated()
            if delegated and (ma_verdict.has_pi_jb or ma_verdict.has_malicious_uri or ma_verdict.has_rai_violation):
                prompt_safety_shadow = {
                    "shadow_mode": True,
                    "pi_jb_tripped": ma_verdict.has_pi_jb,
                    "malicious_uri_tripped": ma_verdict.has_malicious_uri,
                    "rai_tripped": ma_verdict.has_rai_violation,
                }
                ma_details["prompt_safety_shadow"] = prompt_safety_shadow
                logger.info(
                    "prompt_safety_shadow: verdict tripped (pi_jb=%s, malicious_uri=%s, rai=%s), "
                    "delegated to gateway ingress, monitor-only (not escalating)",
                    ma_verdict.has_pi_jb,
                    ma_verdict.has_malicious_uri,
                    ma_verdict.has_rai_violation,
                    extra={
                        "event": "prompt_safety_shadow",
                        "model_armor": ma_details,
                        "session_id": session_id,
                        "ticket_id": t_id,
                    },
                )
            else:
                # 1. Prompt Injection or Jailbreak -> Security Refusal Block (REQ-MA-003)
                if ma_verdict.has_pi_jb:
                    sec_ack = render_security_blocked_reply()
                    return RouterDecision(
                        ticket_id=t_id,
                        session_id=session_id,
                        decision="security_blocked",
                        tier="model_armor",
                        queue="none",
                        predicted_intent="other",
                        escalate=False,
                        sentiment="neutral",
                        urgency="normal",
                        reason="prompt_injection_or_jailbreak",
                        degraded=False,
                        calming_ack=sec_ack,
                        details={
                            "category": "security",
                            "model_armor": ma_details,
                            "sanitized_text": sanitized_text,
                        },
                        tokenized_text=tokenized_text,
                        pii_mapping=pii_mapping,
                    )

                # 2. Malicious URI -> Hard Security Escalation to Human Review
                if ma_verdict.has_malicious_uri:
                    sec_queue = evaluate_gaming_resistant_priority(
                        facts=verified_facts,
                        sentiment="neutral",
                        urgency="normal",
                    )
                    return RouterDecision(
                        ticket_id=t_id,
                        session_id=session_id,
                        decision="escalate",
                        tier="model_armor",
                        queue=sec_queue.value,
                        predicted_intent="other",
                        escalate=True,
                        sentiment="neutral",
                        urgency="normal",
                        reason="model_armor_security_trigger",
                        degraded=False,
                        details={
                            "category": "security",
                            "model_armor": ma_details,
                            "sanitized_text": sanitized_text,
                        },
                        tokenized_text=tokenized_text,
                        pii_mapping=pii_mapping,
                    )

                # 3. Responsible AI (RAI) Harassment / Abuse -> Boundary Ack + Human Review
                if ma_verdict.has_rai_violation:
                    queue = evaluate_gaming_resistant_priority(
                        facts=verified_facts,
                        sentiment="angry",
                        urgency="normal",
                    )
                    return RouterDecision(
                        ticket_id=t_id,
                        session_id=session_id,
                        decision="escalate",
                        tier="model_armor",
                        queue=queue.value,
                        predicted_intent="other",
                        escalate=True,
                        sentiment="angry",
                        urgency="normal",
                        reason="model_armor_rai_trigger",
                        degraded=False,
                        details={
                            "category": "abuse",
                            "model_armor": ma_details,
                            "sanitized_text": sanitized_text,
                        },
                        tokenized_text=tokenized_text,
                        pii_mapping=pii_mapping,
                    )

            # 4. Model Armor Degraded / Timeout -> Fail-safe to human-standard (never drop)
            if ma_verdict.degraded:
                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="escalate",
                    tier="model_armor",
                    queue=QueuePriority.P2_STANDARD.value,
                    predicted_intent="other",
                    escalate=True,
                    sentiment="neutral",
                    urgency="normal",
                    reason="model_armor_degraded",
                    degraded=True,
                    details={
                        "category": "degraded",
                        "model_armor": ma_details,
                    },
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

        # ---------------------------------------------------------------------
        # 2. System 1 (router-s1): Laya Fast Path (REQ-TURN-003)
        # ---------------------------------------------------------------------
        if s1_pred is None and self.s1_client is not None:
            s1_pred, s1_error = self._call_s1(text)

        s1_summary = _summarize_s1_prediction(s1_pred)

        has_pending = conversation_state is not None and conversation_state.pending_confirmation is not None
        has_held_tickets = conversation_state is not None and len(conversation_state.active_tickets) > 0

        # A. Definitive escalation fast path (REQ-AGENT-002, REQ-REVAL-011)
        s1_esc_prob = getattr(s1_pred, "escalate_prob", None)
        is_esc_prob = isinstance(s1_esc_prob, (int, float)) and s1_esc_prob >= self.thresholds.tau_esc
        s1_intent_prob = getattr(s1_pred, "intent_prob", None)
        is_intent_prob = isinstance(s1_intent_prob, (int, float)) and s1_intent_prob >= self.thresholds.tau_intent
        s1_sentiment = str(getattr(s1_pred, "sentiment", "")).lower()
        s1_urgency = str(getattr(s1_pred, "urgency", "")).lower()
        s1_intent = str(getattr(s1_pred, "intent", ""))

        # Laya fast path never decides security or history requests: route to the interpreter
        # whenever Laya intent is "other" or confidence < tau_intent.
        # Confident self-service Laya decisions cannot carry cross-customer requests.
        is_confident_escalation_candidate = (
            s1_intent != "other"
            and (s1_intent in _SELF_SERVICE_INTENTS or s1_intent == "complaint")
            and is_intent_prob
        )

        if (
            not has_pending
            and not has_held_tickets
            and s1_pred is not None
            and is_confident_escalation_candidate
            and (
                is_esc_prob
                or s1_sentiment == "angry"
                or s1_urgency == "immediate"
            )
        ):
            queue = evaluate_gaming_resistant_priority(
                facts=verified_facts,
                sentiment=s1_sentiment or "neutral",
                urgency=s1_urgency or "normal",
            )
            details_esc: Dict[str, Any] = {}
            if isinstance(s1_esc_prob, (int, float)):
                details_esc["escalate_prob"] = float(s1_esc_prob)
            if isinstance(getattr(s1_pred, "sentiment_prob", None), (int, float)):
                details_esc["sentiment_prob"] = float(s1_pred.sentiment_prob)
            if isinstance(getattr(s1_pred, "intent_prob", None), (int, float)):
                details_esc["intent_prob"] = float(s1_pred.intent_prob)

            if s1_summary is not None:
                details_esc["s1_prediction"] = s1_summary
            if ma_details:
                details_esc["model_armor"] = ma_details
            if sanitized_text:
                details_esc["sanitized_text"] = sanitized_text
            if prompt_safety_shadow:
                details_esc["prompt_safety_shadow"] = prompt_safety_shadow

            return RouterDecision(
                ticket_id=t_id,
                session_id=session_id,
                decision="escalate",
                tier="tier1_router_s1",
                queue=queue.value,
                predicted_intent=str(getattr(s1_pred, "intent", "other")),
                escalate=True,
                sentiment=s1_sentiment or "neutral",
                urgency=s1_urgency or "normal",
                reason="tier1_definitive_escalation",
                degraded=False,
                latency_ms=float(getattr(s1_pred, "latency_ms", 0.0) or 0.0) if isinstance(getattr(s1_pred, "latency_ms", None), (int, float)) else 0.0,
                details=details_esc,
                tokenized_text=tokenized_text,
                pii_mapping=pii_mapping,
            )

        # B. Definitive auto-RAG fast path (REQ-TURN-003)
        s1_intent_prob = getattr(s1_pred, "intent_prob", None)
        is_intent_prob = isinstance(s1_intent_prob, (int, float)) and s1_intent_prob >= self.thresholds.tau_intent
        is_rag_prob = isinstance(s1_esc_prob, (int, float)) and s1_esc_prob < self.thresholds.tau_low
        s1_intent = str(getattr(s1_pred, "intent", ""))

        if (
            not has_pending
            and not has_held_tickets
            and s1_pred is not None
            and s1_intent in _SELF_SERVICE_INTENTS
            and is_intent_prob
            and is_rag_prob
        ):
            details_rag: Dict[str, Any] = {}
            if isinstance(s1_esc_prob, (int, float)):
                details_rag["escalate_prob"] = float(s1_esc_prob)
            if isinstance(s1_intent_prob, (int, float)):
                details_rag["intent_prob"] = float(s1_intent_prob)

            if s1_summary is not None:
                details_rag["s1_prediction"] = s1_summary
            if ma_details:
                details_rag["model_armor"] = ma_details
            if sanitized_text:
                details_rag["sanitized_text"] = sanitized_text
            if prompt_safety_shadow:
                details_rag["prompt_safety_shadow"] = prompt_safety_shadow

            return RouterDecision(
                ticket_id=t_id,
                session_id=session_id,
                decision="auto_rag",
                tier="tier1_router_s1",
                queue="none",
                predicted_intent=s1_intent,
                escalate=False,
                sentiment=s1_sentiment or "neutral",
                urgency=s1_urgency or "normal",
                reason="tier1_definitive_auto_rag",
                degraded=False,
                latency_ms=float(getattr(s1_pred, "latency_ms", 0.0) or 0.0) if isinstance(getattr(s1_pred, "latency_ms", None), (int, float)) else 0.0,
                details=details_rag,
                tokenized_text=tokenized_text,
                pii_mapping=pii_mapping,
            )

        # ---------------------------------------------------------------------
        # 3. System 2: Gemini Flash Turn Interpreter (REQ-TURN-001, REQ-TURN-003)
        # ---------------------------------------------------------------------
        text_for_ti = tokenized_text if tokenized_text is not None else text
        ti_interpretation: Optional[TurnInterpretation] = None
        ti_error: Optional[str] = None
        ti_timed_out = False
        t_ti_start = time.monotonic()

        try:
            ti_interpretation = self.turn_interpreter(
                text_for_ti,
                conversation_state=conversation_state,
                recent_intents=recent_intents,
            )
        except ServiceTimeoutError as exc:
            ti_timed_out = True
            ti_error = f"turn_interpreter_timeout: {exc}"
        except Exception as exc:
            if isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError)) or "timeout" in str(exc).lower():
                ti_timed_out = True
            ti_error = f"turn_interpreter_error: {exc}"

        ti_latency_ms = (time.monotonic() - t_ti_start) * 1000.0

        if ti_interpretation is not None:
            # Deterministic Turn Acts and Security Refusals (REQ-TURN-004)
            details_ti: Dict[str, Any] = {
                "s1_error": s1_error,
                "turn_act": ti_interpretation.turn_act,
                "confidence": ti_interpretation.confidence,
                "turn_interpretation": ti_interpretation.model_dump(),
            }
            if ti_interpretation.details:
                details_ti.update(ti_interpretation.details)
            if s1_summary is not None:
                details_ti["s1_prediction"] = s1_summary
            if ma_details:
                details_ti["model_armor"] = ma_details
            if sanitized_text:
                details_ti["sanitized_text"] = sanitized_text
            if prompt_safety_shadow:
                details_ti["prompt_safety_shadow"] = prompt_safety_shadow

            # 1. Security Refusals & Conversation History Access
            if (
                ti_interpretation.security.conversation_history_request
                or ti_interpretation.primary_intent == "conversation_history_access"
            ):
                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="auto_rag",
                    tier="tier2_flash",
                    queue="none",
                    predicted_intent="conversation_history_access",
                    escalate=False,
                    sentiment="neutral",
                    urgency="normal",
                    reason="conversation_history_access",
                    degraded=False,
                    latency_ms=ti_interpretation.latency_ms or ti_latency_ms,
                    details=details_ti,
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            if ti_interpretation.security.cross_customer_data_request:
                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="auto_rag",
                    tier="tier2_flash",
                    queue="none",
                    predicted_intent="cross_customer_data_request",
                    escalate=False,
                    sentiment="neutral",
                    urgency="normal",
                    reason="security_refusal_cross_customer_data_request",
                    degraded=False,
                    calming_ack=ORDER_CROSS_CUSTOMER_REFUSAL,
                    latency_ms=ti_interpretation.latency_ms or ti_latency_ms,
                    details=details_ti,
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            if ti_interpretation.security.authorization_bypass_attempt:
                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="auto_rag",
                    tier="tier2_flash",
                    queue="none",
                    predicted_intent="authorization_bypass_attempt",
                    escalate=False,
                    sentiment="neutral",
                    urgency="normal",
                    reason="security_refusal_authorization_bypass_attempt",
                    calming_ack=AUTH_BYPASS_REFUSAL,
                    latency_ms=ti_interpretation.latency_ms or ti_latency_ms,
                    details=details_ti,
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            # 2. Turn Acts: Conditional Resolution
            if ti_interpretation.turn_act == "conditional_resolution":
                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="auto_rag",
                    tier="ticket_lifecycle",
                    queue="none",
                    predicted_intent="status_inquiry",
                    escalate=False,
                    sentiment="neutral",
                    urgency="normal",
                    reason="conditional_resolution_acknowledgement",
                    degraded=False,
                    calming_ack="Understood! Please keep us posted once your package arrives, or let us know if anything else comes up.",
                    latency_ms=ti_interpretation.latency_ms or ti_latency_ms,
                    details={**details_ti, "condition_detected": True},
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            # 3. Turn Acts: Confirmation Yes / No
            if ti_interpretation.turn_act in ("confirmation_yes", "confirmation_no"):
                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="auto_rag",
                    tier="ticket_lifecycle",
                    queue="none",
                    predicted_intent="confirmation",
                    escalate=False,
                    sentiment="neutral",
                    urgency="normal",
                    reason=f"turn_act_{ti_interpretation.turn_act}",
                    degraded=False,
                    latency_ms=ti_interpretation.latency_ms or ti_latency_ms,
                    details=details_ti,
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            # 4. Turn Act: Resolution Report (REQ-TURN-004 item 7)
            if ti_interpretation.turn_act == "resolution_report":
                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="auto_rag",
                    tier="ticket_lifecycle",
                    queue="none",
                    predicted_intent="status_inquiry",
                    escalate=False,
                    sentiment=ti_interpretation.sentiment,
                    urgency=ti_interpretation.urgency,
                    reason="resolution_report_received",
                    degraded=bool(s1_error),
                    latency_ms=ti_interpretation.latency_ms or ti_latency_ms,
                    details=details_ti,
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            # 5. Turn Act: Follow-up to active holding ticket (REQ-TURN-004 item 8, REQ-AGENT-033)
            if ti_interpretation.turn_act == "followup_to_active_ticket":
                held_ticket = (
                    conversation_state.active_tickets[0]
                    if (conversation_state and conversation_state.active_tickets)
                    else None
                )
                ref_id = (
                    held_ticket.issue_reference or held_ticket.ticket_uuid
                    if held_ticket
                    else t_id
                )
                holding_queue = (
                    held_ticket.owner.replace("queue:", "")
                    if (held_ticket and held_ticket.owner.startswith("queue:"))
                    else "human-standard"
                )
                return RouterDecision(
                    ticket_id=ref_id,
                    session_id=session_id,
                    decision="escalate",
                    tier="human_holding",
                    queue=holding_queue,
                    predicted_intent="followup",
                    escalate=True,
                    sentiment=ti_interpretation.sentiment,
                    urgency=ti_interpretation.urgency,
                    reason="human_ticket_followup",
                    degraded=bool(s1_error),
                    calming_ack=render_followup_calming_reply(ref_id),
                    latency_ms=ti_interpretation.latency_ms or ti_latency_ms,
                    details={
                        **details_ti,
                        "holding_ticket_id": ref_id,
                        "holding_ticket_uuid": held_ticket.ticket_uuid if held_ticket else ref_id,
                    },
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            # 4. Escalation Invariant (REQ-AGENT-003: Add-only escalation)
            s1_esc = (
                s1_pred is not None
                and isinstance(getattr(s1_pred, "escalate_prob", None), (int, float))
                and (
                    s1_pred.escalate_prob >= self.thresholds.tau_esc
                    or s1_pred.sentiment == "angry"
                    or s1_pred.urgency == "immediate"
                )
            )
            final_escalate = (
                ti_interpretation.escalate
                or s1_esc
                or ti_interpretation.sentiment == "angry"
                or ti_interpretation.urgency == "immediate"
            )

            if final_escalate:
                queue = evaluate_gaming_resistant_priority(
                    facts=verified_facts,
                    sentiment=ti_interpretation.sentiment,
                    urgency=ti_interpretation.urgency,
                )
                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="escalate",
                    tier="tier2_flash",
                    queue=queue.value,
                    predicted_intent=ti_interpretation.primary_intent,
                    escalate=True,
                    sentiment=ti_interpretation.sentiment,
                    urgency=ti_interpretation.urgency,
                    reason=ti_interpretation.escalation_reason or "tier2_flash_escalation",
                    degraded=bool(s1_error),
                    latency_ms=ti_interpretation.latency_ms or ti_latency_ms,
                    details=details_ti,
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            # 5. Out-of-Scope Queries
            if ti_interpretation.out_of_scope:
                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="reject_oos",
                    tier="tier2_flash",
                    queue="none",
                    predicted_intent="other",
                    escalate=False,
                    sentiment=ti_interpretation.sentiment,
                    urgency=ti_interpretation.urgency,
                    reason="tier2_out_of_scope",
                    degraded=False,
                    latency_ms=ti_interpretation.latency_ms or ti_latency_ms,
                    details={**details_ti, "out_of_scope": True},
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            # 6. Auto-RAG path
            return RouterDecision(
                ticket_id=t_id,
                session_id=session_id,
                decision="auto_rag",
                tier="tier2_flash",
                queue="none",
                predicted_intent=ti_interpretation.primary_intent,
                escalate=False,
                sentiment=ti_interpretation.sentiment,
                urgency=ti_interpretation.urgency,
                reason="tier2_flash_auto_rag",
                degraded=bool(s1_error),
                latency_ms=ti_interpretation.latency_ms or ti_latency_ms,
                details=details_ti,
                tokenized_text=tokenized_text,
                pii_mapping=pii_mapping,
            )

        # ---------------------------------------------------------------------
        # 4. Degraded Fallback Hierarchy (REQ-TURN-003 §7, REQ-AGENT-011)
        # ---------------------------------------------------------------------
        # A. Usable Laya encoder prediction
        has_usable_s1 = (
            s1_pred is not None
            and s1_pred.intent in _SELF_SERVICE_INTENTS
            and s1_pred.intent_prob >= self.thresholds.tau_usable_laya
            and s1_pred.escalate_prob < self.thresholds.tau_esc
            and s1_pred.sentiment != "angry"
            and s1_pred.urgency != "immediate"
        )
        if has_usable_s1:
            details_s1_deg: Dict[str, Any] = {
                "s1_error": s1_error,
                "turn_interpreter_error": ti_error,
                "turn_interpreter_timed_out": ti_timed_out,
                "t2_error": ti_error,
            }
            if s1_summary is not None:
                details_s1_deg["s1_prediction"] = s1_summary
            if ma_details:
                details_s1_deg["model_armor"] = ma_details
            if sanitized_text:
                details_s1_deg["sanitized_text"] = sanitized_text

            return RouterDecision(
                ticket_id=t_id,
                session_id=session_id,
                decision="auto_rag",
                tier="tier1_router_s1",
                queue="none",
                predicted_intent=s1_pred.intent,
                escalate=False,
                sentiment=s1_pred.sentiment,
                urgency=s1_pred.urgency,
                reason="tier2_unavailable_used_tier1",
                degraded=True,
                latency_ms=ti_latency_ms,
                details=details_s1_deg,
                tokenized_text=tokenized_text,
                pii_mapping=pii_mapping,
            )

        # B. Pending confirmation active -> Retain ticket, do NOT close!
        if has_pending:
            details_deg: Dict[str, Any] = {
                "s1_error": s1_error,
                "turn_interpreter_error": ti_error,
                "turn_interpreter_timed_out": ti_timed_out,
                "t2_error": ti_error,
            }
            if s1_summary is not None:
                details_deg["s1_prediction"] = s1_summary
            if ma_details:
                details_deg["model_armor"] = ma_details
            return RouterDecision(
                ticket_id=t_id,
                session_id=session_id,
                decision="auto_rag",
                tier="ticket_lifecycle",
                queue="none",
                predicted_intent="confirmation",
                escalate=False,
                sentiment="neutral",
                urgency="normal",
                reason="turn_interpreter_timeout_pending_confirmation_retained",
                calming_ack=(
                    "We received your message, but our automated confirmation service temporarily timed out. "
                    "Your ticket remains open. Please reply 'yes' again to close ticket or let us know if anything is unresolved."
                ),
                degraded=True,
                latency_ms=ti_latency_ms,
                details=details_deg,
                tokenized_text=tokenized_text,
                pii_mapping=pii_mapping,
            )

        # B2. Active human holding ticket in conversation_state -> Retain on existing ticket, NEVER create new ticket!
        if conversation_state and len(conversation_state.active_tickets) > 0:
            held_tk = conversation_state.active_tickets[0]
            ref_id = held_tk.issue_reference or held_tk.ticket_uuid
            holding_queue = (
                held_tk.owner.replace("queue:", "")
                if held_tk.owner.startswith("queue:")
                else "human-standard"
            )
            details_held: Dict[str, Any] = {
                "s1_error": s1_error,
                "turn_interpreter_error": ti_error,
                "turn_interpreter_timed_out": ti_timed_out,
                "t2_error": ti_error,
                "turn_act": "followup_to_active_ticket",
                "holding_ticket_id": ref_id,
                "holding_ticket_uuid": held_tk.ticket_uuid,
            }
            if s1_summary is not None:
                details_held["s1_prediction"] = s1_summary
            if ma_details:
                details_held["model_armor"] = ma_details
            if sanitized_text:
                details_held["sanitized_text"] = sanitized_text

            return RouterDecision(
                ticket_id=ref_id,
                session_id=session_id,
                decision="escalate",
                tier="human_holding",
                queue=holding_queue,
                predicted_intent="followup",
                escalate=True,
                sentiment="neutral",
                urgency="normal",
                reason="human_ticket_followup",
                calming_ack=render_followup_calming_reply(ref_id),
                degraded=True,
                latency_ms=ti_latency_ms,
                details=details_held,
                tokenized_text=tokenized_text,
                pii_mapping=pii_mapping,
            )

        # C. Total degradation -> Route to human queue, NEVER drop ticket!
        fallback_queue = evaluate_gaming_resistant_priority(facts=verified_facts)
        details_fb: Dict[str, Any] = {
            "s1_error": s1_error,
            "turn_interpreter_error": ti_error,
            "turn_interpreter_timed_out": ti_timed_out,
            "t2_error": ti_error,
        }
        if s1_summary is not None:
            details_fb["s1_prediction"] = s1_summary
        if ma_details:
            details_fb["model_armor"] = ma_details
        if sanitized_text:
            details_fb["sanitized_text"] = sanitized_text
        if prompt_safety_shadow:
            details_fb["prompt_safety_shadow"] = prompt_safety_shadow

        return RouterDecision(
            ticket_id=t_id,
            session_id=session_id,
            decision="escalate",
            tier="fallback_degraded",
            queue=fallback_queue.value,
            predicted_intent="other",
            escalate=True,
            sentiment="neutral",
            urgency="normal",
            reason=f"degraded_fallback: s1=({s1_error}), ti=({ti_error})",
            degraded=True,
            latency_ms=ti_latency_ms,
            details=details_fb,
            tokenized_text=tokenized_text,
            pii_mapping=pii_mapping,
        )
