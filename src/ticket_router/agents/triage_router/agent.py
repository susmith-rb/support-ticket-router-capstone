"""TriageRouterAgent: Entrypoint and ADK agent wiring for Agent Engine / Agent Runtime.

# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
# REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment
# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-AGENT-006: Inter-Agent A2A Handoff Protocol
# REQ-AUTH-002: Signed Customer Context Token (minting at TriageRouter)
# REQ-AUTH-003: CCT Transport and Verification at order-lookup-mcp
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

__all__ = ["TriageRouterAgent", "resolve_identity"]

from ticket_router.agents.triage_router.a2a_client import (
    A2AClientProtocol,
    HttpA2AClient,
    InMemoryA2AClient,
)
from ticket_router.agents.triage_router.mcp_client import (
    HttpTicketDbMcpClient,
    InMemoryTicketDbMcpClient,
    TicketDbMcpClientProtocol,
    compute_idempotency_key,
)
from ticket_router.agents.triage_router.pipeline import (
    dispatch_route_and_persist,
    evaluate_turn_policy,
    finalize_triage_turn,
    handle_turn_acts_and_special_intents,
    ingress_guard,
    read_memory_bank_context,
    resolve_turn_identity,
)
from ticket_router.agents.triage_router.policy import (
    RouterDecision,
    RouterThresholds,
    TriagePolicy,
    VerifiedFacts,
)
from ticket_router.agents.triage_router.result import TriageResult
from ticket_router.agents.triage_router.turn_interpreter import (
    TurnInterpreter,
    create_turn_interpreter,
)
from ticket_router.common.hedging import hedged_call
from ticket_router.common.observability import (
    StructuredJsonLogger,
    generate_traceparent,
    start_span,
)
from ticket_router.common.resilience import (
    TIER2_DEADLINE_S,
    TIER2_HEDGE_AFTER_S,
    ServiceTimeoutError,
)
from ticket_router.common.settings import Settings
from ticket_router.config.models import model_id, model_location, model_thinking_level
from ticket_router.handoff.human_handoff import HumanHandoff
from ticket_router.handoff.publisher import FakePublisher, PubSubRestPublisher
from ticket_router.identity import resolve_identity
from ticket_router.identity.customer_token import (
    KmsSigner,
    Signer,
    mint_customer_context_token,
)
from ticket_router.memory.client import (
    BaseMemoryBankClient,
    VertexMemoryBankClient,
)
from ticket_router.models.router_s1_client import RouterPrediction, RouterS1Client
from ticket_router.security.envelope import RedactionSealer
from ticket_router.security.model_armor import (
    ModelArmorClient,
    ModelArmorClientProtocol,
    ModelArmorVerdict,
)
from ticket_router.store.ids import canonical_ticket_uuid, issue_reference, legacy_session_issue_reference

logger = logging.getLogger(__name__)
_SIGNER_INIT_LOCK = threading.Lock()

# REQ-AGENT-038: Canonical allowlists and sanitizers for safe OpenTelemetry span attributes
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

    # 1. Sets/updates top-level turn_span attributes so they always match the final decision
    turn_span.set_attribute(
        "decision", _safe_enum(decision.decision, _SAFE_DECISIONS, default="escalate")
    )
    turn_span.set_attribute("tier", _safe_identifier_token(decision.tier, default="unknown"))
    turn_span.set_attribute("queue", _safe_identifier_token(decision.queue, default="none"))
    turn_span.set_attribute("degraded", bool(decision.degraded))
    turn_span.set_attribute(
        "predicted_intent", _safe_enum(decision.predicted_intent, _SAFE_INTENTS, default="other")
    )

    # 2. If decision.details has s1_prediction
    if isinstance(decision.details, dict) and isinstance(decision.details.get("s1_prediction"), dict):
        s1_info = decision.details["s1_prediction"]
        turn_span.set_attribute("s1.intent", _safe_enum(s1_info.get("intent"), _SAFE_INTENTS))
        turn_span.set_attribute("s1.intent_prob", _clamp_prob(s1_info.get("intent_prob")))
        turn_span.set_attribute("s1.escalate_prob", _clamp_prob(s1_info.get("escalate_prob")))

    # 3. Determine safe handoff_status
    handoff_status = "NONE"
    if isinstance(handoff_resp, dict) and handoff_resp.get("status"):
        st = str(handoff_resp["status"]).strip().upper()
        handoff_status = st if st in _SAFE_HANDOFF_STATUSES else "NONE"
    elif decision.decision == "escalate":
        handoff_status = "ESCALATED"
    elif decision.decision == "reject_oos":
        handoff_status = "AUTO_RESOLVED"

    # Effective summary fields
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

    # 4. Format router.final_decision
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


def _record_tier2_span_result(
    span: Any,
    resp_val: Any,
    target_model: str,
    parsed_holder: list[RouterPrediction],
) -> None:
    """Callback recording parsed tier2 response and raw_decision on tier2 span (REQ-AGENT-038)."""
    pred_val, raw_decision_json = _parse_tier2_response(resp_val, target_model)
    parsed_holder.append(pred_val)
    span.set_attribute("tier2.raw_decision", raw_decision_json)


def _record_tier2_span_error(
    span: Any,
    exc: BaseException | None,
    timed_out: bool,
) -> None:
    """Callback recording safe error/timeout metadata on tier2 span (REQ-AGENT-038)."""
    is_timeout = timed_out or isinstance(exc, ServiceTimeoutError)
    err_name = (
        "ServiceTimeoutError"
        if is_timeout
        else (type(exc).__name__ if exc is not None else "UnknownError")
    )
    span.set_attribute(
        "tier2.raw_decision",
        json.dumps(
            {"error": err_name, "parse_mode": "error", "timed_out": bool(is_timeout)},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def derive_channel_source(
    channel: Optional[str] = None,
    oauth_access_token: Optional[str] = None,
    caller_user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Derive channel source string for audit logging (REQ-IDENT-007).

    Returns 'gemini_enterprise', 'direct_api', or 'unknown'.
    Strictly non-authoritative: MUST NEVER be trusted for authorization or customer identity.
    """
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
    """Emit structured WARNING log for prompt injection / jailbreak security refusal (REQ-MA-003).

    CRITICAL SECURITY INVARIANT:
    Zero customer tokens. Never log raw message text or substrings > 12 characters.
    """
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
        if isinstance(held_dec, dict):
            stored = held_dec.get("ticket_id")
            if stored and not _is_uuid_str(stored):
                return str(stored)

        t_uuid = active_ticket.get("ticket_id") or active_ticket.get("id")
        if t_uuid and _is_uuid_str(t_uuid):
            return issue_reference(t_uuid)

    if tid and _is_uuid_str(tid):
        return issue_reference(tid)

    return legacy_session_issue_reference(session_id)


