"""pipeline.py — Staged linear pipeline for TriageRouterAgent turn evaluation.

REQ-AGENT-027, REQ-AGENT-028, REQ-AGENT-030, REQ-AGENT-032, REQ-AGENT-033, REQ-AGENT-036,
REQ-AGENT-037, REQ-TURN-002, REQ-TURN-003, REQ-TURN-004, REQ-TURN-005, REQ-HIST-001,
REQ-HIST-002, REQ-SAFE-002, REQ-SAFE-004, REQ-LIFE-001, REQ-LIFE-002.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import inspect
import json
import logging
import re
import time
from dataclasses import asdict, replace
from typing import Any, Dict, Optional, Tuple

from ticket_router.agents.triage_router.a2a_client import A2AHandoffPayload
from ticket_router.agents.triage_router.conversation_state import (
    ConversationState,
    assemble_conversation_state,
)
from ticket_router.agents.triage_router.pipeline_helpers import (
    _derive_customer_ticket_id,
    _emit_security_blocked_log,
    _extract_ticket_id_reference,
    _is_explicit_ticket_reference,
    _record_turn_final_span_attributes,
    derive_channel_source,
)
from ticket_router.agents.triage_router.policy import (
    RouterDecision,
    VerifiedFacts,
    _extract_blocked_card_info_types,
)
from ticket_router.agents.triage_router.result import TriageResult
from ticket_router.agents.triage_router.security_intents import (
    AUTH_BYPASS_REFUSAL,
    ORDER_CROSS_CUSTOMER_REFUSAL,
    emit_security_refusal_log,
    get_security_intent_reply,
)
from ticket_router.agents.triage_router.templates import (
    render_calming_acknowledgement,
    render_card_data_blocked_reply,
    render_conversational_reply,
    render_followup_calming_reply,
    render_model_armor_security_trigger_ack,
    render_out_of_scope_refusal,
    render_security_blocked_reply,
)
from ticket_router.common.observability import get_current_traceparent
from ticket_router.identity import resolve_identity
from ticket_router.identity.resolver import CustomerContext
from ticket_router.security.model_armor import ModelArmorVerdict
from ticket_router.security.pii_tokens import restore_pii_tokens
from ticket_router.store.models import (
    effective_category,
    is_holding_ticket,
    is_human_owned,
)

logger = logging.getLogger(__name__)

HISTORY_MEMORY_TIMEOUT_S = 1.0


def clean_decision_dict(dec: RouterDecision) -> Dict[str, Any]:
    """Clean router decision dictionary for serialization and persistence."""
    d = asdict(dec)
    d.pop("pii_mapping", None)
    d.pop("tokenized_text", None)
    return d


def _handle_card_data_blocked(
    t_id: str,
    session_id: str,
    blocked_card_types: list[str],
    start_time: float,
    turn_span: Any,
    idempotency_key: str,
) -> TriageResult:
    blocked_reply = render_card_data_blocked_reply()
    turn_span.set_attribute("decision", "blocked")
    turn_span.set_attribute("tier", "ingress_guard")
    turn_span.set_attribute("queue", "none")
    turn_span.set_attribute("degraded", False)
    turn_span.set_attribute("predicted_intent", "other")
    logger.warning(
        json.dumps(
            {
                "event": "card_data_blocked",
                "ticket_id": t_id,
                "session_id": session_id,
                "blocked_info_types": blocked_card_types,
            },
            separators=(",", ":"),
        )
    )
    decision = RouterDecision(
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
        details={"blocked_info_types": blocked_card_types},
        tokenized_text=None,
        pii_mapping={},
    )
    elapsed_ms = (time.monotonic() - start_time) * 1000.0
    handoff_card = {
        "status": "BLOCKED",
        "route": "blocked",
        "answer": blocked_reply,
        "status_code": 400,
        "error": "card_data_blocked",
    }
    _record_turn_final_span_attributes(turn_span, decision, handoff_card)
    return TriageResult(
        ticket_id=t_id,
        session_id=session_id,
        decision=decision,
        calming_ack=blocked_reply,
        handoff_response=handoff_card,
        idempotency_key=idempotency_key,
        latency_ms=elapsed_ms,
        status_code=400,
        error="card_data_blocked",
    )


def _handle_prompt_security_blocked(
    agent: Any,
    message_text: str,
    session_id: str,
    t_id: str,
    channel: str,
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    oauth_access_token: Optional[str],
    caller_user_id: Optional[str],
    traceparent: str,
    ma_verdict: ModelArmorVerdict,
    tokenized_text: Optional[str],
    pii_mapping: Dict[str, str],
) -> TriageResult:
    sec_ack = render_security_blocked_reply()
    eff_channel = derive_channel_source(
        channel=channel,
        oauth_access_token=oauth_access_token,
        caller_user_id=caller_user_id,
        session_id=session_id,
    )
    _emit_security_blocked_log(
        traceparent=traceparent,
        session_id=session_id,
        channel=eff_channel,
        customer_verified=False,
        customer_id=None,
        ma_verdict=ma_verdict,
        message_text=message_text,
    )
    elapsed_ms = (time.monotonic() - start_time) * 1000.0
    turn_span.set_attribute("decision", "security_blocked")
    turn_span.set_attribute("tier", "model_armor")
    turn_span.set_attribute("queue", "none")
    turn_span.set_attribute("degraded", False)
    turn_span.set_attribute("predicted_intent", "other")

    sec_decision = RouterDecision(
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
        latency_ms=elapsed_ms,
        details={
            "category": "security",
            "model_armor": ma_verdict.to_summary_dict(),
        },
        customer_verified=False,
        tokenized_text=tokenized_text,
        pii_mapping=pii_mapping,
    )
    agent.logger.log_decision(
        ticket_id=t_id,
        session_id=session_id,
        decision="security_blocked",
        tier="model_armor",
        queue="none",
        degraded=False,
        latency_ms=elapsed_ms,
        traceparent=traceparent,
        predicted_intent="other",
    )
    handoff_sec = {
        "status": "SECURITY_BLOCKED",
        "route": "security_blocked",
        "answer": sec_ack,
        "status_code": 200,
    }
    _record_turn_final_span_attributes(turn_span, sec_decision, handoff_sec)
    return TriageResult(
        ticket_id=t_id,
        session_id=session_id,
        decision=sec_decision,
        calming_ack=sec_ack,
        handoff_response=handoff_sec,
        idempotency_key=idempotency_key,
        latency_ms=elapsed_ms,
        status_code=200,
        error=None,
    )


def ingress_guard(
    agent: Any,
    message_text: str,
    session_id: str,
    t_id: str,
    channel: str,
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    oauth_access_token: Optional[str],
    caller_user_id: Optional[str],
    traceparent: str,
) -> Tuple[Optional[TriageResult], Optional[ModelArmorVerdict], Optional[str], Optional[str], Dict[str, str]]:
    """Stage 1: Execute Ingress Guard (Model Armor card data and prompt safety)."""
    policy = agent.policy
    ma_verdict: Optional[ModelArmorVerdict] = None
    sanitized_text: Optional[str] = None
    tokenized_text: Optional[str] = None
    pii_mapping: Dict[str, str] = {}
    ma_client = getattr(policy, "model_armor_client", None)
    if ma_client is not None and hasattr(policy, "_run_model_armor"):
        ma_verdict, sanitized_text, tokenized_text, pii_mapping = policy._run_model_armor(message_text)
        blocked_card_types = _extract_blocked_card_info_types(ma_verdict)
        if blocked_card_types:
            card_res = _handle_card_data_blocked(
                t_id=t_id,
                session_id=session_id,
                blocked_card_types=blocked_card_types,
                start_time=start_time,
                turn_span=turn_span,
                idempotency_key=idempotency_key,
            )
            return card_res, ma_verdict, sanitized_text, tokenized_text, pii_mapping

    # Model Armor PI/JB check on every turn before holding or intent routing (REQ-MA-003, Requirement A)
    if (
        ma_verdict is not None
        and ma_verdict.has_pi_jb
        and not getattr(policy, "_is_prompt_safety_delegated", lambda: False)()
    ):
        sec_res = _handle_prompt_security_blocked(
            agent=agent,
            message_text=message_text,
            session_id=session_id,
            t_id=t_id,
            channel=channel,
            turn_span=turn_span,
            start_time=start_time,
            idempotency_key=idempotency_key,
            oauth_access_token=oauth_access_token,
            caller_user_id=caller_user_id,
            traceparent=traceparent,
            ma_verdict=ma_verdict,
            tokenized_text=tokenized_text,
            pii_mapping=pii_mapping,
        )
        return sec_res, ma_verdict, sanitized_text, tokenized_text, pii_mapping

    return None, ma_verdict, sanitized_text, tokenized_text, pii_mapping


def resolve_turn_identity(
    agent: Any,
    customer_id_token: Optional[str],
    customer_id: Optional[str],
    customer_verified: Optional[bool],
    oauth_access_token: Optional[str],
    session_id: str,
    placeholder_cid: str,
) -> Tuple[CustomerContext, bool, str, Optional[str], Optional[str], ConversationState]:
    """Stage 2: Resolve customer identity and assemble conversation state (REQ-AGENT-027, REQ-TURN-002)."""
    mcp_client = agent.mcp_client
    cust_ctx = resolve_identity(
        token=customer_id_token,
        ticket_db_client=mcp_client,
        oauth_access_token=oauth_access_token,
        expected_oauth_client_id=agent._expected_oauth_client_id,
    )
    if customer_verified is not None:
        state_verified = bool(customer_verified)
        state_cid = customer_id or (cust_ctx.customer_id if cust_ctx.verified else None)
    else:
        state_verified = bool(cust_ctx.verified)
        state_cid = cust_ctx.customer_id if cust_ctx.verified else None

    conv_state = assemble_conversation_state(
        mcp_client=mcp_client,
        session_id=session_id,
        customer_id=state_cid if state_verified else None,
        customer_verified=state_verified,
    )

    if customer_verified is not None:
        effective_customer_verified = customer_verified
        ticket_customer_id = customer_id or (cust_ctx.customer_id if cust_ctx.verified else placeholder_cid)
    else:
        effective_customer_verified = cust_ctx.verified
        ticket_customer_id = cust_ctx.customer_id if cust_ctx.verified else placeholder_cid

    identity_issuer = cust_ctx.issuer if cust_ctx.verified else None
    identity_reason = cust_ctx.reason

    return cust_ctx, effective_customer_verified, ticket_customer_id, identity_issuer, identity_reason, conv_state


def evaluate_turn_policy(
    policy: Any,
    message_text: str,
    session_id: str,
    placeholder_cid: str,
    verified_facts: Optional[VerifiedFacts],
    t_id: str,
    conv_state: ConversationState,
    ma_verdict: Optional[ModelArmorVerdict],
    sanitized_text: Optional[str],
    tokenized_text: Optional[str],
    pii_mapping: Dict[str, str],
    customer_verified: bool,
    identity_issuer: Optional[str],
    identity_reason: Optional[str],
    turn_span: Any,
) -> Tuple[RouterDecision, str, Dict[str, str]]:
    """Stage 3: Evaluate tiered routing policy on inbound message text."""
    eval_kwargs = {
        "text": message_text,
        "session_id": session_id,
        "customer_id": placeholder_cid,
        "verified_facts": verified_facts,
        "ticket_id": t_id,
        "conversation_state": conv_state,
    }
    try:
        sig = inspect.signature(policy.evaluate)
        if "precomputed_ma" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            eval_kwargs["precomputed_ma"] = (ma_verdict, sanitized_text, tokenized_text, pii_mapping)
    except Exception as exc:
        logger.debug("Failed inspecting policy.evaluate signature: %s", exc)

    decision = policy.evaluate(**eval_kwargs)

    # Set PII-safe attributes on turn_span (REQ-AGENT-014)
    turn_span.set_attribute("decision", decision.decision)
    turn_span.set_attribute("tier", decision.tier)
    turn_span.set_attribute("queue", decision.queue)
    turn_span.set_attribute("degraded", decision.degraded)
    turn_span.set_attribute("predicted_intent", decision.predicted_intent)

    # Attach verified customer identity claims to decision payload (no token, no email)
    decision = replace(
        decision,
        customer_verified=customer_verified,
        identity_issuer=identity_issuer,
        identity_reason=identity_reason,
    )

    body_to_persist = message_text
    if decision.tokenized_text:
        body_to_persist = decision.tokenized_text
    elif decision.details.get("sanitized_text"):
        body_to_persist = decision.details["sanitized_text"]
    pii_mapping = decision.pii_mapping or {}

    return decision, body_to_persist, pii_mapping


def _handle_confirmation_negative(
    session_id: str,
    cand_tid: str,
    start_time: float,
    turn_span: Any,
    customer_verified: bool,
    tokenized_text: Optional[str],
    pii_mapping: Dict[str, str],
    idempotency_key: str,
) -> TriageResult:
    ack = "Understood. Your ticket remains open. Could you tell us more about what still needs attention?"
    elapsed_ms = (time.monotonic() - start_time) * 1000.0
    turn_span.set_attribute("decision", "auto_rag")
    turn_span.set_attribute("tier", "ticket_lifecycle")
    turn_span.set_attribute("queue", "none")
    turn_span.set_attribute("predicted_intent", "confirmation")

    no_dec = RouterDecision(
        ticket_id=cand_tid,
        session_id=session_id,
        decision="auto_rag",
        tier="ticket_lifecycle",
        queue="none",
        predicted_intent="confirmation",
        escalate=False,
        sentiment="neutral",
        urgency="normal",
        reason="confirmation_no_remains_open",
        degraded=False,
        calming_ack=ack,
        latency_ms=elapsed_ms,
        details={"category": "support", "ticket_id": cand_tid},
        customer_verified=customer_verified,
        tokenized_text=tokenized_text,
        pii_mapping=pii_mapping,
    )
    handoff_no = {
        "status": "AUTO_RESOLVED",
        "route": "confirmation_no",
        "answer": ack,
        "ticket_id": cand_tid,
        "status_code": 200,
    }
    _record_turn_final_span_attributes(turn_span, no_dec, handoff_no)
    return TriageResult(
        ticket_id=cand_tid,
        session_id=session_id,
        decision=no_dec,
        calming_ack=ack,
        handoff_response=handoff_no,
        idempotency_key=idempotency_key,
        latency_ms=elapsed_ms,
        status_code=200,
        error=None,
    )


def _find_candidate_confirmation_ticket(
    mcp_client: Any,
    session_id: str,
    ref_tid: Optional[str],
    ticket_customer_id: str,
) -> Optional[Dict[str, Any]]:
    cand_ticket: Optional[Dict[str, Any]] = None
    if ref_tid:
        try:
            cand_ticket = mcp_client.get_ticket(ref_tid)
        except Exception as exc:
            logger.debug("Failed looking up ref_tid %s: %s", ref_tid, exc)
            cand_ticket = None
    if not cand_ticket:
        try:
            cand_ticket = mcp_client.get_active_ticket_for_session(session_id=session_id)
        except Exception as exc:
            logger.debug("get_active_ticket_for_session failed: %s", exc)
            cand_ticket = None
    if not cand_ticket:
        try:
            cand_resp = mcp_client.get_pending_confirmation(
                session_id=session_id,
                customer_id=ticket_customer_id,
            )
            if isinstance(cand_resp, dict):
                cand_ticket = cand_resp.get("ticket") or cand_resp
        except Exception as exc:
            logger.debug("get_pending_confirmation failed: %s", exc)
            cand_ticket = None
    if not cand_ticket:
        try:
            tickets_dict = getattr(mcp_client, "tickets", None)
            if isinstance(tickets_dict, dict):
                for t in reversed(list(tickets_dict.values())):
                    t_sess = t.get("session_id")
                    if not t_sess and isinstance(t.get("decision"), dict):
                        t_sess = t["decision"].get("session_id")
                    if not t_sess and ":" in t.get("idempotency_key", ""):
                        t_sess = t["idempotency_key"].split(":", 1)[0]
                    if t_sess == session_id and (t.get("status") or "").lower() == "resolved_by_customer":
                        cand_ticket = t
                        break
        except Exception as exc:
            logger.debug("Inspecting tickets on fake mcp_client failed: %s", exc)
    return cand_ticket


def _handle_human_ticket_resolution_note(
    agent: Any,
    cand_ticket: Dict[str, Any],
    cand_tid: str,
    session_id: str,
    ticket_customer_id: str,
    message_text: str,
    traceparent: str,
    start_time: float,
    turn_span: Any,
    decision: RouterDecision,
    idempotency_key: str,
) -> TriageResult:
    cust_ticket_ref = _derive_customer_ticket_id(cand_tid, session_id, cand_ticket)
    try:
        agent.mcp_client.append_customer_followup(
            ticket_id=str(cand_ticket.get("ticket_id") or cand_tid),
            body_redacted=message_text,
            customer_id=cand_ticket.get("customer_id") or ticket_customer_id,
            traceparent=traceparent,
        )
    except Exception as exc:
        logger.warning("Failed appending resolution note to human ticket: %s", exc)

    ack = f"Thank you for letting us know! We have updated ticket #{cust_ticket_ref} with your note."
    elapsed_ms = (time.monotonic() - start_time) * 1000.0
    turn_span.set_attribute("decision", "auto_rag")
    turn_span.set_attribute("tier", "ticket_lifecycle")
    turn_span.set_attribute("queue", "none")
    turn_span.set_attribute("predicted_intent", "confirmation")

    human_note_dec = replace(
        decision,
        decision="auto_rag",
        tier="ticket_lifecycle",
        queue="none",
        predicted_intent="confirmation",
        escalate=False,
        sentiment="neutral",
        urgency="normal",
        reason="human_ticket_resolution_note",
        degraded=False,
        calming_ack=ack,
        latency_ms=elapsed_ms,
        details={"category": "support", "ticket_id": cust_ticket_ref},
    )
    agent.logger.log_decision(
        ticket_id=cust_ticket_ref,
        session_id=session_id,
        decision="auto_rag",
        tier="ticket_lifecycle",
        queue="none",
        degraded=False,
        latency_ms=elapsed_ms,
        traceparent=traceparent,
        predicted_intent="confirmation",
    )
    handoff_human_note = {
        "status": "AUTO_RESOLVED",
        "route": "human_ticket_note",
        "answer": ack,
        "ticket_id": cust_ticket_ref,
        "status_code": 200,
    }
    _record_turn_final_span_attributes(turn_span, human_note_dec, handoff_human_note)
    return TriageResult(
        ticket_id=cust_ticket_ref,
        session_id=session_id,
        decision=human_note_dec,
        calming_ack=ack,
        handoff_response=handoff_human_note,
        idempotency_key=idempotency_key,
        latency_ms=elapsed_ms,
        status_code=200,
        error=None,
    )


def _filter_candidate_confirmation_ticket(
    cand_ticket: Optional[Dict[str, Any]],
    is_affirmative: bool,
    conv_state: ConversationState,
    ref_tid: Optional[str],
    session_id: str,
    ticket_customer_id: str,
) -> Optional[Dict[str, Any]]:
    if not cand_ticket:
        return None
    cand_st = str(cand_ticket.get("status") or "").lower()
    if (
        is_affirmative
        and conv_state.pending_confirmation is None
        and not cand_ticket.get("resolution_confirmation_requested")
        and cand_st != "resolved_by_customer"
    ):
        return None

    ticket_sess = cand_ticket.get("session_id")
    if not ticket_sess and isinstance(cand_ticket.get("decision"), dict):
        ticket_sess = cand_ticket["decision"].get("session_id")
    if not ref_tid and ticket_sess and ticket_sess != session_id:
        return None

    if ticket_customer_id:
        ticket_cust = cand_ticket.get("customer_id")
        if ticket_cust and ticket_cust != ticket_customer_id:
            return None
    return cand_ticket


def _handle_unverified_confirmation_refusal(
    cand_tid: str,
    session_id: str,
    start_time: float,
    turn_span: Any,
    tokenized_text: Optional[str],
    pii_mapping: Dict[str, str],
    idempotency_key: str,
) -> TriageResult:
    anon_refusal = f"To close ticket #{cand_tid}, please sign in with your verified customer account."
    elapsed_ms = (time.monotonic() - start_time) * 1000.0
    anon_dec = RouterDecision(
        ticket_id=cand_tid,
        session_id=session_id,
        decision="auto_rag",
        tier="ticket_lifecycle",
        queue="none",
        predicted_intent="confirmation",
        escalate=False,
        sentiment="neutral",
        urgency="normal",
        reason="unverified_customer_confirmation_refusal",
        degraded=False,
        calming_ack=anon_refusal,
        latency_ms=elapsed_ms,
        details={"category": "support", "ticket_id": cand_tid},
        customer_verified=False,
        tokenized_text=tokenized_text,
        pii_mapping=pii_mapping,
    )
    handoff_forbid = {
        "status": "FORBIDDEN",
        "answer": anon_refusal,
        "ticket_id": cand_tid,
        "status_code": 403,
        "error": "anonymous_confirmation_forbidden",
    }
    _record_turn_final_span_attributes(turn_span, anon_dec, handoff_forbid)
    return TriageResult(
        ticket_id=cand_tid,
        session_id=session_id,
        decision=anon_dec,
        calming_ack=anon_refusal,
        handoff_response=handoff_forbid,
        idempotency_key=idempotency_key,
        latency_ms=elapsed_ms,
        status_code=200,
        error=None,
    )


def _handle_unconditional_resolution_prompt(
    agent: Any,
    cand_ticket: Dict[str, Any],
    cand_tid: str,
    session_id: str,
    decision: RouterDecision,
    start_time: float,
    turn_span: Any,
    traceparent: str,
    idempotency_key: str,
) -> TriageResult:
    cust_ticket_ref = _derive_customer_ticket_id(cand_tid, session_id, cand_ticket)
    prompt_q = f"Did that resolve your issue? Reply yes to close ticket #{cust_ticket_ref}."
    try:
        agent.mcp_client.record_resolution_confirmation_requested(
            ticket_id=str(cand_ticket.get("ticket_id") or cand_tid),
            prompt=prompt_q,
        )
    except Exception as exc:
        logger.warning("Failed to record confirmation requested: %s", exc)
    ack = f"Great to hear! Would you like me to close ticket #{cust_ticket_ref} as resolved?"
    elapsed_ms = (time.monotonic() - start_time) * 1000.0
    turn_span.set_attribute("decision", "auto_rag")
    turn_span.set_attribute("tier", "ticket_lifecycle")
    turn_span.set_attribute("queue", "none")
    turn_span.set_attribute("predicted_intent", "confirmation")

    uncond_dec = replace(
        decision,
        decision="auto_rag",
        tier="ticket_lifecycle",
        queue="none",
        predicted_intent="confirmation",
        escalate=False,
        sentiment="neutral",
        urgency="normal",
        reason="resolution_confirmation_prompt",
        degraded=False,
        calming_ack=ack,
        latency_ms=elapsed_ms,
        details={"category": "support", "ticket_id": cust_ticket_ref},
    )
    agent.logger.log_decision(
        ticket_id=cust_ticket_ref,
        session_id=session_id,
        decision="auto_rag",
        tier="ticket_lifecycle",
        queue="none",
        degraded=False,
        latency_ms=elapsed_ms,
        traceparent=traceparent,
        predicted_intent="confirmation",
    )
    handoff_uncond = {
        "status": "AUTO_RESOLVED",
        "route": "confirmation_prompt",
        "answer": ack,
        "ticket_id": cust_ticket_ref,
        "confirmation_question": prompt_q,
        "status_code": 200,
    }
    _record_turn_final_span_attributes(turn_span, uncond_dec, handoff_uncond)
    return TriageResult(
        ticket_id=cust_ticket_ref,
        session_id=session_id,
        decision=uncond_dec,
        calming_ack=ack,
        handoff_response=handoff_uncond,
        idempotency_key=idempotency_key,
        latency_ms=elapsed_ms,
        status_code=200,
        error=None,
    )


def _handle_verified_customer_closure(
    agent: Any,
    cand_ticket: Dict[str, Any],
    cand_tid: str,
    cand_status: str,
    session_id: str,
    ticket_customer_id: str,
    idempotency_key: str,
    traceparent: str,
    start_time: float,
    turn_span: Any,
    tokenized_text: Optional[str],
    pii_mapping: Dict[str, str],
) -> Optional[TriageResult]:
    try:
        if cand_status != "resolved_by_customer":
            agent.mcp_client.update_ticket_status(
                ticket_id=str(cand_ticket.get("ticket_id") or cand_tid),
                from_status=cand_status,
                to_status="resolved_by_customer",
                actor=ticket_customer_id,
                reason="customer_confirmed_resolution",
                idempotency_key=idempotency_key,
                trace=traceparent,
                customer_id=ticket_customer_id,
            )
        ack = f"Thank you for confirming! Ticket #{cand_tid} has been marked as resolved. Please let us know if you need help with anything else!"
        elapsed_ms = (time.monotonic() - start_time) * 1000.0
        turn_span.set_attribute("decision", "auto_rag")
        turn_span.set_attribute("tier", "ticket_lifecycle")
        turn_span.set_attribute("queue", "none")
        turn_span.set_attribute("predicted_intent", "confirmation")

        res_dec = RouterDecision(
            ticket_id=cand_tid,
            session_id=session_id,
            decision="auto_rag",
            tier="ticket_lifecycle",
            queue="none",
            predicted_intent="confirmation",
            escalate=False,
            sentiment="neutral",
            urgency="normal",
            reason="resolved_by_customer",
            degraded=False,
            calming_ack=ack,
            latency_ms=elapsed_ms,
            details={"category": "support", "resolved_ticket_id": cand_tid},
            customer_verified=True,
            tokenized_text=tokenized_text,
            pii_mapping=pii_mapping,
        )
        agent.logger.log_decision(
            ticket_id=cand_tid,
            session_id=session_id,
            decision="auto_rag",
            tier="ticket_lifecycle",
            queue="none",
            degraded=False,
            latency_ms=elapsed_ms,
            traceparent=traceparent,
            predicted_intent="confirmation",
        )
        handoff_resolved = {
            "status": "RESOLVED",
            "route": "resolved_by_customer",
            "answer": ack,
            "ticket_id": cand_tid,
            "status_code": 200,
        }
        _record_turn_final_span_attributes(turn_span, res_dec, handoff_resolved)
        return TriageResult(
            ticket_id=cand_tid,
            session_id=session_id,
            decision=res_dec,
            calming_ack=ack,
            handoff_response=handoff_resolved,
            idempotency_key=idempotency_key,
            latency_ms=elapsed_ms,
            status_code=200,
            error=None,
        )
    except Exception as exc:
        logger.warning(
            json.dumps(
                {
                    "event": "update_ticket_status_failed",
                    "ticket_id": cand_tid,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                separators=(",", ":"),
            )
        )
        return None


def _handle_bot_ticket_confirmation(
    agent: Any,
    cand_ticket: Dict[str, Any],
    decision: RouterDecision,
    is_affirmative: bool,
    is_uncond: bool,
    session_id: str,
    ticket_customer_id: str,
    customer_verified: bool,
    tokenized_text: Optional[str],
    pii_mapping: Dict[str, str],
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    traceparent: str,
) -> Optional[Tuple[TriageResult, RouterDecision]]:
    cand_tid = str(cand_ticket.get("issue_reference") or cand_ticket.get("ticket_id") or "")
    cand_cat = cand_ticket.get("category", "support")
    cand_owner = cand_ticket.get("current_owner", "bot")
    cand_status = cand_ticket.get("status", "open")

    if cand_cat in ("security", "fraud") or cand_owner != "bot":
        return None

    if not customer_verified or not ticket_customer_id or ticket_customer_id.startswith("anon:"):
        res = _handle_unverified_confirmation_refusal(
            cand_tid=cand_tid,
            session_id=session_id,
            start_time=start_time,
            turn_span=turn_span,
            tokenized_text=tokenized_text,
            pii_mapping=pii_mapping,
            idempotency_key=idempotency_key,
        )
        return res, decision

    if is_uncond and not is_affirmative:
        res = _handle_unconditional_resolution_prompt(
            agent=agent,
            cand_ticket=cand_ticket,
            cand_tid=cand_tid,
            session_id=session_id,
            decision=decision,
            start_time=start_time,
            turn_span=turn_span,
            traceparent=traceparent,
            idempotency_key=idempotency_key,
        )
        return res, decision

    closure_res = _handle_verified_customer_closure(
        agent=agent,
        cand_ticket=cand_ticket,
        cand_tid=cand_tid,
        cand_status=cand_status,
        session_id=session_id,
        ticket_customer_id=ticket_customer_id,
        idempotency_key=idempotency_key,
        traceparent=traceparent,
        start_time=start_time,
        turn_span=turn_span,
        tokenized_text=tokenized_text,
        pii_mapping=pii_mapping,
    )
    if closure_res is not None:
        return closure_res, decision

    return None


def _process_customer_confirmation(
    agent: Any,
    decision: RouterDecision,
    is_conf_no: bool,
    is_affirmative: bool,
    is_uncond: bool,
    t_id: str,
    ref_tid: Optional[str],
    session_id: str,
    ticket_customer_id: str,
    customer_verified: bool,
    message_text: str,
    tokenized_text: Optional[str],
    pii_mapping: Dict[str, str],
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    traceparent: str,
    conv_state: ConversationState,
) -> Tuple[Optional[TriageResult], RouterDecision]:
    """Process customer confirmation yes/no or resolution report (REQ-LIFE-002, REQ-SAFE-004, REQ-TURN-005)."""
    if is_conf_no:
        cand_tid = t_id
        if conv_state.pending_confirmation:
            cand_tid = conv_state.pending_confirmation.issue_reference or conv_state.pending_confirmation.ticket_uuid
        res = _handle_confirmation_negative(
            session_id=session_id,
            cand_tid=cand_tid,
            start_time=start_time,
            turn_span=turn_span,
            customer_verified=customer_verified,
            tokenized_text=tokenized_text,
            pii_mapping=pii_mapping,
            idempotency_key=idempotency_key,
        )
        return res, decision

    cand_ticket = _find_candidate_confirmation_ticket(
        mcp_client=agent.mcp_client,
        session_id=session_id,
        ref_tid=ref_tid,
        ticket_customer_id=ticket_customer_id,
    )
    if isinstance(cand_ticket, dict):
        cand_tid = str(cand_ticket.get("issue_reference") or cand_ticket.get("ticket_id") or cand_ticket.get("id") or "")
        cand_queue = str(cand_ticket.get("assigned_queue") or cand_ticket.get("queue") or "")
        cand_owner = cand_ticket.get("current_owner", "bot")
        if is_human_owned(cand_ticket) or cand_queue.startswith("human") or cand_owner != "bot":
            res = _handle_human_ticket_resolution_note(
                agent=agent,
                cand_ticket=cand_ticket,
                cand_tid=cand_tid,
                session_id=session_id,
                ticket_customer_id=ticket_customer_id,
                message_text=message_text,
                traceparent=traceparent,
                start_time=start_time,
                turn_span=turn_span,
                decision=decision,
                idempotency_key=idempotency_key,
            )
            return res, decision

    cand_ticket = _filter_candidate_confirmation_ticket(
        cand_ticket=cand_ticket,
        is_affirmative=is_affirmative,
        conv_state=conv_state,
        ref_tid=ref_tid,
        session_id=session_id,
        ticket_customer_id=ticket_customer_id,
    )

    if not isinstance(cand_ticket, dict) and decision.tier == "ticket_lifecycle":
        fallback_tier = "tier1_router_s1" if decision.details.get("s1_prediction") else "auto_rag"
        decision = replace(decision, tier=fallback_tier, reason="unconfirmed_affirmative_ignored")

    if isinstance(cand_ticket, dict):
        bot_res = _handle_bot_ticket_confirmation(
            agent=agent,
            cand_ticket=cand_ticket,
            decision=decision,
            is_affirmative=is_affirmative,
            is_uncond=is_uncond,
            session_id=session_id,
            ticket_customer_id=ticket_customer_id,
            customer_verified=customer_verified,
            tokenized_text=tokenized_text,
            pii_mapping=pii_mapping,
            turn_span=turn_span,
            start_time=start_time,
            idempotency_key=idempotency_key,
            traceparent=traceparent,
        )
        if bot_res is not None:
            return bot_res

    return None, decision


def _lookup_active_holding_ticket(
    mcp_client: Any,
    session_id: str,
    ref_tid: Optional[str],
) -> Optional[Dict[str, Any]]:
    active_ticket: Optional[Dict[str, Any]] = None
    try:
        active_ticket = mcp_client.get_active_ticket_for_session(session_id=session_id)
    except Exception as exc:
        logger.warning(
            json.dumps(
                {
                    "event": "get_active_ticket_failed",
                    "session_id": session_id,
                    "error_type": type(exc).__name__,
                },
                separators=(",", ":"),
            )
        )

    if active_ticket is None and ref_tid:
        try:
            active_ticket = mcp_client.get_ticket(ticket_id=ref_tid)
        except Exception:
            active_ticket = None
    return active_ticket


_ACTIONABLE_INTENTS = frozenset({
    "order_status",
    "order_inquiry",
    "order_lookup",
    "returns_refunds",
    "shipping_policy",
    "store_hours",
    "account_access",
    "payment_methods",
    "cancellation",
    "warranty",
    "price_match",
})


def _is_followup_continuation_allowed(
    is_explicit_ref: bool,
    cur_intent: str,
    held_intent: Optional[str],
    turn_act: Optional[str],
    is_held: bool,
    tier: str,
) -> bool:
    if is_explicit_ref:
        return True
    if cur_intent in ("followup", "other"):
        return (
            turn_act == "followup_to_active_ticket"
            or tier == "human_holding"
            or (is_held and turn_act != "new_request")
        )
    if held_intent and cur_intent == held_intent:
        return True
    if cur_intent in _ACTIONABLE_INTENTS:
        return False
    return False


def _build_holding_followup_result(
    agent: Any,
    active_ticket: Dict[str, Any],
    customer_ticket_id: str,
    holding_tid: str,
    holding_queue: str,
    cur_intent: str,
    session_id: str,
    start_time: float,
    turn_span: Any,
    decision: RouterDecision,
    tokenized_text: Optional[str],
    pii_mapping: Dict[str, str],
    traceparent: str,
    idempotency_key: str,
) -> TriageResult:
    calming_ack = render_followup_calming_reply(
        ticket_id=customer_ticket_id,
        session_id=session_id,
    )
    calming_ack = restore_pii_tokens(calming_ack, pii_mapping)
    elapsed_ms = (time.monotonic() - start_time) * 1000.0

    turn_span.set_attribute("decision", "escalate")
    turn_span.set_attribute("tier", "human_holding")
    turn_span.set_attribute("queue", holding_queue)
    turn_span.set_attribute("degraded", False)
    turn_span.set_attribute("classifier_predicted_intent", cur_intent)
    turn_span.set_attribute("predicted_intent", "followup")

    details_followup = {
        "holding_ticket_id": customer_ticket_id,
        "holding_ticket_uuid": holding_tid,
        "queue": holding_queue,
        "classifier_predicted_intent": cur_intent,
    }
    if isinstance(decision.details, dict) and "s1_prediction" in decision.details:
        details_followup["s1_prediction"] = decision.details["s1_prediction"]

    followup_decision = RouterDecision(
        ticket_id=customer_ticket_id,
        session_id=session_id,
        decision="escalate",
        tier="human_holding",
        queue=holding_queue,
        predicted_intent="followup",
        escalate=True,
        sentiment="neutral",
        urgency="normal",
        reason="multi_turn_holding",
        degraded=False,
        calming_ack=calming_ack,
        latency_ms=elapsed_ms,
        details=details_followup,
        customer_verified=bool(active_ticket.get("customer_verified", False)),
        tokenized_text=tokenized_text,
        pii_mapping=pii_mapping,
    )
    agent.logger.log_decision(
        ticket_id=customer_ticket_id,
        session_id=session_id,
        decision="escalate",
        tier="human_holding",
        queue=holding_queue,
        degraded=False,
        latency_ms=elapsed_ms,
        traceparent=traceparent,
        predicted_intent="followup",
    )
    handoff_holding = {
        "status": "ESCALATED",
        "route": "followup",
        "answer": calming_ack,
        "ticket_id": customer_ticket_id,
        "holding_ticket_uuid": holding_tid,
        "queue": holding_queue,
        "handoff_done": True,
    }
    _record_turn_final_span_attributes(turn_span, followup_decision, handoff_holding)
    return TriageResult(
        ticket_id=customer_ticket_id,
        session_id=session_id,
        decision=followup_decision,
        calming_ack=calming_ack,
        handoff_response=handoff_holding,
        idempotency_key=idempotency_key,
        latency_ms=elapsed_ms,
        status_code=200,
        error=None,
    )


def _process_active_ticket_followup(
    agent: Any,
    decision: RouterDecision,
    ref_tid: Optional[str],
    message_text: str,
    tokenized_text: Optional[str],
    sanitized_text: Optional[str],
    pii_mapping: Dict[str, str],
    session_id: str,
    placeholder_cid: str,
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    traceparent: str,
) -> Optional[TriageResult]:
    """Process follow-up to active human holding ticket (REQ-AGENT-033, REQ-LIFE-001)."""
    active_ticket = _lookup_active_holding_ticket(agent.mcp_client, session_id, ref_tid)
    if not (isinstance(active_ticket, dict) and (active_ticket.get("ticket_id") or active_ticket.get("id"))):
        return None

    holding_tid = str(active_ticket.get("ticket_id") or active_ticket.get("id"))
    holding_queue = str(
        active_ticket.get("assigned_queue")
        or active_ticket.get("queue")
        or "human-standard"
    )
    holding_cat = effective_category(active_ticket)
    raw_owner = active_ticket.get("current_owner")
    if raw_owner:
        holding_owner = str(raw_owner)
    elif is_human_owned(active_ticket) or holding_queue.startswith("human"):
        holding_owner = f"queue:{holding_queue}"
    else:
        holding_owner = "bot"
    customer_ticket_id = _derive_customer_ticket_id(holding_tid, session_id, active_ticket)

    held_dec = active_ticket.get("decision")
    if isinstance(held_dec, str):
        try:
            held_dec = json.loads(held_dec)
        except Exception:
            held_dec = None
    held_intent = held_dec.get("predicted_intent") if isinstance(held_dec, dict) else None
    cur_intent = decision.predicted_intent

    is_held = (
        is_holding_ticket(
            status=active_ticket.get("status"),
            assigned_queue=holding_queue,
            category=holding_cat,
            current_owner=holding_owner,
            decision=active_ticket.get("decision"),
        )
        and holding_cat not in ("security", "fraud")
        and is_human_owned(active_ticket)
    )

    is_explicit_ref = bool((ref_tid and ref_tid == holding_tid) or _is_explicit_ticket_reference(message_text))
    should_continue = _is_followup_continuation_allowed(
        is_explicit_ref=is_explicit_ref,
        cur_intent=cur_intent,
        held_intent=held_intent,
        turn_act=decision.details.get("turn_act"),
        is_held=is_held,
        tier=decision.tier,
    )

    if not (should_continue and is_held):
        return None

    body_to_persist = tokenized_text or sanitized_text or message_text
    ticket_customer_id = active_ticket.get("customer_id") or placeholder_cid

    if pii_mapping:
        agent._save_redaction_map_if_needed(
            ticket_id=customer_ticket_id,
            ticket_uuid=holding_tid,
            customer_id=ticket_customer_id,
            mapping=pii_mapping,
        )
    try:
        agent.mcp_client.append_customer_followup(
            ticket_id=holding_tid,
            body_redacted=body_to_persist,
            customer_id=ticket_customer_id,
            traceparent=traceparent,
        )
    except Exception as exc:
        logger.warning(
            json.dumps(
                {
                    "event": "append_customer_followup_failed",
                    "ticket_id": holding_tid,
                    "error_type": type(exc).__name__,
                },
                separators=(",", ":"),
            )
        )

    return _build_holding_followup_result(
        agent=agent,
        active_ticket=active_ticket,
        customer_ticket_id=customer_ticket_id,
        holding_tid=holding_tid,
        holding_queue=holding_queue,
        cur_intent=cur_intent,
        session_id=session_id,
        start_time=start_time,
        turn_span=turn_span,
        decision=decision,
        tokenized_text=tokenized_text,
        pii_mapping=pii_mapping,
        traceparent=traceparent,
        idempotency_key=idempotency_key,
    )


def _build_conversation_history_text(
    agent: Any,
    verified_cid: str,
    message_text: str,
) -> Tuple[str, bool, int]:
    raw_memory = ""
    try:
        raw_memory = agent.memory_client.retrieve_summary(
            customer_id=verified_cid,
            query=message_text,
            timeout_s=HISTORY_MEMORY_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning("Memory Bank retrieve failed open: %s", exc)
        raw_memory = ""

    memory_found = bool(raw_memory and raw_memory.strip())
    if memory_found:
        cleaned_memory = re.sub(r"\[[A-Z0-9_]+\]", "(redacted)", raw_memory.strip())
        mem_section = f"Summary of previous interactions:\n{cleaned_memory}"
    else:
        mem_section = "I don't have any saved history from earlier conversations."

    recent_tickets: list[Dict[str, Any]] = []
    try:
        recent_tickets = agent.mcp_client.list_customer_tickets(
            customer_id=verified_cid,
            limit=5,
            customer_verified=True,
        )
    except Exception as exc:
        logger.warning("list_customer_tickets failed: %s", exc)
        recent_tickets = []

    ticket_count = len(recent_tickets)
    sections = [mem_section]
    if recent_tickets:
        ticket_lines = []
        seen_refs: set[str] = set()
        for tk in recent_tickets:
            ref = tk.get("issue_reference") or "TICK-UNKNOWN"
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            cat = tk.get("category", "support")
            st = "under review" if cat in ("security", "fraud") else tk.get("status", "open")
            ticket_lines.append(f"- #{ref} (status: {st})")
        sections.append("Recent support tickets:\n" + "\n".join(ticket_lines))

    sections.append("Full chat transcripts are kept in the Gemini Enterprise app's own chat history.")
    reply_text = "\n\n".join(sections)
    return reply_text, memory_found, ticket_count


def _build_conversation_history_result(
    agent: Any,
    decision: RouterDecision,
    session_id: str,
    t_id: str,
    traceparent: str,
    trace_id: str,
    customer_verified: bool,
    outcome: str,
    reply_text: str,
    memory_found: bool,
    ticket_count: int,
    start_time: float,
    turn_span: Any,
    idempotency_key: str,
) -> TriageResult:
    turn_span.set_attribute("decision", "auto_rag")
    turn_span.set_attribute("tier", "tier0_rules")
    turn_span.set_attribute("queue", "none")
    turn_span.set_attribute("degraded", False)
    turn_span.set_attribute("predicted_intent", "conversation_history_access")

    logger.info(
        json.dumps(
            {
                "event": "conversation_history_access",
                "trace_id": trace_id,
                "session_id": session_id,
                "customer_verified": bool(customer_verified),
                "outcome": outcome,
                "memory_found": bool(memory_found),
                "ticket_count": int(ticket_count),
            },
            separators=(",", ":"),
        )
    )

    elapsed_ms = (time.monotonic() - start_time) * 1000.0
    hist_details = {"category": "conversation_history", "outcome": outcome}
    if isinstance(decision.details, dict) and "s1_prediction" in decision.details:
        hist_details["s1_prediction"] = decision.details["s1_prediction"]

    hist_dec = replace(
        decision,
        decision="auto_rag",
        tier="tier0_rules",
        queue="none",
        predicted_intent="conversation_history_access",
        escalate=False,
        reason="conversation_history_access",
        degraded=False,
        calming_ack=reply_text,
        latency_ms=elapsed_ms,
        details=hist_details,
    )
    agent.logger.log_decision(
        ticket_id=t_id,
        session_id=session_id,
        decision="auto_rag",
        tier="tier0_rules",
        queue="none",
        degraded=False,
        latency_ms=elapsed_ms,
        traceparent=traceparent,
        predicted_intent="conversation_history_access",
    )
    handoff_hist = {
        "status": "AUTO_RESOLVED",
        "route": "conversation_history",
        "answer": reply_text,
        "ticket_id": t_id,
        "status_code": 200,
        "outcome": outcome,
    }
    _record_turn_final_span_attributes(turn_span, hist_dec, handoff_hist)
    return TriageResult(
        ticket_id=t_id,
        session_id=session_id,
        decision=hist_dec,
        calming_ack=reply_text,
        handoff_response=handoff_hist,
        idempotency_key=idempotency_key,
        latency_ms=elapsed_ms,
        status_code=200,
        error=None,
    )


def _process_conversation_history(
    agent: Any,
    decision: RouterDecision,
    message_text: str,
    session_id: str,
    t_id: str,
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    traceparent: str,
    customer_verified: bool,
    cust_ctx: CustomerContext,
) -> TriageResult:
    """Process conversation history access request (REQ-HIST-001, REQ-HIST-002)."""
    parts_trace = traceparent.split("-")
    trace_id = parts_trace[1] if len(parts_trace) >= 2 else ""

    verified_cid = (
        cust_ctx.customer_id
        if (customer_verified and cust_ctx.customer_id and not str(cust_ctx.customer_id).startswith("anon:"))
        else None
    )

    ti_sec = (decision.details.get("turn_interpretation") or {}).get("security") or {}
    is_cross_cust = bool(
        ti_sec.get("cross_customer_data_request")
        or decision.details.get("security_flags", {}).get("cross_customer_data_request")
    )
    if is_cross_cust:
        outcome = "cross_customer_refused"
        reply_text = (
            "You can only view your own conversation history. Access to other users' conversations is not permitted."
        )
        memory_found = False
        ticket_count = 0
    elif not customer_verified or not verified_cid:
        outcome = "signin_required"
        reply_text = (
            "Please sign in with your verified customer account to view your conversation history."
        )
        memory_found = False
        ticket_count = 0
    else:
        outcome = "served"
        reply_text, memory_found, ticket_count = _build_conversation_history_text(
            agent=agent,
            verified_cid=verified_cid,
            message_text=message_text,
        )

    return _build_conversation_history_result(
        agent=agent,
        decision=decision,
        session_id=session_id,
        t_id=t_id,
        traceparent=traceparent,
        trace_id=trace_id,
        customer_verified=customer_verified,
        outcome=outcome,
        reply_text=reply_text,
        memory_found=memory_found,
        ticket_count=ticket_count,
        start_time=start_time,
        turn_span=turn_span,
        idempotency_key=idempotency_key,
    )


def _handle_policy_security_blocked(
    agent: Any,
    decision: RouterDecision,
    message_text: str,
    session_id: str,
    t_id: str,
    channel: str,
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    traceparent: str,
    customer_verified: bool,
    ticket_customer_id: str,
    ma_verdict: Optional[ModelArmorVerdict],
    oauth_access_token: Optional[str],
    caller_user_id: Optional[str],
) -> Tuple[TriageResult, RouterDecision]:
    sec_ack = render_security_blocked_reply()
    eff_channel = derive_channel_source(
        channel=channel,
        oauth_access_token=oauth_access_token,
        caller_user_id=caller_user_id,
        session_id=session_id,
    )
    _emit_security_blocked_log(
        traceparent=traceparent,
        session_id=session_id,
        channel=eff_channel,
        customer_verified=customer_verified,
        customer_id=ticket_customer_id,
        ma_verdict=ma_verdict,
        message_text=message_text,
    )
    elapsed_ms = (time.monotonic() - start_time) * 1000.0
    handoff_sec_eval = {
        "status": "SECURITY_BLOCKED",
        "route": "security_blocked",
        "answer": sec_ack,
        "ticket_id": t_id,
        "queue": "none",
        "status_code": 200,
    }
    _record_turn_final_span_attributes(turn_span, decision, handoff_sec_eval)
    return (
        TriageResult(
            ticket_id=t_id,
            session_id=session_id,
            decision=decision,
            calming_ack=sec_ack,
            handoff_response=handoff_sec_eval,
            idempotency_key=idempotency_key,
            latency_ms=elapsed_ms,
            status_code=200,
            error=None,
        ),
        decision,
    )


def _handle_security_intents_refusal(
    agent: Any,
    decision: RouterDecision,
    sec_intent_name: str,
    sec_reply: str,
    message_text: str,
    session_id: str,
    t_id: str,
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    traceparent: str,
    customer_verified: bool,
) -> Tuple[TriageResult, RouterDecision]:
    trace_id_val = traceparent.split("-")[1] if traceparent and "-" in traceparent else (traceparent or "unknown")
    emit_security_refusal_log(
        trace_id=trace_id_val,
        session_id=session_id,
        customer_verified=customer_verified,
        reason=sec_intent_name,
        message_text=message_text,
    )
    elapsed_ms = (time.monotonic() - start_time) * 1000.0
    turn_span.set_attribute("decision", "auto_rag")
    turn_span.set_attribute("tier", decision.tier)
    turn_span.set_attribute("queue", "none")
    turn_span.set_attribute("degraded", False)
    turn_span.set_attribute("predicted_intent", sec_intent_name)

    sec_decision = replace(
        decision,
        decision="auto_rag",
        tier=decision.tier,
        queue="none",
        predicted_intent=sec_intent_name,
        escalate=False,
        sentiment="neutral",
        urgency="normal",
        reason=f"security_refusal_{sec_intent_name}",
        degraded=False,
        calming_ack=sec_reply,
        latency_ms=elapsed_ms,
        details={"category": "security_refusal", "security_intent": sec_intent_name},
    )
    agent.logger.log_decision(
        ticket_id=t_id,
        session_id=session_id,
        decision="auto_rag",
        tier=decision.tier,
        queue="none",
        degraded=False,
        latency_ms=elapsed_ms,
        traceparent=traceparent,
        predicted_intent=sec_intent_name,
    )
    handoff_sec = {
        "status": "AUTO_RESOLVED",
        "route": "security_refusal",
        "outcome": "cross_customer_refused" if sec_intent_name == "cross_customer_data_request" else sec_intent_name,
        "answer": sec_reply,
        "ticket_id": t_id,
        "queue": "none",
        "status_code": 200,
    }
    _record_turn_final_span_attributes(turn_span, sec_decision, handoff_sec)
    return (
        TriageResult(
            ticket_id=t_id,
            session_id=session_id,
            decision=sec_decision,
            calming_ack=sec_reply,
            handoff_response=handoff_sec,
            idempotency_key=idempotency_key,
            latency_ms=elapsed_ms,
            status_code=200,
            error=None,
        ),
        sec_decision,
    )


def _handle_conditional_resolution(
    agent: Any,
    decision: RouterDecision,
    session_id: str,
    t_id: str,
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    traceparent: str,
) -> Tuple[TriageResult, RouterDecision]:
    cond_ack = decision.calming_ack or "Understood! Please keep us posted once your package arrives, or let us know if anything else comes up."
    elapsed_ms = (time.monotonic() - start_time) * 1000.0
    turn_span.set_attribute("decision", "auto_rag")
    turn_span.set_attribute("tier", "ticket_lifecycle")
    turn_span.set_attribute("queue", "none")
    turn_span.set_attribute("predicted_intent", "status_inquiry")

    cond_decision = replace(
        decision,
        decision="auto_rag",
        tier="ticket_lifecycle",
        queue="none",
        predicted_intent="status_inquiry",
        escalate=False,
        sentiment="neutral",
        urgency="normal",
        reason="conditional_resolution_acknowledgement",
        degraded=False,
        calming_ack=cond_ack,
        latency_ms=elapsed_ms,
        details={"category": "support", "condition_detected": True},
    )
    agent.logger.log_decision(
        ticket_id=t_id,
        session_id=session_id,
        decision="auto_rag",
        tier="ticket_lifecycle",
        queue="none",
        degraded=False,
        latency_ms=elapsed_ms,
        traceparent=traceparent,
        predicted_intent="status_inquiry",
    )
    handoff_cond = {
        "status": "AUTO_RESOLVED",
        "route": "conditional_resolution",
        "answer": cond_ack,
        "ticket_id": t_id,
        "status_code": 200,
    }
    _record_turn_final_span_attributes(turn_span, cond_decision, handoff_cond)
    return (
        TriageResult(
            ticket_id=t_id,
            session_id=session_id,
            decision=cond_decision,
            calming_ack=cond_ack,
            handoff_response=handoff_cond,
            idempotency_key=idempotency_key,
            latency_ms=elapsed_ms,
            status_code=200,
            error=None,
        ),
        cond_decision,
    )


def _handle_security_ticket_status_inquiry(
    agent: Any,
    ref_tid: str,
    session_id: str,
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    customer_verified: bool,
    tokenized_text: Optional[str],
    pii_mapping: Dict[str, str],
) -> Optional[Tuple[TriageResult, RouterDecision]]:
    try:
        ref_ticket = agent.mcp_client.get_ticket(ticket_id=ref_tid)
    except Exception:
        ref_ticket = None

    if not isinstance(ref_ticket, dict):
        return None

    ref_cat = ref_ticket.get("category")
    ref_dec = ref_ticket.get("decision")
    if isinstance(ref_dec, str):
        try:
            ref_dec = json.loads(ref_dec)
        except Exception:
            ref_dec = None
    ref_reason = ref_dec.get("reason") if isinstance(ref_dec, dict) else None
    ref_tier = ref_dec.get("tier") if isinstance(ref_dec, dict) else None
    if ref_cat == "security" or ref_reason == "model_armor_security_trigger" or ref_tier == "model_armor":
        safe_ack = f"Ticket #{ref_tid} is currently under internal review with our security team."
        elapsed_ms = (time.monotonic() - start_time) * 1000.0
        turn_span.set_attribute("decision", "escalate")
        turn_span.set_attribute("tier", "ticket_status")
        turn_span.set_attribute("queue", "none")
        turn_span.set_attribute("predicted_intent", "status_inquiry")

        status_decision = RouterDecision(
            ticket_id=ref_tid,
            session_id=session_id,
            decision="escalate",
            tier="ticket_status",
            queue="none",
            predicted_intent="status_inquiry",
            escalate=False,
            sentiment="neutral",
            urgency="normal",
            reason="security_ticket_status_inquiry",
            degraded=False,
            calming_ack=safe_ack,
            latency_ms=elapsed_ms,
            details={
                "category": "security",
                "referenced_ticket_id": ref_tid,
            },
            customer_verified=customer_verified,
            tokenized_text=tokenized_text,
            pii_mapping=pii_mapping,
        )
        handoff_status_inq = {
            "status": "STATUS_INQUIRY",
            "route": "ticket_status",
            "answer": safe_ack,
            "ticket_id": ref_tid,
            "queue": "none",
            "status_code": 200,
        }
        _record_turn_final_span_attributes(turn_span, status_decision, handoff_status_inq)
        return (
            TriageResult(
                ticket_id=ref_tid,
                session_id=session_id,
                decision=status_decision,
                calming_ack=safe_ack,
                handoff_response=handoff_status_inq,
                idempotency_key=idempotency_key,
                latency_ms=elapsed_ms,
                status_code=200,
                error=None,
            ),
            status_decision,
        )
    return None


def _detect_and_handle_security_intent(
    agent: Any,
    decision: RouterDecision,
    message_text: str,
    session_id: str,
    t_id: str,
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    traceparent: str,
    customer_verified: bool,
) -> Optional[Tuple[TriageResult, RouterDecision]]:
    sec_flags = (decision.details.get("turn_interpretation") or {}).get("security") or {}
    sec_intent = None
    if sec_flags.get("cross_customer_data_request") or decision.predicted_intent == "cross_customer_data_request":
        sec_intent = "cross_customer_data_request"
    elif sec_flags.get("authorization_bypass_attempt") or decision.predicted_intent == "authorization_bypass_attempt":
        sec_intent = "authorization_bypass_attempt"
    elif decision.reason.startswith("security_refusal"):
        sec_intent = decision.predicted_intent

    if not sec_intent:
        return None

    sec_intent_name = sec_intent[0] if isinstance(sec_intent, tuple) else (
        sec_intent if isinstance(sec_intent, str) else decision.predicted_intent
    )
    sec_reply = (
        decision.calming_ack
        or (sec_intent[1] if isinstance(sec_intent, tuple) else get_security_intent_reply(sec_intent_name, message_text))
        or (ORDER_CROSS_CUSTOMER_REFUSAL if sec_intent_name == "cross_customer_data_request" else AUTH_BYPASS_REFUSAL)
    )
    return _handle_security_intents_refusal(
        agent=agent,
        decision=decision,
        sec_intent_name=sec_intent_name,
        sec_reply=sec_reply,
        message_text=message_text,
        session_id=session_id,
        t_id=t_id,
        turn_span=turn_span,
        start_time=start_time,
        idempotency_key=idempotency_key,
        traceparent=traceparent,
        customer_verified=customer_verified,
    )


def _dispatch_confirmation_flow(
    agent: Any,
    decision: RouterDecision,
    t_id: str,
    ref_tid: Optional[str],
    session_id: str,
    ticket_customer_id: str,
    customer_verified: bool,
    message_text: str,
    tokenized_text: Optional[str],
    pii_mapping: Dict[str, str],
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    traceparent: str,
    conv_state: ConversationState,
) -> Optional[Tuple[Optional[TriageResult], RouterDecision]]:
    turn_act = decision.details.get("turn_act")
    is_conf_no = (turn_act == "confirmation_no")
    is_affirmative = (turn_act == "confirmation_yes")
    is_uncond = (turn_act == "resolution_report")

    if is_conf_no or is_affirmative or is_uncond:
        return _process_customer_confirmation(
            agent=agent,
            decision=decision,
            is_conf_no=is_conf_no,
            is_affirmative=is_affirmative,
            is_uncond=is_uncond,
            t_id=t_id,
            ref_tid=ref_tid,
            session_id=session_id,
            ticket_customer_id=ticket_customer_id,
            customer_verified=customer_verified,
            message_text=message_text,
            tokenized_text=tokenized_text,
            pii_mapping=pii_mapping,
            turn_span=turn_span,
            start_time=start_time,
            idempotency_key=idempotency_key,
            traceparent=traceparent,
            conv_state=conv_state,
        )
    return None


def _handle_pre_routing_security_and_conditions(
    agent: Any,
    decision: RouterDecision,
    message_text: str,
    session_id: str,
    t_id: str,
    channel: str,
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    traceparent: str,
    customer_verified: bool,
    ticket_customer_id: str,
    ma_verdict: Optional[ModelArmorVerdict],
    tokenized_text: Optional[str],
    pii_mapping: Dict[str, str],
    oauth_access_token: Optional[str],
    caller_user_id: Optional[str],
) -> Optional[Tuple[TriageResult, RouterDecision]]:
    if decision.decision == "security_blocked":
        return _handle_policy_security_blocked(
            agent=agent,
            decision=decision,
            message_text=message_text,
            session_id=session_id,
            t_id=t_id,
            channel=channel,
            turn_span=turn_span,
            start_time=start_time,
            idempotency_key=idempotency_key,
            traceparent=traceparent,
            customer_verified=customer_verified,
            ticket_customer_id=ticket_customer_id,
            ma_verdict=ma_verdict,
            oauth_access_token=oauth_access_token,
            caller_user_id=caller_user_id,
        )

    sec_res = _detect_and_handle_security_intent(
        agent=agent,
        decision=decision,
        message_text=message_text,
        session_id=session_id,
        t_id=t_id,
        turn_span=turn_span,
        start_time=start_time,
        idempotency_key=idempotency_key,
        traceparent=traceparent,
        customer_verified=customer_verified,
    )
    if sec_res is not None:
        return sec_res

    is_cond = (
        decision.reason == "conditional_resolution_acknowledgement"
        or decision.details.get("condition_detected")
        or decision.details.get("turn_act") == "conditional_resolution"
    )
    if is_cond:
        return _handle_conditional_resolution(
            agent=agent,
            decision=decision,
            session_id=session_id,
            t_id=t_id,
            turn_span=turn_span,
            start_time=start_time,
            idempotency_key=idempotency_key,
            traceparent=traceparent,
        )

    ref_tid = _extract_ticket_id_reference(message_text)
    if ref_tid:
        sec_inq = _handle_security_ticket_status_inquiry(
            agent=agent,
            ref_tid=ref_tid,
            session_id=session_id,
            turn_span=turn_span,
            start_time=start_time,
            idempotency_key=idempotency_key,
            customer_verified=customer_verified,
            tokenized_text=tokenized_text,
            pii_mapping=pii_mapping,
        )
        if sec_inq is not None:
            return sec_inq

    return None


def handle_turn_acts_and_special_intents(
    agent: Any,
    decision: RouterDecision,
    message_text: str,
    session_id: str,
    t_id: str,
    channel: str,
    turn_span: Any,
    start_time: float,
    idempotency_key: str,
    traceparent: str,
    customer_verified: bool,
    ticket_customer_id: str,
    placeholder_cid: str,
    conv_state: ConversationState,
    ma_verdict: Optional[ModelArmorVerdict],
    tokenized_text: Optional[str],
    pii_mapping: Dict[str, str],
    oauth_access_token: Optional[str],
    caller_user_id: Optional[str],
    cust_ctx: CustomerContext,
) -> Tuple[Optional[TriageResult], RouterDecision]:
    """Stage 4: Handle deterministic turn acts, security refusals, and active ticket continuation."""
    sec_res = _handle_pre_routing_security_and_conditions(
        agent=agent,
        decision=decision,
        message_text=message_text,
        session_id=session_id,
        t_id=t_id,
        channel=channel,
        turn_span=turn_span,
        start_time=start_time,
        idempotency_key=idempotency_key,
        traceparent=traceparent,
        customer_verified=customer_verified,
        ticket_customer_id=ticket_customer_id,
        ma_verdict=ma_verdict,
        tokenized_text=tokenized_text,
        pii_mapping=pii_mapping,
        oauth_access_token=oauth_access_token,
        caller_user_id=caller_user_id,
    )
    if sec_res is not None:
        return sec_res

    ref_tid = _extract_ticket_id_reference(message_text)
    conf_res = _dispatch_confirmation_flow(
        agent=agent,
        decision=decision,
        t_id=t_id,
        ref_tid=ref_tid,
        session_id=session_id,
        ticket_customer_id=ticket_customer_id,
        customer_verified=customer_verified,
        message_text=message_text,
        tokenized_text=tokenized_text,
        pii_mapping=pii_mapping,
        turn_span=turn_span,
        start_time=start_time,
        idempotency_key=idempotency_key,
        traceparent=traceparent,
        conv_state=conv_state,
    )
    if conf_res is not None:
        early_res, decision = conf_res
        if early_res is not None:
            return early_res, decision

    early_res = _process_active_ticket_followup(
        agent=agent,
        decision=decision,
        ref_tid=ref_tid,
        message_text=message_text,
        tokenized_text=tokenized_text,
        sanitized_text=decision.details.get("sanitized_text"),
        pii_mapping=pii_mapping,
        session_id=session_id,
        placeholder_cid=placeholder_cid,
        turn_span=turn_span,
        start_time=start_time,
        idempotency_key=idempotency_key,
        traceparent=traceparent,
    )
    if early_res is not None:
        return early_res, decision

    if decision.predicted_intent == "conversation_history_access":
        early_res = _process_conversation_history(
            agent=agent,
            decision=decision,
            message_text=message_text,
            session_id=session_id,
            t_id=t_id,
            turn_span=turn_span,
            start_time=start_time,
            idempotency_key=idempotency_key,
            traceparent=traceparent,
            customer_verified=customer_verified,
            cust_ctx=cust_ctx,
        )
        return early_res, decision

    return None, decision


def read_memory_bank_context(
    memory_client: Any,
    customer_verified: bool,
    ticket_customer_id: str,
    memory_summary: str,
    body_to_persist: str,
    decision: RouterDecision,
    turn_span: Any,
) -> str:
    """Stage 5: Non-blocking Vertex AI Memory Bank retrieval for verified customer (REQ-AGENT-036)."""
    if (
        customer_verified
        and ticket_customer_id
        and not str(ticket_customer_id).startswith("anon:")
        and not memory_summary
    ):
        try:
            memory_summary = memory_client.retrieve_summary(
                customer_id=ticket_customer_id,
                query=body_to_persist,
                timeout_s=0.300,
            )
        except Exception as exc:
            logger.warning("Memory Bank retrieve failed open: %s", exc)
            memory_summary = ""

    if memory_summary:
        turn_span.set_attribute("memory_summary_len", len(memory_summary))
        decision.details["memory_summary"] = memory_summary

    return memory_summary


def _dispatch_security_or_blocked_route(
    decision: RouterDecision,
    message_text: str,
    session_id: str,
    t_id: str,
    channel: str,
    customer_verified: bool,
    ticket_customer_id: str,
    ma_verdict: Optional[ModelArmorVerdict],
    oauth_access_token: Optional[str],
    caller_user_id: Optional[str],
    traceparent: str,
) -> Optional[Tuple[RouterDecision, Optional[str], Optional[Dict[str, Any]], int, Optional[str]]]:
    if decision.decision == "blocked":
        blocked_reply = render_card_data_blocked_reply()
        handoff_resp = {
            "status": "BLOCKED",
            "route": "blocked",
            "answer": blocked_reply,
            "status_code": 400,
            "error": "card_data_blocked",
        }
        logger.warning(
            json.dumps(
                {
                    "event": "card_data_blocked",
                    "ticket_id": t_id,
                    "session_id": session_id,
                    "blocked_info_types": decision.details.get("blocked_info_types", []),
                },
                separators=(",", ":"),
            )
        )
        return decision, blocked_reply, handoff_resp, 400, "card_data_blocked"

    if decision.decision == "security_blocked":
        sec_ack = render_security_blocked_reply()
        handoff_resp = {
            "status": "SECURITY_BLOCKED",
            "route": "security_blocked",
            "answer": sec_ack,
            "status_code": 200,
        }
        eff_channel = derive_channel_source(
            channel=channel,
            oauth_access_token=oauth_access_token,
            caller_user_id=caller_user_id,
            session_id=session_id,
        )
        _emit_security_blocked_log(
            traceparent=traceparent,
            session_id=session_id,
            channel=eff_channel,
            customer_verified=customer_verified,
            customer_id=ticket_customer_id,
            ma_verdict=ma_verdict,
            message_text=message_text,
        )
        return decision, sec_ack, handoff_resp, 200, None

    return None


def _dispatch_escalation_route(
    agent: Any,
    decision: RouterDecision,
    body_to_persist: str,
    session_id: str,
    t_id: str,
    canonical_uuid: str,
    channel: str,
    ticket_customer_id: str,
    idempotency_key: str,
    pii_mapping: Dict[str, str],
    traceparent: str,
) -> Optional[Tuple[RouterDecision, Optional[str], Optional[Dict[str, Any]], int, Optional[str]]]:
    if decision.decision != "escalate":
        return None

    category = decision.details.get("category")
    handoff_res = agent.human_handoff.escalate(
        ticket_id=t_id,
        ticket_uuid=canonical_uuid,
        queue=decision.queue,
        reason=decision.reason,
        escalated_by="triage_router",
        degraded=decision.degraded,
        traceparent=traceparent,
        customer_id=ticket_customer_id,
        session_id=session_id,
        body_redacted=body_to_persist,
        channel=channel,
        category=category,
        idempotency_key=idempotency_key,
        decision_payload=clean_decision_dict(decision),
    )
    if decision.reason == "model_armor_security_trigger":
        calming_ack = render_model_armor_security_trigger_ack(t_id)
    else:
        calming_ack = handoff_res.calming_ack

    if handoff_res.persisted:
        agent._save_redaction_map_if_needed(
            ticket_id=t_id,
            ticket_uuid=canonical_uuid,
            customer_id=ticket_customer_id,
            mapping=pii_mapping,
        )
    return decision, calming_ack, None, 200, None


def _dispatch_conversational_or_oos_route(
    agent: Any,
    decision: RouterDecision,
    body_to_persist: str,
    channel: str,
    t_id: str,
    canonical_uuid: str,
    ticket_customer_id: str,
    idempotency_key: str,
    pii_mapping: Dict[str, str],
) -> Optional[Tuple[RouterDecision, Optional[str], Optional[Dict[str, Any]], int, Optional[str]]]:
    if decision.decision == "reject_oos":
        calming_ack = render_out_of_scope_refusal(ticket_id=t_id)
        upsert_res = agent._safe_upsert(
            ticket_id=t_id,
            idempotency_key=idempotency_key,
            customer_id=ticket_customer_id,
            channel=channel,
            body_redacted=body_to_persist,
            decision=clean_decision_dict(decision),
        )
        ticket_uuid = (
            upsert_res.get("ticket_id")
            if isinstance(upsert_res, dict) and upsert_res.get("ticket_id")
            else canonical_uuid
        )
        if upsert_res is not None:
            agent._save_redaction_map_if_needed(
                ticket_id=t_id,
                ticket_uuid=ticket_uuid,
                customer_id=ticket_customer_id,
                mapping=pii_mapping,
            )
        return decision, calming_ack, None, 200, None

    if decision.details.get("category") in ("greeting", "pleasantry"):
        upsert_res = agent._safe_upsert(
            ticket_id=t_id,
            idempotency_key=idempotency_key,
            customer_id=ticket_customer_id,
            channel=channel,
            body_redacted=body_to_persist,
            decision=clean_decision_dict(decision),
        )
        ticket_uuid = (
            upsert_res.get("ticket_id")
            if isinstance(upsert_res, dict) and upsert_res.get("ticket_id")
            else canonical_uuid
        )
        if upsert_res is not None:
            agent._save_redaction_map_if_needed(
                ticket_id=t_id,
                ticket_uuid=ticket_uuid,
                customer_id=ticket_customer_id,
                mapping=pii_mapping,
            )
        handoff_resp = {
            "status": "AUTO_RESOLVED",
            "route": "resolved",
            "answer": render_conversational_reply(decision.details.get("category")),
        }
        return decision, None, handoff_resp, 200, None

    return None


def _dispatch_terminal_routes(
    agent: Any,
    decision: RouterDecision,
    message_text: str,
    body_to_persist: str,
    session_id: str,
    t_id: str,
    canonical_uuid: str,
    channel: str,
    customer_verified: bool,
    ticket_customer_id: str,
    idempotency_key: str,
    pii_mapping: Dict[str, str],
    ma_verdict: Optional[ModelArmorVerdict],
    oauth_access_token: Optional[str],
    caller_user_id: Optional[str],
    traceparent: str,
) -> Optional[Tuple[RouterDecision, Optional[str], Optional[Dict[str, Any]], int, Optional[str]]]:
    sec_res = _dispatch_security_or_blocked_route(
        decision=decision,
        message_text=message_text,
        session_id=session_id,
        t_id=t_id,
        channel=channel,
        customer_verified=customer_verified,
        ticket_customer_id=ticket_customer_id,
        ma_verdict=ma_verdict,
        oauth_access_token=oauth_access_token,
        caller_user_id=caller_user_id,
        traceparent=traceparent,
    )
    if sec_res is not None:
        return sec_res

    esc_res = _dispatch_escalation_route(
        agent=agent,
        decision=decision,
        body_to_persist=body_to_persist,
        session_id=session_id,
        t_id=t_id,
        canonical_uuid=canonical_uuid,
        channel=channel,
        ticket_customer_id=ticket_customer_id,
        idempotency_key=idempotency_key,
        pii_mapping=pii_mapping,
        traceparent=traceparent,
    )
    if esc_res is not None:
        return esc_res

    return _dispatch_conversational_or_oos_route(
        agent=agent,
        decision=decision,
        body_to_persist=body_to_persist,
        channel=channel,
        t_id=t_id,
        canonical_uuid=canonical_uuid,
        ticket_customer_id=ticket_customer_id,
        idempotency_key=idempotency_key,
        pii_mapping=pii_mapping,
    )


def _execute_a2a_handoff(
    agent: Any,
    decision: RouterDecision,
    body_to_persist: str,
    session_id: str,
    t_id: str,
    canonical_uuid: str,
    channel: str,
    customer_verified: bool,
    ticket_customer_id: str,
    cust_ctx: CustomerContext,
    identity_issuer: Optional[str],
    memory_summary: str,
    traceparent: str,
    idempotency_key: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    verified_cid = cust_ctx.customer_id if cust_ctx.verified else None
    cct = agent._mint_cct_if_verified(
        customer_verified=customer_verified,
        customer_id=verified_cid,
        ticket_id=t_id,
    )
    sub_q = None
    if decision.details.get("turn_interpretation"):
        ti_dict = decision.details["turn_interpretation"]
        intents_list = ti_dict.get("intents") or []
        if len(intents_list) >= 2:
            sub_q = [{"text": s.get("text_span", ""), "intent": s.get("intent", "other")} for s in intents_list]
    payload = A2AHandoffPayload(
        ticket_id=t_id,
        session_id=session_id,
        predicted_intent=decision.predicted_intent,
        redacted_message=body_to_persist,
        memory_summary=memory_summary,
        customer_id=verified_cid,
        customer_verified=customer_verified,
        identity_issuer=identity_issuer,
        traceparent=get_current_traceparent() or traceparent,
        customer_context_token=cct,
        ticket_uuid=canonical_uuid,
        sub_questions=sub_q if sub_q else None,
    )
    handoff_resp = agent.a2a_client.handoff(payload)
    if isinstance(handoff_resp, dict) and not customer_verified:
        handoff_resp.pop("confirmation_question", None)
        handoff_resp.pop("resolution_prompt", None)
    if (
        customer_verified
        and isinstance(handoff_resp, dict)
        and handoff_resp.get("status") == "AUTO_RESOLVED"
    ):
        conf_question = f"\n\nDid that resolve your issue? Reply yes to close ticket #{t_id}."
        handoff_resp["confirmation_question"] = conf_question.strip()
        handoff_resp["resolution_prompt"] = conf_question.strip()
        if "reply_text" in handoff_resp and conf_question.strip() not in handoff_resp["reply_text"]:
            handoff_resp["reply_text"] = handoff_resp["reply_text"] + conf_question
    calming_ack: Optional[str] = None
    if isinstance(handoff_resp, dict) and handoff_resp.get("status") == "ESCALATED":
        if handoff_resp.get("handoff_done"):
            calming_ack = str(
                handoff_resp.get("calming_ack")
                or render_calming_acknowledgement(
                    queue=str(handoff_resp.get("queue", "human-standard")),
                    ticket_id=t_id,
                )
            )
        else:
            handoff_res = agent.human_handoff.escalate(
                ticket_id=t_id,
                ticket_uuid=canonical_uuid,
                queue=str(handoff_resp.get("queue", "human-standard")),
                reason=str(handoff_resp.get("reason", "faq_escalated")),
                escalated_by="triage_router",
                degraded=bool(handoff_resp.get("degraded", False)),
                traceparent=traceparent,
                customer_id=ticket_customer_id,
                session_id=session_id,
                body_redacted=body_to_persist,
                channel=channel,
                idempotency_key=idempotency_key,
                decision_payload=clean_decision_dict(decision),
            )
            calming_ack = handoff_res.calming_ack
    return handoff_resp, calming_ack


def _dispatch_a2a_handoff_and_persist(
    agent: Any,
    decision: RouterDecision,
    body_to_persist: str,
    session_id: str,
    t_id: str,
    canonical_uuid: str,
    channel: str,
    customer_verified: bool,
    ticket_customer_id: str,
    cust_ctx: CustomerContext,
    identity_issuer: Optional[str],
    memory_summary: str,
    traceparent: str,
    idempotency_key: str,
    pii_mapping: Dict[str, str],
) -> Tuple[RouterDecision, Optional[str], Optional[Dict[str, Any]], int, Optional[str]]:
    decision_snapshot = clean_decision_dict(decision)
    decision_snapshot["ticket_id"] = t_id
    decision_snapshot["session_id"] = session_id
    upsert_ctx = contextvars.copy_context()
    a2a_failed_exc: Optional[Exception] = None
    calming_ack: Optional[str] = None
    handoff_resp: Optional[Dict[str, Any]] = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as upsert_pool:
        upsert_fut = upsert_pool.submit(
            upsert_ctx.run,
            agent._safe_upsert,
            ticket_id=t_id,
            idempotency_key=idempotency_key,
            customer_id=ticket_customer_id,
            channel=channel,
            body_redacted=body_to_persist,
            decision=decision_snapshot,
        )
        try:
            handoff_resp, calming_ack = _execute_a2a_handoff(
                agent=agent,
                decision=decision,
                body_to_persist=body_to_persist,
                session_id=session_id,
                t_id=t_id,
                canonical_uuid=canonical_uuid,
                channel=channel,
                customer_verified=customer_verified,
                ticket_customer_id=ticket_customer_id,
                cust_ctx=cust_ctx,
                identity_issuer=identity_issuer,
                memory_summary=memory_summary,
                traceparent=traceparent,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            a2a_failed_exc = exc
        finally:
            upsert_res = upsert_fut.result()

    ticket_uuid = (
        upsert_res.get("ticket_id")
        if isinstance(upsert_res, dict) and upsert_res.get("ticket_id")
        else canonical_uuid
    )

    if upsert_res is not None:
        agent._save_redaction_map_if_needed(
            ticket_id=t_id,
            ticket_uuid=ticket_uuid,
            customer_id=ticket_customer_id,
            mapping=pii_mapping,
        )
        if (
            isinstance(handoff_resp, dict)
            and handoff_resp.get("status") == "AUTO_RESOLVED"
        ):
            try:
                agent.mcp_client.record_resolution_confirmation_requested(
                    ticket_id=ticket_uuid or t_id,
                    prompt=handoff_resp.get("confirmation_question"),
                )
            except Exception as exc:
                logger.warning("Failed to record resolution confirmation requested: %s", exc)

    if a2a_failed_exc is not None:
        decision = replace(
            decision,
            decision="escalate",
            escalate=True,
            queue="human-standard",
            degraded=True,
            details={**decision.details, "a2a_error": type(a2a_failed_exc).__name__},
        )
        handoff_res = agent.human_handoff.escalate(
            ticket_id=t_id,
            ticket_uuid=ticket_uuid,
            queue=decision.queue,
            reason="a2a_handoff_failed",
            escalated_by="triage_router",
            degraded=True,
            traceparent=traceparent,
            customer_id=ticket_customer_id,
            session_id=session_id,
            body_redacted=body_to_persist,
            channel=channel,
            idempotency_key=idempotency_key,
            decision_payload=clean_decision_dict(decision),
        )
        calming_ack = handoff_res.calming_ack
        handoff_resp = {
            "status": "ESCALATED",
            "queue": decision.queue,
            "reason": "a2a_handoff_failed",
            "calming_ack": calming_ack,
            "handoff_done": True,
        }

    return decision, calming_ack, handoff_resp, 200, None


def dispatch_route_and_persist(
    agent: Any,
    decision: RouterDecision,
    message_text: str,
    body_to_persist: str,
    session_id: str,
    t_id: str,
    canonical_uuid: str,
    channel: str,
    customer_verified: bool,
    ticket_customer_id: str,
    cust_ctx: CustomerContext,
    identity_issuer: Optional[str],
    memory_summary: str,
    traceparent: str,
    idempotency_key: str,
    pii_mapping: Dict[str, str],
    ma_verdict: Optional[ModelArmorVerdict],
    oauth_access_token: Optional[str],
    caller_user_id: Optional[str],
) -> Tuple[RouterDecision, Optional[str], Optional[Dict[str, Any]], int, Optional[str]]:
    """Stage 6: Dispatch route (Escalation vs Auto-RAG vs Conversational vs OOS) and persist."""
    term = _dispatch_terminal_routes(
        agent=agent,
        decision=decision,
        message_text=message_text,
        body_to_persist=body_to_persist,
        session_id=session_id,
        t_id=t_id,
        canonical_uuid=canonical_uuid,
        channel=channel,
        customer_verified=customer_verified,
        ticket_customer_id=ticket_customer_id,
        idempotency_key=idempotency_key,
        pii_mapping=pii_mapping,
        ma_verdict=ma_verdict,
        oauth_access_token=oauth_access_token,
        caller_user_id=caller_user_id,
        traceparent=traceparent,
    )
    if term is not None:
        return term

    return _dispatch_a2a_handoff_and_persist(
        agent=agent,
        decision=decision,
        body_to_persist=body_to_persist,
        session_id=session_id,
        t_id=t_id,
        canonical_uuid=canonical_uuid,
        channel=channel,
        customer_verified=customer_verified,
        ticket_customer_id=ticket_customer_id,
        cust_ctx=cust_ctx,
        identity_issuer=identity_issuer,
        memory_summary=memory_summary,
        traceparent=traceparent,
        idempotency_key=idempotency_key,
        pii_mapping=pii_mapping,
    )


def finalize_triage_turn(
    agent: Any,
    decision: RouterDecision,
    t_id: str,
    session_id: str,
    traceparent: str,
    start_time: float,
    customer_verified: bool,
    ticket_customer_id: str,
    body_to_persist: str,
    calming_ack: Optional[str],
    handoff_resp: Optional[Dict[str, Any]],
    idempotency_key: str,
    status_code: int,
    error_msg: Optional[str],
    pii_mapping: Dict[str, str],
    turn_span: Any,
) -> TriageResult:
    """Stage 7: Finalize turn logging, asynchronous memory synthesis, PII restoration, and result construction."""
    elapsed_ms = (time.monotonic() - start_time) * 1000.0

    # 3. Emit structured PII-free decision log (REQ-AGENT-014)
    agent.logger.log_decision(
        ticket_id=t_id,
        session_id=session_id,
        decision=decision.decision,
        tier=decision.tier,
        queue=decision.queue,
        degraded=decision.degraded,
        latency_ms=elapsed_ms,
        traceparent=traceparent,
        predicted_intent=decision.predicted_intent,
    )

    # REQ-AGENT-036: Non-blocking write to Memory Bank from tokenized turn (never restored PII)
    if (
        customer_verified
        and ticket_customer_id
        and not str(ticket_customer_id).startswith("anon:")
        and decision.decision not in ("blocked", "security_blocked")
        and decision.reason != "card_data_blocked"
    ):
        try:
            dec_summary = (
                f"Decision: {decision.decision}, queue: {decision.queue}, "
                f"intent: {decision.predicted_intent}"
            )
            agent.memory_client.generate_turn_memories(
                customer_id=ticket_customer_id,
                user_message_tokenized=body_to_persist,
                decision_summary=dec_summary,
                async_mode=True,
            )
        except Exception as exc:
            logger.warning("Memory Bank write failed: %s", exc)

    # Restore in-memory PII tokens for customer-facing reply paths (REQ-STORE-009)
    if calming_ack:
        calming_ack = restore_pii_tokens(calming_ack, pii_mapping)
    elif decision.calming_ack:
        calming_ack = restore_pii_tokens(decision.calming_ack, pii_mapping)
    if isinstance(handoff_resp, dict):
        handoff_resp = dict(handoff_resp)
        if handoff_resp.get("answer"):
            handoff_resp["answer"] = restore_pii_tokens(str(handoff_resp["answer"]), pii_mapping)
        if handoff_resp.get("calming_ack"):
            handoff_resp["calming_ack"] = restore_pii_tokens(str(handoff_resp["calming_ack"]), pii_mapping)
        if handoff_resp.get("message"):
            handoff_resp["message"] = restore_pii_tokens(str(handoff_resp["message"]), pii_mapping)

    _record_turn_final_span_attributes(turn_span, decision, handoff_resp)
    return TriageResult(
        ticket_id=t_id,
        session_id=session_id,
        decision=decision,
        calming_ack=calming_ack,
        handoff_response=handoff_resp,
        idempotency_key=idempotency_key,
        latency_ms=elapsed_ms,
        status_code=status_code,
        error=error_msg,
    )
