"""Pure routing decision policy for TriageRouterAgent.

# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
# REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional

from ticket_router.common.resilience import CircuitBreakerError
from ticket_router.models.router_s1_client import RouterPrediction, RouterS1Client


class QueuePriority(str, Enum):
    """Operational queue priority and target routing queues (REQ-AGENT-004)."""
    P1_URGENT = "human-urgent"
    P2_STANDARD = "human-standard"
    P3_LOW = "human-low"


@dataclass(frozen=True)
class RouterThresholds:
    """Configurable routing policy thresholds (REQ-AGENT-002).

    Operating points per specs/agent-service.md:
    tau_esc = 0.35: Definitive escalation floor
    tau_low = 0.15: Auto-RAG uncertainty lower bound
    tau_intent = 0.70: Intent confidence floor
    """
    tau_esc: float = 0.35
    tau_low: float = 0.15
    tau_intent: float = 0.70


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
    """Deterministic regex / keyword rule evaluation result."""
    matched: bool = False
    escalate: bool = False
    reason: Optional[str] = None
    category: Optional[str] = None  # abuse, emergency, legal, human_request
    sentiment: str = "neutral"
    urgency: str = "normal"


@dataclass(frozen=True)
class RouterDecision:
    """Typed decision returned by TriageRouter policy evaluation."""
    ticket_id: str
    session_id: str
    decision: str  # "auto_rag" or "escalate"
    tier: str      # "tier0_rules", "tier1_router_s1", "tier2_flash", "fallback_degraded"
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


# -----------------------------------------------------------------------------
# Tier 0 Deterministic Rules (REQ-AGENT-002, REQ-AGENT-003)
# -----------------------------------------------------------------------------

# Emergency & Legal triggers
_EMERGENCY_REGEX = re.compile(
    r"\b(suicide|self-harm|kill myself|end my life|weapon|bomb|police|lawyer|attorney|lawsuit|subpoena|court|sue you)\b",
    re.IGNORECASE,
)

# Abuse / Toxicity / Profanity triggers (REQ-AGENT-003: must escalate to human, never drop)
_ABUSE_REGEX = re.compile(
    r"\b(fuck\w*|shit\w*|bitch\w*|bastard\w*|asshole\w*|cunt\w*|dick\w*|idiot\w*|moron\w*|scam\w*|burn in hell|go die\w*|kill yourself)\b",
    re.IGNORECASE,
)

# Explicit human representative request
_HUMAN_REQUEST_REGEX = re.compile(
    r"\b(speak to a human|talk to a human|speak with human|real person|human agent|operator|representative|manager)\b",
    re.IGNORECASE,
)


def evaluate_tier0_rules(text: str) -> Tier0Result:
    """Evaluate deterministic regex rules before invoking any neural model (REQ-AGENT-002).

    Guarantees:
    - Abuse/toxic phrases escalate immediately to human review and are NEVER dropped (REQ-AGENT-003).
    - Emergency/legal threats escalate immediately with immediate urgency.
    - Explicit human requests bypass automated answering.
    """
    if not text or not text.strip():
        return Tier0Result(matched=False)

    # 1. Emergency or legal threat
    match_em = _EMERGENCY_REGEX.search(text)
    if match_em:
        term = match_em.group(0).lower()
        cat = "legal" if term in ("lawyer", "attorney", "lawsuit", "subpoena", "court", "sue you") else "emergency"
        return Tier0Result(
            matched=True,
            escalate=True,
            reason=f"tier0_{cat}_trigger",
            category=cat,
            sentiment="angry" if cat == "legal" else "neutral",
            urgency="immediate",
        )

    # 2. Abuse / Toxicity (REQ-AGENT-003)
    if _ABUSE_REGEX.search(text):
        return Tier0Result(
            matched=True,
            escalate=True,
            reason="tier0_abusive_language",
            category="abuse",
            sentiment="angry",
            urgency="normal",
        )

    # 3. Direct human request
    if _HUMAN_REQUEST_REGEX.search(text):
        return Tier0Result(
            matched=True,
            escalate=True,
            reason="tier0_human_agent_request",
            category="human_request",
            sentiment="neutral",
            urgency="normal",
        )

    return Tier0Result(matched=False)


# -----------------------------------------------------------------------------
# Gaming-Resistant Priority Assignment (REQ-AGENT-004)
# -----------------------------------------------------------------------------


def evaluate_gaming_resistant_priority(
    facts: Optional[VerifiedFacts] = None,
    tier0: Optional[Tier0Result] = None,
    sentiment: str = "neutral",
    urgency: str = "normal",
) -> QueuePriority:
    """Assign human review queue priority based strictly on verified business facts.

    REQ-AGENT-004:
    - Expressed customer anger or abusive sentiment MUST bypass automated self-service
      into human review queues, but MUST NOT artificially inflate operational ticket priority
      (e.g. promoting a normal inquiry to Urgent/P1).
    - Queue priority is determined strictly from verified business facts (delivery delay
      exceeding SLA, monetary transaction value, stranded returns, or VIP account status).
    """
    # 1. Emergency safety/legal concerns always warrant immediate human attention
    if tier0 and tier0.category in ("emergency", "legal"):
        return QueuePriority.P1_URGENT

    # 2. Verified operational facts warranting P1 Urgent
    if facts:
        # High financial transaction value ($100+ threshold)
        if facts.order_value >= 100.0:
            return QueuePriority.P1_URGENT
        # Verified delivery delay exceeding 24h SLA
        if facts.delivery_delay_hours >= 24.0:
            return QueuePriority.P1_URGENT
        # Stranded physical return or VIP tier status
        if facts.stranded_return or facts.vip_customer:
            return QueuePriority.P1_URGENT
        # Repeat customer contact on unresolved inquiry
        if facts.repeat_contact:
            return QueuePriority.P1_URGENT

    # 3. Customer anger/abuse alone without verified facts routes to Standard (P2)
    # Shouting, toxic language, or self-reported urgency cannot game P1 priority.
    return QueuePriority.P2_STANDARD


# -----------------------------------------------------------------------------
# Triage Policy Orchestration (REQ-AGENT-002, REQ-AGENT-003, REQ-AGENT-005)
# -----------------------------------------------------------------------------


class TriagePolicy:
    """Orchestrates Tier 0 deterministic rules, Tier 1 router-s1, and Tier 2 Flash.

    Guarantees:
    - Add-only escalation: final.escalate = t0 or t1 or t2 (REQ-AGENT-003).
    - Sub-100 ms routing for definitive cases; Tier 2 only in the uncertainty band (REQ-AGENT-002).
    - Gaming-resistant queue priority based on verified facts (REQ-AGENT-004).
    - Fallback to Tier 2 if router-s1 is degraded/open, and fallback to human if Tier 2 fails (REQ-AGENT-011).
    """

    def __init__(
        self,
        thresholds: Optional[RouterThresholds] = None,
        s1_client: Optional[RouterS1Client] = None,
        tier2_invoker: Optional[Callable[[str], RouterPrediction]] = None,
    ) -> None:
        self.thresholds = thresholds or RouterThresholds()
        self.s1_client = s1_client
        self.tier2_invoker = tier2_invoker

    def evaluate(
        self,
        text: str,
        session_id: str,
        customer_id: str,
        verified_facts: Optional[VerifiedFacts] = None,
        ticket_id: Optional[str] = None,
    ) -> RouterDecision:
        """Execute tiered routing policy on inbound message text."""
        t_id = ticket_id or f"TICK-{session_id[:8]}"

        # ---------------------------------------------------------------------
        # Tier 0: Deterministic Safety, Abuse & Keyword Rules (REQ-AGENT-002)
        # ---------------------------------------------------------------------
        tier0 = evaluate_tier0_rules(text)
        if tier0.matched and tier0.escalate:
            queue = evaluate_gaming_resistant_priority(
                facts=verified_facts,
                tier0=tier0,
                sentiment=tier0.sentiment,
                urgency=tier0.urgency,
            )
            return RouterDecision(
                ticket_id=t_id,
                session_id=session_id,
                decision="escalate",
                tier="tier0_rules",
                queue=queue.value,
                predicted_intent="human_escalation",
                escalate=True,
                sentiment=tier0.sentiment,
                urgency=tier0.urgency,
                reason=tier0.reason or "tier0_rule_match",
                degraded=False,
                details={"category": tier0.category},
            )

        # ---------------------------------------------------------------------
        # Tier 1: Router-S1 Encoder (REQ-AGENT-002)
        # ---------------------------------------------------------------------
        s1_pred: Optional[RouterPrediction] = None
        s1_error: Optional[str] = None

        if self.s1_client is not None:
            try:
                s1_pred = self.s1_client.route(text)
            except CircuitBreakerError as cb_exc:
                s1_error = f"circuit_breaker_open: {cb_exc}"
            except Exception as exc:
                s1_error = f"endpoint_error: {exc}"

        # If router-s1 succeeded, inspect prediction against operating thresholds
        if s1_pred is not None:
            # 1. Definitive escalation: p_esc >= tau_esc (0.35), angry, or immediate
            is_definitive_escalate = (
                s1_pred.escalate_prob >= self.thresholds.tau_esc
                or s1_pred.sentiment == "angry"
                or s1_pred.urgency == "immediate"
            )

            if is_definitive_escalate:
                queue = evaluate_gaming_resistant_priority(
                    facts=verified_facts,
                    tier0=tier0,
                    sentiment=s1_pred.sentiment,
                    urgency=s1_pred.urgency,
                )
                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="escalate",
                    tier="tier1_router_s1",
                    queue=queue.value,
                    predicted_intent=s1_pred.intent,
                    escalate=True,
                    sentiment=s1_pred.sentiment,
                    urgency=s1_pred.urgency,
                    reason="tier1_definitive_escalation",
                    degraded=False,
                    latency_ms=s1_pred.latency_ms,
                    details={
                        "escalate_prob": s1_pred.escalate_prob,
                        "sentiment_prob": s1_pred.sentiment_prob,
                        "intent_prob": s1_pred.intent_prob,
                    },
                )

            # 2. Definitive auto-RAG: p_esc < tau_low (0.15) and p_intent >= tau_intent (0.70)
            is_definitive_rag = (
                s1_pred.escalate_prob < self.thresholds.tau_low
                and s1_pred.intent_prob >= self.thresholds.tau_intent
                and s1_pred.intent != "other"
            )

            if is_definitive_rag:
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
                    reason="tier1_definitive_auto_rag",
                    degraded=False,
                    latency_ms=s1_pred.latency_ms,
                    details={
                        "escalate_prob": s1_pred.escalate_prob,
                        "intent_prob": s1_pred.intent_prob,
                    },
                )

        # ---------------------------------------------------------------------
        # Tier 2: Gemini Flash (Uncertainty Band or S1 Fallback)
        # ---------------------------------------------------------------------
        # Invoked if:
        # - s1 failed / breaker open
        # - s1 confidence in uncertainty band: tau_low <= p_esc < tau_esc (0.15 <= p_esc < 0.35)
        # - intent == "other" or intent_prob < tau_intent (0.70)
        t2_pred: Optional[RouterPrediction] = None
        t2_error: Optional[str] = None

        if self.tier2_invoker is not None:
            try:
                t2_pred = self.tier2_invoker(text)
            except Exception as exc:
                t2_error = f"tier2_error: {exc}"

        if t2_pred is not None:
            # INVARIANT: Add-only escalation (REQ-AGENT-003)
            # final.escalate = rules OR tier1 OR tier2
            t1_esc = (s1_pred.escalate if s1_pred else False) or (
                s1_pred.escalate_prob >= self.thresholds.tau_esc if s1_pred else False
            )
            final_escalate = (
                tier0.escalate
                or t1_esc
                or t2_pred.escalate
                or t2_pred.sentiment == "angry"
                or t2_pred.urgency == "immediate"
            )

            if final_escalate:
                queue = evaluate_gaming_resistant_priority(
                    facts=verified_facts,
                    tier0=tier0,
                    sentiment=t2_pred.sentiment,
                    urgency=t2_pred.urgency,
                )
                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="escalate",
                    tier="tier2_flash",
                    queue=queue.value,
                    predicted_intent=t2_pred.intent,
                    escalate=True,
                    sentiment=t2_pred.sentiment,
                    urgency=t2_pred.urgency,
                    reason="tier2_flash_escalation",
                    degraded=bool(s1_error),
                    latency_ms=t2_pred.latency_ms,
                    details={"s1_error": s1_error},
                )

            return RouterDecision(
                ticket_id=t_id,
                session_id=session_id,
                decision="auto_rag",
                tier="tier2_flash",
                queue="none",
                predicted_intent=t2_pred.intent,
                escalate=False,
                sentiment=t2_pred.sentiment,
                urgency=t2_pred.urgency,
                reason="tier2_flash_auto_rag",
                degraded=bool(s1_error),
                latency_ms=t2_pred.latency_ms,
                details={"s1_error": s1_error},
            )

        # ---------------------------------------------------------------------
        # Fallback Hierarchy: Total Degradation (REQ-AGENT-011)
        # ---------------------------------------------------------------------
        # Both Tier 1 and Tier 2 unavailable: route to human queue, NEVER drop ticket!
        fallback_queue = evaluate_gaming_resistant_priority(facts=verified_facts, tier0=tier0)
        return RouterDecision(
            ticket_id=t_id,
            session_id=session_id,
            decision="escalate",
            tier="fallback_degraded",
            queue=fallback_queue.value,
            predicted_intent="unknown_degraded",
            escalate=True,
            sentiment="neutral",
            urgency="normal",
            reason=f"degraded_fallback: s1=({s1_error}), t2=({t2_error})",
            degraded=True,
            details={"s1_error": s1_error, "t2_error": t2_error},
        )