def _parse_tier2_json(clean_text: str, extra_raw: Dict[str, Any]) -> Tuple[Optional[RouterPrediction], str]:
    """Parse and validate Tier 2 JSON response payload into RouterPrediction."""
    try:
        parsed = json.loads(clean_text)
        if isinstance(parsed, dict) and (
            "escalate" in parsed or "intent" in parsed or "sentiment" in parsed
        ):
            esc_raw = parsed.get("escalate", False)
            if isinstance(esc_raw, bool):
                is_esc = esc_raw
            elif isinstance(esc_raw, str):
                is_esc = esc_raw.strip().lower() in ("true", "yes", "1", "escalate")
            else:
                is_esc = bool(esc_raw)

            sentiment = str(parsed.get("sentiment") or ("angry" if is_esc else "neutral")).strip().lower()
            urgency = str(parsed.get("urgency") or "normal").strip().lower()
            if sentiment == "angry" or urgency == "immediate":
                is_esc = True

            oos_raw = parsed.get("out_of_scope", False)
            if isinstance(oos_raw, bool):
                is_oos = oos_raw
            elif isinstance(oos_raw, str):
                is_oos = oos_raw.strip().lower() in ("true", "yes", "1")
            else:
                is_oos = bool(oos_raw)

            if is_esc:
                is_oos = False

            intent = "other" if is_oos else str(parsed.get("intent") or ("other" if is_esc else "faq")).strip()
            esc_prob = float(parsed.get("escalate_prob", 0.85 if is_esc else 0.10))
            intent_prob = float(parsed.get("intent_prob", 0.80))
            merged_raw = {**parsed, **extra_raw}
            return (
                RouterPrediction(
                    escalate=is_esc,
                    escalate_prob=esc_prob,
                    sentiment=sentiment,
                    sentiment_prob=0.85,
                    urgency=urgency,
                    urgency_prob=0.85,
                    intent=intent,
                    intent_prob=intent_prob,
                    latency_ms=120.0,
                    raw=merged_raw,
                    out_of_scope=is_oos,
                ),
                "json",
            )
    except Exception:
        pass

    is_angry = "angry" in clean_text.lower()
    is_urgent = "immediate" in clean_text.lower()
    is_esc = "escalate" in clean_text.lower() or is_angry or is_urgent
    fallback_raw = {"raw_text": clean_text, **extra_raw}
    pred = RouterPrediction(
        escalate=is_esc,
        escalate_prob=0.85 if is_esc else 0.10,
        sentiment="angry" if is_angry else "neutral",
        sentiment_prob=0.85,
        urgency="immediate" if is_urgent else "normal",
        urgency_prob=0.85,
        intent="other" if is_esc else "faq",
        intent_prob=0.80,
        latency_ms=120.0,
        raw=fallback_raw,
        out_of_scope=False,
    )
    return pred, "keyword_fallback"

