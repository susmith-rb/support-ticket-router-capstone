"""Pure routing decision policy for TriageRouterAgent.

# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
# REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment
# REQ-AGENT-026: Deterministic Order-Lookup Intent Rule and Pleasantry Coverage
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from ticket_router.common.resilience import CircuitBreakerError
from ticket_router.models.router_s1_client import RouterPrediction, RouterS1Client
from ticket_router.security.model_armor import (
    ModelArmorClientProtocol,
    ModelArmorVerdict,
)
from ticket_router.security.pii_tokens import tokenize_pii

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
    """
    tau_esc: float = 0.50
    tau_low: float = 0.15
    tau_intent: float = 0.70

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
    """Deterministic regex / keyword rule evaluation result."""
    matched: bool = False
    escalate: bool = False
    reason: Optional[str] = None
    category: Optional[str] = None  # abuse, emergency, legal, human_request, greeting, pleasantry
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

# Standalone conversational greetings and capability openers (REQ-AGENT-024)
_GREETING_REGEX = re.compile(
    r"^(?:"
    r"(?:hi|hey|hello|howdy|hiya|greetings|good\s+(?:morning|afternoon|evening|day))"
    r"(?:\s+(?:there|team|support|assistant|folks|everyone|all))?"
    r"(?:[\s,!?.]+(?:how\s+are\s+you(?:\s+doing|\s+today)?|how['’]?s\s+it\s+going|how\s+can\s+you\s+help(?:\s+me)?|what\s+can\s+you\s+(?:do|help(?:\s+me)?\s+with)))?"
    r"|"
    r"(?:how\s+can\s+you\s+help(?:\s+me)?|what\s+can\s+you\s+(?:do|help(?:\s+me)?\s+with))"
    r")[\s!?.]*$",
    re.IGNORECASE,
)

# Standalone conversational pleasantries and polite thanks (REQ-AGENT-024, REQ-AGENT-026)
_PLEASANTRY_REGEX = re.compile(
    r"^(?:"
    r"(?:(?:ok|okay)[\s,]+)?(?:thanks|thank\s+you|thx|ty|many\s+thanks|much\s+appreciated)"
    r"(?:\s+(?:so\s+much|very\s+much|a\s+lot|a\s+bunch))?"
    r"(?:\s+for\s+(?:your|the)\s+(?:help|assistance|support|info|information))?"
    r"(?:\s+(?:team|support|assistant))?"
    r"|"
    r"(?:(?:thanks|thank\s+you)[\s,]+)?have\s+a\s+(?:great|good|nice|wonderful|lovely)\s+(?:day|evening|weekend)"
    r"|"
    r"(?:i\s+)?appreciate\s+(?:it|that|your\s+help|the\s+help)"
    r"|"
    r"cheers"
    r"|"
    r"much\s+obliged"
    r"|"
    r"that(?:'s|’s|\s+is)\s+(?:helpful|great|perfect|awesome)(?:[\s,]+(?:thanks|thank\s+you|thx|ty))"
    r"|"
    r"bye|goodbye|good\s+bye|bye\s+bye|see\s+you"
    r")[\s!?.]*$",
    re.IGNORECASE,
)


