"""pipeline_helpers.py - Allowed enums, span attribute recorders, and safe parsing helpers.

REQ-AGENT-038, REQ-IDENT-007, REQ-MA-003, REQ-AGENT-033, REQ-SAFE-004.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any, Dict, Optional

from ticket_router.agents.triage_router.policy import RouterDecision
from ticket_router.security.model_armor import ModelArmorVerdict

logger = logging.getLogger(__name__)

_SAFE_INTENTS = frozenset({
    "order_status",
    "shipping_policy",
    "returns_refunds",
    "account_payment",
    "other",
    "faq",
    "billing",
    "shipping",
    "refund",
    "technical",
    "status_inquiry",
    "confirmation",
    "followup",
    "conversation_history_access",
    "cross_customer_data_request",
    "authorization_bypass_attempt",
})
_SAFE_SENTIMENTS = frozenset({"angry", "frustrated", "neutral", "positive"})
_SAFE_URGENCIES = frozenset({"can wait days", "within a day", "within hours", "immediate", "normal"})
_SAFE_DECISIONS = frozenset({"escalate", "auto_rag", "reject_oos", "blocked", "security_blocked"})
_SAFE_HANDOFF_STATUSES = frozenset({
    "NONE",
    "AUTO_RESOLVED",
    "ESCALATED",
    "BLOCKED",
    "SECURITY_BLOCKED",
    "STATUS_INQUIRY",
    "FORBIDDEN",
    "RESOLVED",
})
_SAFE_TOKEN_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
_SAFE_REASON_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")


def _clamp_prob(val: Any) -> float:
    """Safely clamp probability float to [0.0, 1.0] rounded to 4 decimals."""
    try:
        f = float(val)
        if f != f or f == float("inf") or f == float("-inf"):
            return 0.0
        return round(max(0.0, min(1.0, f)), 4)
    except (ValueError, TypeError):
        return 0.0


def _safe_enum(val: Any, allowed: frozenset[str], default: str = "unrecognized") -> str:
    """Validate string against allowlist returning normalized value or default."""
    if val is None:
        return default
    s = str(val).strip().lower()
    return s if s in allowed else default


def _safe_reason_code(reason: Any) -> str:
    """Extract canonical reason prefix before ':' ensuring zero exception message leakage."""
    prefix = str(reason or "").split(":", 1)[0].strip().lower()
    return prefix if _SAFE_TOKEN_RE.match(prefix) else "unspecified"


def _safe_identifier_token(val: Any, default: str = "none") -> str:
    """Validate identifier token against safe regex returning normalized string or default."""
    s = str(val or "").strip().lower()
    return s if _SAFE_TOKEN_RE.match(s) else default


def _record_turn_final_span_attributes(
    turn_span: Any,
    decision: RouterDecision,
    handoff_resp: Optional[Dict[str, Any]] = None,
) -> None:
    """Record allowlisted routing decision and S1 attributes on turn_span (REQ-AGENT-038)."""
    if turn_span is None:
        return

    turn_span.set_attribute(
        "decision", _safe_enum(decision.decision, _SAFE_DECISIONS, default="escalate")
    )
    turn_span.set_attribute("tier", _safe_identifier_token(decision.tier, default="unknown"))
    turn_span.set_attribute("queue", _safe_identifier_token(decision.queue, default="none"))
    turn_span.set_attribute("degraded", bool(decision.degraded))
    turn_span.set_attribute(
        "predicted_intent", _safe_enum(decision.predicted_intent, _SAFE_INTENTS, default="other")
    )

    if isinstance(decision.details, dict) and isinstance(decision.details.get("s1_prediction"), dict):
        s1_info = decision.details["s1_prediction"]
        turn_span.set_attribute("s1.intent", _safe_enum(s1_info.get("intent"), _SAFE_INTENTS))
        turn_span.set_attribute("s1.intent_prob", _clamp_prob(s1_info.get("intent_prob")))
        turn_span.set_attribute("s1.escalate_prob", _clamp_prob(s1_info.get("escalate_prob")))

    handoff_status = "NONE"
    if isinstance(handoff_resp, dict) and handoff_resp.get("status"):
        st = str(handoff_resp["status"]).strip().upper()
        handoff_status = st if st in _SAFE_HANDOFF_STATUSES else "NONE"
    elif decision.decision == "escalate":
        handoff_status = "ESCALATED"
    elif decision.decision == "reject_oos":
        handoff_status = "AUTO_RESOLVED"

    if decision.decision == "auto_rag" and handoff_status == "ESCALATED":
        effective_decision = "escalate"
        effective_escalate = True
        effective_queue = _safe_identifier_token(
            handoff_resp.get("queue") if isinstance(handoff_resp, dict) else None,
            default="human-standard",
        )
        effective_reason = _safe_reason_code(
            (handoff_resp.get("reason") if isinstance(handoff_resp, dict) else None)
            or decision.reason
        )
    else:
        effective_decision = _safe_enum(decision.decision, _SAFE_DECISIONS, default="escalate")
        effective_escalate = bool(decision.escalate)
        effective_queue = _safe_identifier_token(decision.queue, default="none")
        effective_reason = _safe_reason_code(decision.reason)

    effective_degraded = bool(decision.degraded)
    if isinstance(handoff_resp, dict) and handoff_resp.get("degraded") is not None:
        effective_degraded = effective_degraded or bool(handoff_resp.get("degraded"))

    final_payload = {
        "decision": effective_decision,
        "degraded": effective_degraded,
        "escalate": bool(effective_escalate),
        "handoff_status": handoff_status,
        "intent": _safe_enum(decision.predicted_intent, _SAFE_INTENTS),
        "queue": effective_queue,
        "reason": effective_reason,
        "tier": _safe_identifier_token(decision.tier, default="unknown"),
    }
    turn_span.set_attribute(
        "router.final_decision",
        json.dumps(final_payload, sort_keys=True, separators=(",", ":")),
    )


def derive_channel_source(
    channel: Optional[str] = None,
    oauth_access_token: Optional[str] = None,
    caller_user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Derive channel source string for audit logging (REQ-IDENT-007)."""
    if channel in ("gemini_enterprise", "direct_api"):
        return channel
    if oauth_access_token:
        return "gemini_enterprise"

    uid = (caller_user_id or "").lower()
    sid = (session_id or "").lower()
    ch = (channel or "").lower()

    if any(k in uid or k in sid for k in ("ge-", "gemini", "workplace")):
        return "gemini_enterprise"
    if uid == "smoke-client" or uid.startswith("api-") or sid.startswith("smoke-ingress-"):
        return "direct_api"
    if ch in ("api", "rest", "direct_api"):
        return "direct_api"
    return "unknown"