def _parse_tier2_response(resp: Any, target_model: str) -> tuple[RouterPrediction, str]:
    """Parse Tier 2 model response into RouterPrediction and allowlisted raw decision JSON (REQ-AGENT-038)."""
    usage_data: Optional[Dict[str, int]] = None
    usage_meta = getattr(resp, "usage_metadata", None)
    if usage_meta is not None:
        pt = getattr(usage_meta, "prompt_token_count", None)
        ct = getattr(usage_meta, "candidates_token_count", None)
        tt = getattr(usage_meta, "total_token_count", None)
        if pt is not None or ct is not None or tt is not None:
            p_int = int(pt) if pt is not None else 0
            c_int = int(ct) if ct is not None else 0
            t_int = int(tt) if tt is not None else (p_int + c_int)
            usage_data = {
                "prompt_tokens": p_int,
                "output_tokens": c_int,
                "total_tokens": t_int,
            }
    extra_raw: Dict[str, Any] = {
        "model_id": target_model,
        "usage": usage_data,
    }

    raw_text = ""
    try:
        raw_text = str(resp.text)
    except AttributeError:
        raw_text = ""

    clean_text = raw_text.strip()
    if clean_text.startswith("```"):
        lines = clean_text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        clean_text = "\n".join(lines).strip()

    pred, parse_mode = _parse_tier2_json(clean_text, extra_raw)

    raw_decision_payload = {
        "escalate": bool(pred.escalate),
        "escalate_prob": _clamp_prob(pred.escalate_prob),
        "intent": _safe_enum(pred.intent, _SAFE_INTENTS),
        "intent_prob": _clamp_prob(pred.intent_prob),
        "out_of_scope": bool(pred.out_of_scope),
        "parse_mode": parse_mode,
        "sentiment": _safe_enum(pred.sentiment, _SAFE_SENTIMENTS),
        "urgency": _safe_enum(pred.urgency, _SAFE_URGENCIES),
    }
    raw_decision_json = json.dumps(
        raw_decision_payload, sort_keys=True, separators=(",", ":")
    )
    return pred, raw_decision_json


def invoke_tier2_gemini(
    text: str,
    target_model: str,
    project: Optional[str] = None,
    location: Optional[str] = None,
) -> RouterPrediction:
    """Execute Tier 2 fallback inference using Gemini Flash (REQ-AGENT-002, REQ-AGENT-013, REQ-AGENT-038)."""
    from ticket_router.models.tier2_gemini import check_gemini_429_hook

    check_gemini_429_hook()
    try:
        from ticket_router.models.genai_client import get_genai_client
        effective_location = location or model_location("router_tier2")
        client = get_genai_client(
            project=project,
            location=effective_location,
        )
        from ticket_router.config.agent_tuning import (
            get_active_tier2_prompt_version,
            get_tier2_prompt,
            get_tier2_system_instruction,
        )
        prompt_ver = get_active_tier2_prompt_version()
        prompt = get_tier2_prompt(text, version=prompt_ver)
        sys_inst = get_tier2_system_instruction(version=prompt_ver)
        thinking_lvl = model_thinking_level("router_tier2")

        call_kwargs: Dict[str, Any] = {
            "model": target_model,
            "contents": prompt,
        }
        if sys_inst or thinking_lvl:
            gen_cfg: Any = None
            try:
                from google.genai import types as genai_types  # type: ignore[import-untyped]
                if hasattr(genai_types, "GenerateContentConfig"):
                    cfg_kwargs: Dict[str, Any] = {}
                    if sys_inst:
                        cfg_kwargs["system_instruction"] = sys_inst
                    if thinking_lvl:
                        if hasattr(genai_types, "ThinkingConfig"):
                            cfg_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                                thinking_level=thinking_lvl
                            )
                        else:
                            cfg_kwargs["thinking_config"] = {"thinking_level": thinking_lvl}
                    gen_cfg = genai_types.GenerateContentConfig(**cfg_kwargs)
                else:
                    dict_cfg: Dict[str, Any] = {}
                    if sys_inst:
                        dict_cfg["system_instruction"] = sys_inst
                    if thinking_lvl:
                        dict_cfg["thinking_config"] = {"thinking_level": thinking_lvl}
                    gen_cfg = dict_cfg
            except Exception:
                dict_cfg = {}
                if sys_inst:
                    dict_cfg["system_instruction"] = sys_inst
                if thinking_lvl:
                    dict_cfg["thinking_config"] = {"thinking_level": thinking_lvl}
                gen_cfg = dict_cfg
            call_kwargs["config"] = gen_cfg

        parsed_holder: list[RouterPrediction] = []

        resp = hedged_call(
            lambda: client.models.generate_content(**call_kwargs),
            TIER2_HEDGE_AFTER_S,
            TIER2_DEADLINE_S,
            span_name="tier2.generate_content",
            span_attributes={"tier2.model": str(target_model), "tier2.prompt_version": prompt_ver},
            on_span_result=lambda s, r: _record_tier2_span_result(s, r, target_model, parsed_holder),
            on_span_error=_record_tier2_span_error,
        )

        if parsed_holder:
            return parsed_holder[0]
        return _parse_tier2_response(resp, target_model)[0]
    except Exception as exc:
        raise RuntimeError(f"Tier 2 model call failed ({target_model}): {exc}") from exc