def evaluate_tier0_rules(text: str) -> Tier0Result:
    """Evaluate deterministic regex rules before invoking any neural model (REQ-AGENT-002).

    Guarantees:
    - Abuse/toxic phrases escalate immediately to human review and are NEVER dropped (REQ-AGENT-003).
    - Emergency/legal threats escalate immediately with immediate urgency.
    - Explicit human requests bypass automated answering.
    - Standalone greetings and pleasantries match without escalation (REQ-AGENT-024).
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

    cleaned = text.strip()
    # 4. Standalone conversational greeting (REQ-AGENT-024)
    if _GREETING_REGEX.match(cleaned):
        return Tier0Result(
            matched=True,
            escalate=False,
            reason="tier0_conversational_greeting",
            category="greeting",
            sentiment="neutral",
            urgency="normal",
        )

    # 5. Standalone conversational pleasantry (REQ-AGENT-024)
    if _PLEASANTRY_REGEX.match(cleaned):
        return Tier0Result(
            matched=True,
            escalate=False,
            reason="tier0_conversational_pleasantry",
            category="pleasantry",
            sentiment="positive",
            urgency="normal",
        )

    return Tier0Result(matched=False)


# -----------------------------------------------------------------------------
# Tier 1 Order-Lookup Deterministic Rule (REQ-AGENT-026)
# -----------------------------------------------------------------------------

# Words indicating complaints, distress, or non-order-status intents
_ORDER_LOOKUP_EXCLUDE_REGEX = re.compile(
    r"\b("
    r"refund\w*|"
    r"return\w*|"
    r"cancel\w*|"
    r"damaged?|"
    r"broken|"
    r"wrong|"
    r"missing|"
    r"late|"
    r"never\s+arrived|"
    r"angry|"
    r"ridiculous|"
    r"scam\w*|"
    r"lawyer\w*|"
    r"attorney\w*|"
    r"help\s+me|"
    r"urgent\w*|"
    r"asap"
    r")\b|!{2,}",
    re.IGNORECASE,
)

# Anchored regex for calm own-order history and order-ID lookups (REQ-AGENT-026)
_ORDER_LOOKUP_REGEX = re.compile(
    r"^(?!.*(?:\b(?:refund\w*|return\w*|cancel\w*|damaged?|broken|wrong|missing|late|never\s+arrived|angry|ridiculous|scam\w*|lawyer\w*|attorney\w*|help\s+me|urgent\w*|asap)\b|!{2,}))(?:"
    # own-order history
    r"what(?:\s+are|'re)\s+my\s+(?:recent\s+)?orders(?:\s+recently)?"
    r"|"
    r"what\s+(?:did|have)\s+i\s+(?:buy|bought|order|ordered|purchase|purchased)(?:\s+(?:recently|lately))?"
    r"|"
    r"(?:(?:can|could)\s+you\s+)?(?:show|list|give\s+me|give|get|check|see|view)(?:\s+me)?\s+my\s+(?:recent\s+|latest\s+|past\s+)?(?:orders|purchases|order\s+history)(?:\s+(?:from|in)\s+the\s+(?:last|past)\s+(?:week|month|\d+\s+days))?"
    r"|"
    r"my\s+(?:recent\s+|latest\s+|past\s+)?(?:orders|purchases|order\s+history)"
    r"|"
    r"(?:check|see|view)\s+my\s+(?:recent\s+)?orders"
    r"|"
    r"order\s+history"
    r"|"
    # order-ID lookups
    r"(?:where\s+is|where['’]?s)\s+(?:(?:my|the)\s+order\s+)?ORD-\d{3,}"
    r"|"
    r"(?:(?:what['’]?s|what\s+is)\s+the\s+)?status\s+of\s+(?:(?:my|the)\s+order\s+)?ORD-\d{3,}"
    r"|"
    r"(?:(?:can|could)\s+you\s+)?(?:track|tracking|locate|find|check)(?:\s+(?:the\s+)?status\s+of)?\s+(?:(?:my|the)\s+order\s+)?ORD-\d{3,}"
    r"|"
    r"(?:(?:my|the)\s+order\s+)?ORD-\d{3,}\s+status"
    r"|"
    r"status\s+(?:(?:my|the)\s+order\s+)?ORD-\d{3,}"
    r")[\s!?.]*$",
    re.IGNORECASE,
)


def matches_order_lookup(text: str) -> bool:
    """Return True if text matches calm order-lookup intent rule (REQ-AGENT-026)."""
    if not text:
        return False
    cleaned = text.strip()
    if _ORDER_LOOKUP_EXCLUDE_REGEX.search(cleaned):
        return False
    return bool(_ORDER_LOOKUP_REGEX.match(cleaned))


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
        model_armor_client: Optional[ModelArmorClientProtocol] = None,
    ) -> None:
        self.thresholds = thresholds or RouterThresholds()
        self.s1_client = s1_client
        self.tier2_invoker = tier2_invoker
        self.model_armor_client = model_armor_client

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
        """Execute Model Armor sanitization and PII tokenization safely.

        Never raises: on client failure returns a degraded verdict (fail-safe gate).
        Returns:
            (ma_verdict, sanitized_text, tokenized_text, pii_mapping)
        """
        if self.model_armor_client is None:
            return None, None, None, {}

        try:
            ma_verdict = self.model_armor_client.sanitize_user_prompt(text)
        except Exception as exc:
            # Fail safe: surface a degraded verdict so the REQ-MA-005 degraded gate
            # still applies (never silently skip Model Armor on the non-tier0 path).
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
                # Ingress guard: Card data must never be tokenized, mapped, or persisted (TDD §4.2 C4)
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
        session_id: str,
        customer_id: str,
        verified_facts: Optional[VerifiedFacts] = None,
        ticket_id: Optional[str] = None,
    ) -> RouterDecision:
        """Execute tiered routing policy on inbound message text."""
        t_id = ticket_id or f"TICK-{session_id[:8]}"

        # Tier 0: Deterministic Safety, Abuse & Keyword Rules (REQ-AGENT-002) (<1 ms)
        tier0 = evaluate_tier0_rules(text)
        if tier0.matched and tier0.escalate:
            queue = evaluate_gaming_resistant_priority(
                facts=verified_facts,
                tier0=tier0,
                sentiment=tier0.sentiment,
                urgency=tier0.urgency,
            )
            details_t0: Dict[str, Any] = {"category": tier0.category}
            tokenized_text: Optional[str] = None
            pii_mapping: Dict[str, str] = {}
            if self.model_armor_client is not None:
                ma_verdict, sanitized_text, tokenized_text, pii_mapping = self._run_model_armor(text)
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
                        sentiment=tier0.sentiment,
                        urgency=tier0.urgency,
                        reason="card_data_blocked",
                        degraded=False,
                        details={"blocked_info_types": blocked_types},
                        tokenized_text=None,
                        pii_mapping={},
                    )
                if sanitized_text:
                    details_t0["sanitized_text"] = sanitized_text

            return RouterDecision(
                ticket_id=t_id,
                session_id=session_id,
                decision="escalate",
                tier="tier0_rules",
                queue=queue.value,
                predicted_intent="other",
                escalate=True,
                sentiment=tier0.sentiment,
                urgency=tier0.urgency,
                reason=tier0.reason or "tier0_rule_match",
                degraded=False,
                details=details_t0,
                tokenized_text=tokenized_text,
                pii_mapping=pii_mapping,
            )

        if tier0.matched and not tier0.escalate and tier0.category in ("greeting", "pleasantry"):
            return RouterDecision(
                ticket_id=t_id,
                session_id=session_id,
                decision="auto_rag",
                tier="tier0_rules",
                queue="none",
                predicted_intent="other",
                escalate=False,
                sentiment=tier0.sentiment,
                urgency=tier0.urgency,
                reason=tier0.reason or f"tier0_conversational_{tier0.category}",
                degraded=False,
                details={"category": tier0.category},
            )

        ma_verdict: Optional[ModelArmorVerdict] = None
        s1_pred: Optional[RouterPrediction] = None
        s1_error: Optional[str] = None
        sanitized_text: Optional[str] = None
        tokenized_text: Optional[str] = None
        pii_mapping: Dict[str, str] = {}

        # ---------------------------------------------------------------------
        # Tier 1 & Model Armor: Concurrent Execution (REQ-MA-003, REQ-MA-005)
        # ---------------------------------------------------------------------
        if self.model_armor_client is not None:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                ma_future = executor.submit(self._run_model_armor, text)
                s1_future = executor.submit(self._call_s1, text)
                ma_verdict, sanitized_text, tokenized_text, pii_mapping = ma_future.result()
                s1_pred, s1_error = s1_future.result()
        else:
            s1_pred, s1_error = self._call_s1(text)

        # ---------------------------------------------------------------------
        # Model Armor Security Gate (Pre-LLM / Pre-A2A) (REQ-MA-003)
        # ---------------------------------------------------------------------
        ma_details: Dict[str, Any] = {}
        if ma_verdict is not None:
            ma_details = ma_verdict.to_summary_dict()

            # 0. Payment Card Data -> Ingress Guard Hard Block (TDD §4.2 C4, REQ-MA-007)
            # Card data is blocked at ingress with HTTP 400 and never persisted.
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

            # 1. Prompt Injection, Jailbreak, or Malicious URI -> Hard Security Escalation
            # NEVER forwarded to neural LLM tiers or FaqResolverAgent.
            # Queue comes from verified facts only: an injection attempt must
            # never buy a faster lane (priority-gaming invariant).
            if ma_verdict.has_pi_jb or ma_verdict.has_malicious_uri:
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

            # 2. Responsible AI (RAI) Harassment / Abuse -> Boundary Ack + Human Review
            # NEVER dropped, does NOT raise priority (REQ-AGENT-004).
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

            # 3. Model Armor Degraded / Timeout -> Fail-safe to human-standard (never drop)
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

        # If router-s1 succeeded, inspect prediction against operating thresholds
        if s1_pred is not None:
            # 1. Definitive escalation: p_esc >= tau_esc (0.50), angry, or immediate
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
                details_s1: Dict[str, Any] = {
                    "escalate_prob": s1_pred.escalate_prob,
                    "sentiment_prob": s1_pred.sentiment_prob,
                    "intent_prob": s1_pred.intent_prob,
                }
                if ma_details:
                    details_s1["model_armor"] = ma_details
                if sanitized_text:
                    details_s1["sanitized_text"] = sanitized_text

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
                    details=details_s1,
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            # 2. Definitive auto-RAG: p_esc < tau_low (0.15) and p_intent >= tau_intent (0.70)
            is_definitive_rag = (
                s1_pred.escalate_prob < self.thresholds.tau_low
                and s1_pred.intent_prob >= self.thresholds.tau_intent
                and s1_pred.intent != "other"
            )

            if is_definitive_rag:
                details_rag: Dict[str, Any] = {
                    "escalate_prob": s1_pred.escalate_prob,
                    "intent_prob": s1_pred.intent_prob,
                }
                if ma_details:
                    details_rag["model_armor"] = ma_details
                if sanitized_text:
                    details_rag["sanitized_text"] = sanitized_text

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
                    details=details_rag,
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            # 3. Deterministic order-lookup intent rule (REQ-AGENT-026)
            if matches_order_lookup(text):
                details_order: Dict[str, Any] = {
                    "category": "order_lookup",
                    "escalate_prob": s1_pred.escalate_prob,
                    "intent_prob": s1_pred.intent_prob,
                }
                if ma_details:
                    details_order["model_armor"] = ma_details
                if sanitized_text:
                    details_order["sanitized_text"] = sanitized_text

                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="auto_rag",
                    tier="tier1_router_s1",
                    queue="none",
                    predicted_intent="order_status",
                    escalate=False,
                    sentiment=s1_pred.sentiment,
                    urgency=s1_pred.urgency,
                    reason="tier1_order_lookup_rule",
                    degraded=False,
                    latency_ms=s1_pred.latency_ms,
                    details=details_order,
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

        # ---------------------------------------------------------------------
        # Tier 2: Gemini Flash (Uncertainty Band or S1 Fallback)
        # ---------------------------------------------------------------------
        # Invoked if:
        # - s1 failed / breaker open
        # - s1 confidence in uncertainty band: tau_low <= p_esc < tau_esc (0.15 <= p_esc < 0.50)
        # - intent == "other" or intent_prob < tau_intent (0.70)
        t2_pred: Optional[RouterPrediction] = None
        t2_error: Optional[str] = None

        if self.tier2_invoker is not None:
            try:
                text_for_t2 = tokenized_text if tokenized_text is not None else text
                t2_pred = self.tier2_invoker(text_for_t2)
            except Exception as exc:
                t2_error = f"tier2_error: {exc}"

        if t2_pred is not None:
            # INVARIANT: Add-only escalation (REQ-AGENT-003)
            # final.escalate = rules OR tier1 OR tier2
            t1_esc = (
                (
                    s1_pred.escalate_prob >= self.thresholds.tau_esc
                    or s1_pred.sentiment == "angry"
                    or s1_pred.urgency == "immediate"
                )
                if s1_pred
                else False
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
                details_t2: Dict[str, Any] = {"s1_error": s1_error}
                if ma_details:
                    details_t2["model_armor"] = ma_details
                if sanitized_text:
                    details_t2["sanitized_text"] = sanitized_text

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
                    details=details_t2,
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            if t2_pred.out_of_scope:
                details_t2_oos: Dict[str, Any] = {
                    "s1_error": s1_error,
                    "out_of_scope": True,
                }
                if ma_details:
                    details_t2_oos["model_armor"] = ma_details
                if sanitized_text:
                    details_t2_oos["sanitized_text"] = sanitized_text

                return RouterDecision(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision="reject_oos",
                    tier="tier2_flash",
                    queue="none",
                    predicted_intent="other",
                    escalate=False,
                    sentiment=t2_pred.sentiment,
                    urgency=t2_pred.urgency,
                    reason="tier2_out_of_scope",
                    degraded=bool(s1_error),
                    latency_ms=t2_pred.latency_ms,
                    details=details_t2_oos,
                    tokenized_text=tokenized_text,
                    pii_mapping=pii_mapping,
                )

            details_t2_rag: Dict[str, Any] = {"s1_error": s1_error}
            if ma_details:
                details_t2_rag["model_armor"] = ma_details
            if sanitized_text:
                details_t2_rag["sanitized_text"] = sanitized_text

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
                details=details_t2_rag,
                tokenized_text=tokenized_text,
                pii_mapping=pii_mapping,
            )

        # ---------------------------------------------------------------------
        # Fallback Hierarchy: Total Degradation (REQ-AGENT-011)
        # ---------------------------------------------------------------------
        # Both Tier 1 and Tier 2 unavailable: route to human queue, NEVER drop ticket!
        fallback_queue = evaluate_gaming_resistant_priority(facts=verified_facts, tier0=tier0)
        details_fb: Dict[str, Any] = {"s1_error": s1_error, "t2_error": t2_error}
        if ma_details:
            details_fb["model_armor"] = ma_details
        if sanitized_text:
            details_fb["sanitized_text"] = sanitized_text

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
            reason=f"degraded_fallback: s1=({s1_error}), t2=({t2_error})",
            degraded=True,
            details=details_fb,
            tokenized_text=tokenized_text,
            pii_mapping=pii_mapping,
        )