def _build_detection_reason(ma_verdict: Optional[ModelArmorVerdict]) -> Dict[str, Any]:
    """Extract matched filters and confidence levels without customer text or PII."""
    reason: Dict[str, Any] = {}
    if ma_verdict is None:
        return {"matched_filters": ["prompt_injection_or_jailbreak"], "confidence": "HIGH"}
    if ma_verdict.pi_jb and ma_verdict.pi_jb.tripped:
        reason["pi_and_jailbreak"] = {
            "tripped": True,
            "confidence": ma_verdict.pi_jb.confidence,
        }
    if ma_verdict.malicious_uri and ma_verdict.malicious_uri.tripped:
        reason["malicious_uris"] = {
            "tripped": True,
            "confidence": ma_verdict.malicious_uri.confidence,
        }
    for cat, fv in ma_verdict.rai_categories.items():
        if fv.tripped:
            reason[f"rai_{cat}"] = {
                "tripped": True,
                "confidence": fv.confidence,
            }
    if not reason:
        reason = {"matched_filters": ["prompt_injection_or_jailbreak"], "confidence": "HIGH"}
    return reason


def _emit_security_blocked_log(
    *,
    traceparent: str,
    session_id: str,
    channel: str,
    customer_verified: bool,
    customer_id: Optional[str],
    ma_verdict: Optional[ModelArmorVerdict],
    message_text: str,
) -> None:
    """Emit structured WARNING log for prompt injection / jailbreak security refusal (REQ-MA-003)."""
    parts = traceparent.split("-")
    trace_id = parts[1] if len(parts) >= 2 else ""
    cid = (
        customer_id
        if (customer_verified and customer_id and not str(customer_id).startswith("anon:"))
        else "anon"
    )
    msg_hash = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
    detection_reason = _build_detection_reason(ma_verdict)

    log_payload = {
        "event": "security_blocked",
        "severity": "WARNING",
        "traceparent": traceparent,
        "trace_id": trace_id,
        "session_id": session_id,
        "channel": channel,
        "customer_verified": bool(customer_verified),
        "customer_id": cid,
        "detection_reason": detection_reason,
        "message_sha256": msg_hash,
        "message_length": len(message_text),
    }
    logger.warning(json.dumps(log_payload, separators=(",", ":")))


def _extract_ticket_id_reference(text: str) -> Optional[str]:
    """Extract explicit TICK-xxxxxxxx reference from text if present."""
    match = re.search(r"\b(TICK-[A-Za-z0-9_-]+)\b", text, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return None


def _is_explicit_ticket_reference(text: str) -> bool:
    """Check if message explicitly references a ticket or specialist inquiry."""
    if _extract_ticket_id_reference(text):
        return True
    t = text.lower()
    return any(
        p in t
        for p in (
            "my ticket",
            "open ticket",
            "my open ticket",
            "the specialist",
            "my specialist",
            "ticket status",
            "status of my ticket",
        )
    )


def _derive_customer_ticket_id(
    tid: str,
    session_id: str,
    active_ticket: Optional[Dict[str, Any]] = None,
) -> str:
    """Derive customer-safe ticket reference string (REQ-AGENT-033, REQ-SAFE-004)."""
    def _is_uuid_str(val: Any) -> bool:
        try:
            uuid.UUID(str(val))
            return True
        except (ValueError, AttributeError, TypeError):
            return False

    if tid and not _is_uuid_str(tid):
        return tid

    if active_ticket:
        if active_ticket.get("issue_reference"):
            return str(active_ticket["issue_reference"])
        held_dec = active_ticket.get("decision")
        if isinstance(held_dec, str):
            try:
                held_dec = json.loads(held_dec)
            except Exception:
                held_dec = None
        if isinstance(held_dec, dict) and held_dec.get("ticket_id"):
            cand = str(held_dec["ticket_id"])
            if cand and not _is_uuid_str(cand):
                return cand

    try:
        from ticket_router.store.models import issue_reference
        return issue_reference(tid)
    except Exception:
        short_s = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8].upper()
        return f"TICK-{short_s}"