@dataclass(frozen=True)
class Tier2GeminiInvoker:
    """Picklable top-level callable invoking Tier 2 Gemini Flash with configured project/location."""

    model_role: str = "router_tier2"
    project: Optional[str] = None
    location: Optional[str] = None

    def __call__(self, text: str) -> RouterPrediction:
        target_model = model_id(self.model_role)
        resolved_location = model_location(self.model_role) or self.location
        proj = self.project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
        return invoke_tier2_gemini(
            text,
            target_model,
            project=proj,
            location=resolved_location,
        )


def default_tier2_invoker(text: str) -> RouterPrediction:
    """Default module-scope Tier 2 invoker resolving model dynamically."""
    model_role = "router_tier2"
    target_model = model_id(model_role)
    resolved_location = model_location(model_role) or os.environ.get("GOOGLE_CLOUD_LOCATION")
    proj = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
    return invoke_tier2_gemini(
        text,
        target_model,
        project=proj,
        location=resolved_location,
    )


def create_tier2_invoker(
    model_role: str = "router_tier2",
    custom_caller: Optional[Callable[[str], RouterPrediction]] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
) -> Callable[[str], RouterPrediction]:
    """Build a Tier 2 fallback invoker using Gemini Flash (REQ-AGENT-002, REQ-AGENT-013).

    Model ID is resolved dynamically from config/models.yaml to guarantee zero hard-coded literals.
    """
    if custom_caller is not None:
        return custom_caller
    # Validate model_role exists in registry at creation time
    _ = model_id(model_role)
    if model_role == "router_tier2" and project is None and location is None:
        return default_tier2_invoker
    return Tier2GeminiInvoker(model_role=model_role, project=project, location=location)


class TriageRouterAgent:
    """TriageRouterAgent coordinating rules, router-s1, Flash, MCP store, and A2A handoffs.

    Fulfills REQ-AGENT-001..006, 011..015.
    Supports lazy initialization for Agent Engine serialization safety.
    """

    def __init__(
        self,
        policy: Optional[TriagePolicy] = None,
        mcp_client: Optional[TicketDbMcpClientProtocol] = None,
        a2a_client: Optional[A2AClientProtocol] = None,
        model_armor_client: Optional[ModelArmorClientProtocol] = None,
        logger: Optional[StructuredJsonLogger] = None,
        lazy_init: bool = False,
        cct_signer: Optional[Signer] = None,
        signer: Optional[Signer] = None,
        settings: Optional[Settings] = None,
        human_handoff: Optional[HumanHandoff] = None,
        redaction_sealer: Optional[RedactionSealer] = None,
        memory_client: Optional[BaseMemoryBankClient] = None,
        turn_interpreter: Optional[TurnInterpreter] = None,
    ) -> None:
        self._policy = policy
        self._mcp_client = mcp_client
        self._a2a_client = a2a_client
        self._model_armor_client = model_armor_client
        self._human_handoff = human_handoff
        self._redaction_sealer = redaction_sealer
        self._memory_client = memory_client
        self._turn_interpreter = turn_interpreter
        self.logger = logger or StructuredJsonLogger(name="triage_router")
        self.lazy_init = lazy_init
        self._injected_signer: Optional[Signer] = cct_signer if cct_signer is not None else signer
        self._default_kms_signer: Optional[KmsSigner] = None
        self._settings: Optional[Settings] = settings
        self._expected_oauth_client_id: Optional[str] = os.environ.get("GE_OAUTH_CLIENT_ID") or None
        if not lazy_init:
            self._ensure_initialized()

    def _ensure_initialized(self) -> None:
        """Initialize dependencies; validates environment settings when require_real_deps is True."""
        if not self.lazy_init and self._policy is not None and self._mcp_client is not None and self._a2a_client is not None and self._human_handoff is not None:
            return

        settings = Settings.from_env()
        if self._expected_oauth_client_id is None and settings.ge_oauth_client_id:
            self._expected_oauth_client_id = settings.ge_oauth_client_id
        if self._model_armor_client is None and settings.model_armor_enabled:
            self._model_armor_client = ModelArmorClient(
                template=settings.model_armor_template,
            )

        if settings.require_real_deps:
            settings.validate_for_triage()
            s1_client = RouterS1Client(
                endpoint=settings.router_s1_endpoint,
                base_url=settings.router_s1_base_url,
            )
            tier2_invoker = create_tier2_invoker(
                "router_tier2",
                project=settings.project,
                location=settings.location,
            )
            ti = self._turn_interpreter or create_turn_interpreter(
                project=settings.project,
                location=settings.location,
            )
            self._policy = TriagePolicy(
                thresholds=RouterThresholds.from_tuning_config(),
                s1_client=s1_client,
                tier2_invoker=tier2_invoker,
                turn_interpreter=ti,
                model_armor_client=self._model_armor_client,
            )
            self._mcp_client = HttpTicketDbMcpClient(
                base_url=settings.ticket_db_mcp_url or "http://localhost:8080",
            )
            self._a2a_client = HttpA2AClient(
                endpoint_url=settings.faq_resolver_url or "http://localhost:8080",
                target_audience=settings.faq_resolver_audience,
            )
        else:
            if self._policy is None:
                ti = self._turn_interpreter or create_turn_interpreter()
                self._policy = TriagePolicy(
                    thresholds=RouterThresholds.from_tuning_config(),
                    s1_client=RouterS1Client(),
                    tier2_invoker=create_tier2_invoker("router_tier2"),
                    turn_interpreter=ti,
                    model_armor_client=self._model_armor_client,
                )
            if self._mcp_client is None:
                self._mcp_client = InMemoryTicketDbMcpClient()
            if self._a2a_client is None:
                self._a2a_client = InMemoryA2AClient()

        if self._human_handoff is None:
            if settings.require_real_deps:
                pub = PubSubRestPublisher(
                    project=settings.project or os.environ.get("GOOGLE_CLOUD_PROJECT"),
                    topic=os.environ.get("HUMAN_QUEUE_TOPIC", "human-queue"),
                )
            else:
                pub = FakePublisher()
            self._human_handoff = HumanHandoff(
                ticket_db_client=self._mcp_client,
                publisher=pub,
                escalated_by="triage_router",
            )
        if self._memory_client is None:
            self._memory_client = VertexMemoryBankClient(
                project=settings.project,
                region=settings.location,
            )
        self.lazy_init = False

    @property
    def policy(self) -> TriagePolicy:
        self._ensure_initialized()
        assert self._policy is not None
        return self._policy

    @property
    def mcp_client(self) -> TicketDbMcpClientProtocol:
        self._ensure_initialized()
        assert self._mcp_client is not None
        return self._mcp_client

    @property
    def a2a_client(self) -> A2AClientProtocol:
        self._ensure_initialized()
        assert self._a2a_client is not None
        return self._a2a_client

    @property
    def human_handoff(self) -> HumanHandoff:
        self._ensure_initialized()
        assert self._human_handoff is not None
        return self._human_handoff

    @property
    def memory_client(self) -> BaseMemoryBankClient:
        self._ensure_initialized()
        if self._memory_client is None:
            self._memory_client = VertexMemoryBankClient()
        return self._memory_client

    def _mint_cct_if_verified(
        self,
        *,
        customer_verified: bool,
        customer_id: Optional[str],
        ticket_id: str,
    ) -> Optional[str]:
        """Mint a Customer Context Token solely for verified customers when enabled.

        # REQ-AUTH-002: Fail-safe on any minting exception; never log token or customer_id.
        """
        if not customer_verified or not customer_id or customer_id.startswith("anon:"):
            return None

        settings = self._settings if self._settings is not None else Settings.from_env()
        key_version = settings.cct_key_version
        if not key_version:
            return None

        try:
            active_signer = self._injected_signer
            if active_signer is None:
                with _SIGNER_INIT_LOCK:
                    if self._default_kms_signer is None or self._default_kms_signer.kid != key_version:
                        self._default_kms_signer = KmsSigner(key_version)
                    active_signer = self._default_kms_signer

            return mint_customer_context_token(
                signer=active_signer,
                issuer=settings.cct_issuer,
                audience=settings.cct_audience,
                customer_id=customer_id,
                ticket_id=ticket_id,
                ttl_s=settings.cct_ttl_s,
            )
        except Exception as exc:
            logger.warning(
                json.dumps(
                    {
                        "event": "cct_mint_failed",
                        "ticket_id": ticket_id,
                        "error_type": type(exc).__name__,
                    },
                    separators=(",", ":"),
                )
            )
            return None

    def _safe_upsert(
        self,
        *,
        ticket_id: str,
        idempotency_key: str,
        customer_id: str,
        channel: str,
        body_redacted: str,
        decision: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Persist ticket with 1 retry; log PII-free warning on final failure (REQ-AGENT-027)."""
        last_exc: Optional[Exception] = None
        for _attempt in range(2):
            try:
                return self.mcp_client.upsert_ticket(
                    idempotency_key=idempotency_key,
                    customer_id=customer_id,
                    channel=channel,
                    body_redacted=body_redacted,
                    decision=decision,
                )
            except Exception as exc:
                last_exc = exc

        err_type = type(last_exc).__name__ if last_exc is not None else "UnknownError"
        logger.warning(
            json.dumps(
                {
                    "event": "ticket_upsert_failed",
                    "ticket_id": ticket_id,
                    "error_type": err_type,
                },
                separators=(",", ":"),
            )
        )
        return None

    def _save_redaction_map_if_needed(
        self,
        *,
        ticket_id: str,
        customer_id: str,
        mapping: Dict[str, str],
        ticket_uuid: Optional[str] = None,
    ) -> None:
        """Seal and save KMS envelope-encrypted PII redaction map (REQ-STORE-009).

        Never raises; failures log redaction_map_failed with ticket_id only.
        """
        if not mapping:
            return

        kms_key = os.environ.get("REDACTION_KMS_KEY", "").strip()
        if not kms_key and self._redaction_sealer is None:
            logger.warning(
                json.dumps(
                    {
                        "event": "redaction_map_failed",
                        "ticket_id": ticket_id,
                    },
                    separators=(",", ":"),
                )
            )
            return

        try:
            sealer = self._redaction_sealer or RedactionSealer(kms_key_name=kms_key)
            envelope_b64, key_id = sealer.seal(ticket_id=ticket_id, mapping=mapping)
            target_uuid = ticket_uuid or ticket_id
            kwargs: Dict[str, Any] = {
                "ticket_id": target_uuid,
                "customer_id": customer_id,
                "envelope_b64": envelope_b64,
                "key_id": key_id,
            }
            if ticket_id and ticket_id != target_uuid and hasattr(self.mcp_client, "redaction_maps"):
                kwargs["alias_id"] = ticket_id
            self.mcp_client.save_redaction_map(**kwargs)
        except Exception:
            logger.warning(
                json.dumps(
                    {
                        "event": "redaction_map_failed",
                        "ticket_id": ticket_id,
                    },
                    separators=(",", ":"),
                )
            )

    def handle_turn(
        self,
        session_id: str,
        message_text: str,
        customer_id: Optional[str] = None,
        customer_id_token: Optional[str] = None,
        verified_facts: Optional[VerifiedFacts] = None,
        channel: str = "web",
        ticket_id: Optional[str] = None,
        memory_summary: str = "",
        oauth_access_token: Optional[str] = None,
        caller_user_id: Optional[str] = None,
        customer_verified: Optional[bool] = None,
    ) -> TriageResult:
        """Handle customer turn with (session_id, message_text) ordering."""
        return self.triage(
            session_id=session_id,
            customer_id=customer_id,
            message_text=message_text,
            customer_id_token=customer_id_token,
            verified_facts=verified_facts,
            channel=channel,
            ticket_id=ticket_id,
            memory_summary=memory_summary,
            oauth_access_token=oauth_access_token,
            caller_user_id=caller_user_id,
            customer_verified=customer_verified,
        )

    def triage(
        self,
        session_id: str,
        customer_id: Optional[str] = None,
        message_text: str = "",
        customer_id_token: Optional[str] = None,
        verified_facts: Optional[VerifiedFacts] = None,
        channel: str = "web",
        ticket_id: Optional[str] = None,
        memory_summary: str = "",
        oauth_access_token: Optional[str] = None,
        caller_user_id: Optional[str] = None,
        customer_verified: Optional[bool] = None,
    ) -> TriageResult:
        """Execute end-to-end triage evaluation, persistence, and dispatch via staged linear pipeline."""
        self._ensure_initialized()
        start_time = time.monotonic()
        traceparent = generate_traceparent()
        idempotency_key = compute_idempotency_key(session_id, message_text)
        canonical_uuid = canonical_ticket_uuid(idempotency_key)
        t_id = ticket_id or issue_reference(canonical_uuid)

        if customer_id is not None:
            logger.warning("customer_id argument is deprecated and ignored; pass customer_id_token instead.")

        with start_span("triage_router_turn", {"session_id": session_id, "ticket_id": t_id, "channel": channel}, traceparent=traceparent) as turn_span:
            placeholder_cid = f"anon:{session_id}"

            # Stage 1: Ingress Guard
            early_res, ma_verdict, sanitized_text, tokenized_text, pii_mapping = ingress_guard(
                self, message_text=message_text, session_id=session_id, t_id=t_id, channel=channel,
                turn_span=turn_span, start_time=start_time, idempotency_key=idempotency_key,
                oauth_access_token=oauth_access_token, caller_user_id=caller_user_id, traceparent=traceparent,
            )
            if early_res is not None:
                return early_res

            # Stage 2: Identity Resolution
            cust_ctx, cust_verified, ticket_customer_id, identity_issuer, identity_reason, conv_state = resolve_turn_identity(
                self, customer_id_token=customer_id_token, customer_id=customer_id, customer_verified=customer_verified,
                oauth_access_token=oauth_access_token, session_id=session_id, placeholder_cid=placeholder_cid,
            )

            # Stage 3: Turn Policy Evaluation
            decision, body_to_persist, pii_mapping = evaluate_turn_policy(
                self.policy, message_text=message_text, session_id=session_id, placeholder_cid=placeholder_cid,
                verified_facts=verified_facts, t_id=t_id, conv_state=conv_state, ma_verdict=ma_verdict,
                sanitized_text=sanitized_text, tokenized_text=tokenized_text, pii_mapping=pii_mapping,
                customer_verified=cust_verified, identity_issuer=identity_issuer, identity_reason=identity_reason,
                turn_span=turn_span,
            )

            # Stage 4: Turn Acts and Special Intents
            act_res, decision = handle_turn_acts_and_special_intents(
                self, decision=decision, message_text=message_text, session_id=session_id, t_id=t_id, channel=channel,
                turn_span=turn_span, start_time=start_time, idempotency_key=idempotency_key, traceparent=traceparent,
                customer_verified=cust_verified, ticket_customer_id=ticket_customer_id, placeholder_cid=placeholder_cid,
                conv_state=conv_state, ma_verdict=ma_verdict, tokenized_text=tokenized_text, pii_mapping=pii_mapping,
                oauth_access_token=oauth_access_token, caller_user_id=caller_user_id, cust_ctx=cust_ctx,
            )
            if act_res is not None:
                return act_res

            # Stage 5: Memory Bank Context
            effective_mem_summary = read_memory_bank_context(
                self.memory_client, customer_verified=cust_verified, ticket_customer_id=ticket_customer_id,
                memory_summary=memory_summary, body_to_persist=body_to_persist, decision=decision, turn_span=turn_span,
            )

            # Stage 6: Dispatch Route and Persist
            decision, calming_ack, handoff_resp, status_code, error_msg = dispatch_route_and_persist(
                self, decision=decision, message_text=message_text, body_to_persist=body_to_persist, session_id=session_id,
                t_id=t_id, canonical_uuid=canonical_uuid, channel=channel, customer_verified=cust_verified,
                ticket_customer_id=ticket_customer_id, cust_ctx=cust_ctx, identity_issuer=identity_issuer,
                memory_summary=effective_mem_summary, traceparent=traceparent, idempotency_key=idempotency_key,
                pii_mapping=pii_mapping, ma_verdict=ma_verdict, oauth_access_token=oauth_access_token, caller_user_id=caller_user_id,
            )

            # Stage 7: Finalize Turn
            return finalize_triage_turn(
                self, decision=decision, t_id=t_id, session_id=session_id, traceparent=traceparent, start_time=start_time,
                customer_verified=cust_verified, ticket_customer_id=ticket_customer_id, body_to_persist=body_to_persist,
                calming_ack=calming_ack, handoff_resp=handoff_resp, idempotency_key=idempotency_key, status_code=status_code,
                error_msg=error_msg, pii_mapping=pii_mapping, turn_span=turn_span,
            )


# -----------------------------------------------------------------------------
# ADK Root Agent Wiring (for Agent Engine / Agent Runtime deploy)
# -----------------------------------------------------------------------------

class _AdkAgentShim:
    """Lightweight ADK-compatible agent container for environments where google-adk is optional."""

    def __init__(self, name: str, model: str, instruction: str, triage_handler: TriageRouterAgent) -> None:
        self.name = name
        self.model = model
        self.instruction = instruction
        self.triage_handler = triage_handler

    def query(
        self,
        query: str,
        session_id: str = "default_session",
        customer_id_token: Optional[str] = None,
        customer_id: Optional[str] = None,
        caller_user_id: Optional[str] = None,
        channel: Optional[str] = None,
        oauth_access_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Synchronous query entrypoint."""
        if customer_id is not None:
            logger.warning(
                "customer_id kwarg is deprecated and ignored; pass customer_id_token instead."
            )
        res = self.triage_handler.triage(
            session_id=session_id,
            message_text=query,
            customer_id_token=customer_id_token,
            caller_user_id=caller_user_id,
            channel=channel or "web",
            oauth_access_token=oauth_access_token,
        )
        ans = None
        if res.handoff_response and res.handoff_response.get("answer"):
            ans = str(res.handoff_response.get("answer"))
            conf_q = (
                res.handoff_response.get("confirmation_question")
                or res.handoff_response.get("resolution_prompt")
            )
            if conf_q and str(conf_q).strip() not in ans:
                ans = f"{ans.rstrip()}\n\n{str(conf_q).strip()}"
        elif res.calming_ack:
            ans = res.calming_ack

        resp: Dict[str, Any] = {
            "ticket_id": res.ticket_id,
            "decision": res.decision.decision,
            "queue": res.decision.queue,
            "calming_ack": res.calming_ack,
            "intent": res.decision.predicted_intent,
        }
        if ans is not None:
            resp["answer"] = ans
        if res.status_code != 200:
            resp["status_code"] = res.status_code
        if res.error:
            resp["error"] = res.error
        return resp

    def stream_query(
        self,
        query: str,
        session_id: str = "default_session",
        customer_id_token: Optional[str] = None,
        customer_id: Optional[str] = None,
        caller_user_id: Optional[str] = None,
        channel: Optional[str] = None,
        oauth_access_token: Optional[str] = None,
    ):
        """Generator stream query entrypoint for Gemini Enterprise compatibility."""
        yield self.query(
            query,
            session_id=session_id,
            customer_id_token=customer_id_token,
            customer_id=customer_id,
            caller_user_id=caller_user_id,
            channel=channel,
            oauth_access_token=oauth_access_token,
        )

    def triage(self, *args: Any, **kwargs: Any) -> TriageResult:
        """Direct triage delegation."""
        return self.triage_handler.triage(*args, **kwargs)


def create_root_agent() -> Any:
    """Factory creating root_agent for Agent Engine / Agent Runtime deploy."""
    router_agent_instance = TriageRouterAgent(lazy_init=True)
    instruction = (
        "You are the TriageRouterAgent for customer support. You classify inbound "
        "messages into automated FAQ resolution or human queue escalation."
    )
    resolved_model = model_id("router_tier2")

    try:
        from google.adk.agents import Agent  # type: ignore[import-untyped]
        return Agent(
            name="triage_router",
            model=resolved_model,
            instruction=instruction,
            tools=[],
        )
    except ImportError:
        return _AdkAgentShim(
            name="triage_router",
            model=resolved_model,
            instruction=instruction,
            triage_handler=router_agent_instance,
        )


# Expose root_agent at module level as required for Agent Engine / Agent Runtime deployment
root_agent = create_root_agent()
