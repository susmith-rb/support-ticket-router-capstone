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

import concurrent.futures
import contextvars
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Dict, Optional

from ticket_router.agents.triage_router.a2a_client import (
    A2AClientProtocol,
    A2AHandoffPayload,
    HttpA2AClient,
    InMemoryA2AClient,
)
from ticket_router.agents.triage_router.history import (
    is_cross_customer_history_request,
)
from ticket_router.agents.triage_router.mcp_client import (
    HttpTicketDbMcpClient,
    InMemoryTicketDbMcpClient,
    TicketDbMcpClientProtocol,
    compute_idempotency_key,
)
from ticket_router.agents.triage_router.policy import (
    RouterDecision,
    RouterThresholds,
    TriagePolicy,
    VerifiedFacts,
    _extract_blocked_card_info_types,
)
from ticket_router.agents.triage_router.templates import (
    render_calming_acknowledgement,
    render_card_data_blocked_reply,
    render_conversational_reply,
    render_followup_calming_reply,
    render_out_of_scope_refusal,
    render_security_blocked_reply,
)
from ticket_router.common.hedging import hedged_call
from ticket_router.common.observability import (
    StructuredJsonLogger,
    generate_traceparent,
    get_current_traceparent,
    start_span,
)
from ticket_router.common.resilience import TIER2_DEADLINE_S, TIER2_HEDGE_AFTER_S
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
from ticket_router.security.pii_tokens import restore_pii_tokens
from ticket_router.store.ids import canonical_ticket_uuid
from ticket_router.store.models import effective_category, is_holding_ticket, is_human_owned

logger = logging.getLogger(__name__)
_SIGNER_INIT_LOCK = threading.Lock()


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


def _is_affirmative_resolution(text: str) -> bool:
    """Check if message is an affirmative confirmation that resolves an issue."""
    t = text.strip().lower()
    if not t:
        return False
    if re.search(
        r"\b(but|however|still|not|didn't|does not|unresolved|haven't|problem|issue|broken|help|urgent|agent|human|here)\b",
        t,
    ):
        return False
    patterns = [
        r"^yes[!.?]*$",
        r"^yep[!.?]*$",
        r"^yeah[!.?]*$",
        r"^yes\s+(?:thank\s+you|thanks|please)[!.?]*$",
        r"^(?:yes\s*,?\s*)?(?:that\s+(?:helped|resolved\s+it|fixed\s+it)|all\s+(?:good|set))(?:\s*,?\s*(?:thank\s+you|thanks))?[!.?]*$",
        r"^(?:yes\s*,?\s*)?(?:close\s+ticket|mark\s+as\s+resolved|issue\s+resolved)(?:\s*,?\s*(?:thank\s+you|thanks))?[!.?]*$",
        r"^(?:yes\s*,?\s*)?(?:that\s+helped\s+close\s+ticket)(?:\s*,?\s*(?:thank\s+you|thanks))?[!.?]*$",
    ]
    return any(re.match(p, t) for p in patterns)


def _is_conversational_followup_for_held_ticket(
    text: str,
    active_ticket: Optional[Dict[str, Any]] = None,
) -> bool:
    """Determine if message is a conversational follow-up to an active human-held ticket."""
    if _is_explicit_ticket_reference(text):
        return True
    t = text.strip().lower()
    if not t:
        return True
    # Conversational connectives indicating follow-up to existing dialogue
    if re.match(r"^(also|and|by the way|additionally|as well|furthermore|update:|regarding)\b", t):
        return True
    # Phrases indicating elaboration, ongoing wait, or specialist inquiry
    followup_phrases = (
        "while you review",
        "review this",
        "waiting",
        "when will",
        "follow up with me",
        "get back to me",
        "for updates",
        "my alternate email",
        "my email is",
        "my phone is",
        "here is",
        "receipt",
        "purchase receipt",
        "attached",
        "i am here",
        "i'm here",
        "still waiting",
        "just checking",
    )
    if any(p in t for p in followup_phrases):
        return True
    # Short acknowledgments in an active human queue
    if re.match(r"^(yes|yep|yeah|ok|okay|sure|thanks|thank you)\b", t):
        return True
    return False


def _derive_customer_ticket_id(
    tid: str,
    session_id: str,
    active_ticket: Optional[Dict[str, Any]] = None,
) -> str:
    """Derive customer-safe ticket reference string (REQ-AGENT-033)."""
    def _is_uuid_str(val: Any) -> bool:
        try:
            uuid.UUID(str(val))
            return True
        except (ValueError, AttributeError, TypeError):
            return False

    if tid and not _is_uuid_str(tid):
        return tid

    if active_ticket:
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

        held_sess = (
            active_ticket.get("session_id")
            or (isinstance(held_dec, dict) and held_dec.get("session_id"))
        )
        if held_sess:
            return f"TICK-{str(held_sess)[:8]}"

    return f"TICK-{session_id[:8]}"


@dataclass(frozen=True)
class TriageResult:
    """End-to-end outcome of a triage routing turn."""
    ticket_id: str
    session_id: str
    decision: RouterDecision
    calming_ack: Optional[str] = None
    handoff_response: Optional[Dict[str, Any]] = None
    idempotency_key: str = ""
    latency_ms: float = 0.0
    status_code: int = 200
    error: Optional[str] = None

    def __getitem__(self, item: str) -> Any:
        return self.to_dict()[item]

    def __contains__(self, item: object) -> bool:
        return item in self.to_dict()

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        """Convert result to response dictionary."""
        d: Dict[str, Any] = {
            "ticket_id": self.ticket_id,
            "session_id": self.session_id,
            "decision": self.decision.decision,
            "queue": self.decision.queue,
            "calming_ack": self.calming_ack,
            "intent": self.decision.predicted_intent,
            "latency_ms": self.latency_ms,
            "status_code": self.status_code,
        }
        if self.error:
            d["error"] = self.error
        if self.calming_ack:
            d["answer"] = self.calming_ack
        elif self.handoff_response and self.handoff_response.get("answer"):
            d["answer"] = self.handoff_response.get("answer")
        if self.handoff_response:
            d["handoff_response"] = self.handoff_response
        return d


def invoke_tier2_gemini(
    text: str,
    target_model: str,
    project: Optional[str] = None,
    location: Optional[str] = None,
) -> RouterPrediction:
    """Execute Tier 2 fallback inference using Gemini Flash (REQ-AGENT-002, REQ-AGENT-013)."""
    from ticket_router.models.tier2_gemini import check_gemini_429_hook

    check_gemini_429_hook()
    try:
        from google import genai  # type: ignore[import-untyped]
        client_kwargs: Dict[str, Any] = {}
        effective_project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
        if effective_project:
            client_kwargs["project"] = effective_project
            client_kwargs["vertexai"] = True
        if location:
            client_kwargs["location"] = location
        client = genai.Client(**client_kwargs)
        from ticket_router.config.agent_tuning import (
            get_tier2_prompt,
            get_tier2_system_instruction,
        )
        prompt = get_tier2_prompt(text)
        sys_inst = get_tier2_system_instruction()
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

        resp = hedged_call(
            lambda: client.models.generate_content(**call_kwargs),
            TIER2_HEDGE_AFTER_S,
            TIER2_DEADLINE_S,
            span_name="tier2.generate_content",
        )

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

                if is_oos:
                    intent = "other"
                else:
                    intent = str(parsed.get("intent") or ("other" if is_esc else "faq")).strip()

                esc_prob = float(parsed.get("escalate_prob", 0.85 if is_esc else 0.10))
                intent_prob = float(parsed.get("intent_prob", 0.80))
                merged_raw = {**parsed, **extra_raw}
                return RouterPrediction(
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
                )
        except Exception:
            pass

        is_angry = "angry" in raw_text.lower()
        is_urgent = "immediate" in raw_text.lower()
        is_esc = "escalate" in raw_text.lower() or is_angry or is_urgent
        fallback_raw = {"raw_text": raw_text, **extra_raw}
        return RouterPrediction(
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
    ) -> None:
        self._policy = policy
        self._mcp_client = mcp_client
        self._a2a_client = a2a_client
        self._model_armor_client = model_armor_client
        self._human_handoff = human_handoff
        self._redaction_sealer = redaction_sealer
        self._memory_client = memory_client
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
            self._policy = TriagePolicy(
                thresholds=RouterThresholds.from_tuning_config(),
                s1_client=s1_client,
                tier2_invoker=tier2_invoker,
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
                self._policy = TriagePolicy(
                    thresholds=RouterThresholds.from_tuning_config(),
                    s1_client=RouterS1Client(),
                    tier2_invoker=create_tier2_invoker("router_tier2"),
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
    ) -> TriageResult:
        """Execute end-to-end triage evaluation, persistence, and dispatch."""
        self._ensure_initialized()
        start_time = time.monotonic()
        t_id = ticket_id or f"TICK-{session_id[:8]}"
        traceparent = generate_traceparent()
        idempotency_key = compute_idempotency_key(session_id, message_text)
        canonical_uuid = canonical_ticket_uuid(idempotency_key)

        if customer_id is not None:
            logger.warning(
                "customer_id argument is deprecated and ignored; pass customer_id_token instead."
            )

        with start_span(
            "triage_router_turn",
            attributes={"session_id": session_id, "ticket_id": t_id, "channel": channel},
            traceparent=traceparent,
        ) as turn_span:
            mcp_client = self.mcp_client
            policy = self.policy
            placeholder_cid = f"anon:{session_id}"

            # Ingress Guard: Model Armor payment card check before any persistence or models (REQ-MA-007, REQ-AGENT-032)
            ma_verdict: Optional[ModelArmorVerdict] = None
            sanitized_text: Optional[str] = None
            tokenized_text: Optional[str] = None
            pii_mapping: Dict[str, str] = {}
            ma_client = getattr(policy, "model_armor_client", None)
            if ma_client is not None and hasattr(policy, "_run_model_armor"):
                ma_verdict, sanitized_text, tokenized_text, pii_mapping = policy._run_model_armor(message_text)
                blocked_card_types = _extract_blocked_card_info_types(ma_verdict)
                if blocked_card_types:
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
                    return TriageResult(
                        ticket_id=t_id,
                        session_id=session_id,
                        decision=decision,
                        calming_ack=blocked_reply,
                        handoff_response={
                            "status": "BLOCKED",
                            "route": "blocked",
                            "answer": blocked_reply,
                            "status_code": 400,
                            "error": "card_data_blocked",
                        },
                        idempotency_key=idempotency_key,
                        latency_ms=elapsed_ms,
                        status_code=400,
                        error="card_data_blocked",
                    )

            # Model Armor PI/JB check on every turn before holding or intent routing (REQ-MA-003, Requirement A)
            if (
                ma_verdict is not None
                and ma_verdict.has_pi_jb
                and not getattr(policy, "_is_prompt_safety_delegated", lambda: False)()
            ):
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
                self.logger.log_decision(
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
                return TriageResult(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision=sec_decision,
                    calming_ack=sec_ack,
                    handoff_response={
                        "status": "SECURITY_BLOCKED",
                        "route": "security_blocked",
                        "answer": sec_ack,
                        "ticket_id": t_id,
                        "queue": "none",
                        "status_code": 200,
                    },
                    idempotency_key=idempotency_key,
                    latency_ms=elapsed_ms,
                    status_code=200,
                    error=None,
                )

            # REQ-AGENT-027: Run identity resolution concurrently with policy evaluation on every turn
            eval_kwargs = {
                "text": message_text,
                "session_id": session_id,
                "customer_id": placeholder_cid,
                "verified_facts": verified_facts,
                "ticket_id": t_id,
            }
            try:
                sig = inspect.signature(policy.evaluate)
                if "precomputed_ma" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    eval_kwargs["precomputed_ma"] = (ma_verdict, sanitized_text, tokenized_text, pii_mapping)
            except Exception:
                pass

            id_ctx = contextvars.copy_context()
            pol_ctx = contextvars.copy_context()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as step1_pool:
                id_fut = step1_pool.submit(
                    id_ctx.run,
                    resolve_identity,
                    token=customer_id_token,
                    ticket_db_client=mcp_client,
                    oauth_access_token=oauth_access_token,
                    expected_oauth_client_id=self._expected_oauth_client_id,
                )
                pol_fut = step1_pool.submit(
                    pol_ctx.run,
                    policy.evaluate,
                    **eval_kwargs,
                )
                cust_ctx = id_fut.result()
                decision = pol_fut.result()

            ticket_customer_id = cust_ctx.customer_id if cust_ctx.verified else placeholder_cid
            customer_verified = cust_ctx.verified
            identity_issuer = cust_ctx.issuer if cust_ctx.verified else None
            identity_reason = cust_ctx.reason

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

            # Determine body_redacted (use reversible tokenized text if present, else sanitized_text)
            body_to_persist = message_text
            if decision.tokenized_text:
                body_to_persist = decision.tokenized_text
            elif decision.details.get("sanitized_text"):
                body_to_persist = decision.details["sanitized_text"]
            pii_mapping = decision.pii_mapping or {}

            # Handle Model Armor security block returned from policy evaluation
            if decision.decision == "security_blocked":
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
                return TriageResult(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision=decision,
                    calming_ack=sec_ack,
                    handoff_response={
                        "status": "SECURITY_BLOCKED",
                        "route": "security_blocked",
                        "answer": sec_ack,
                        "ticket_id": t_id,
                        "queue": "none",
                        "status_code": 200,
                    },
                    idempotency_key=idempotency_key,
                    latency_ms=elapsed_ms,
                    status_code=200,
                    error=None,
                )

            # REQ-AGENT-037 / REQ-LIFE-001: Explicit reference to security ticket -> safe status-only response
            ref_tid = _extract_ticket_id_reference(message_text)
            if ref_tid:
                try:
                    ref_ticket = mcp_client.get_ticket(ticket_id=ref_tid)
                except Exception:
                    ref_ticket = None

                if isinstance(ref_ticket, dict):
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
                        return TriageResult(
                            ticket_id=ref_tid,
                            session_id=session_id,
                            decision=status_decision,
                            calming_ack=safe_ack,
                            handoff_response={
                                "status": "STATUS_INQUIRY",
                                "route": "ticket_status",
                                "answer": safe_ack,
                                "ticket_id": ref_tid,
                                "queue": "none",
                                "status_code": 200,
                            },
                            idempotency_key=idempotency_key,
                            latency_ms=elapsed_ms,
                            status_code=200,
                            error=None,
                        )

            # REQ-LIFE-002: Customer resolution confirmation flow (affirmative reply to auto-resolved bot ticket)
            if _is_affirmative_resolution(message_text):
                cand_ticket: Optional[Dict[str, Any]] = None
                if ref_tid:
                    try:
                        cand_ticket = mcp_client.get_ticket(ref_tid)
                    except Exception:
                        cand_ticket = None
                if not cand_ticket:
                    try:
                        cand_ticket = mcp_client.get_active_ticket_for_session(session_id=session_id)
                    except Exception:
                        cand_ticket = None
                if not cand_ticket:
                    try:
                        cand_ticket = mcp_client.get_ticket(f"TICK-{session_id[:8]}")
                    except Exception:
                        cand_ticket = None

                if isinstance(cand_ticket, dict):
                    # 1. Require pending-confirmation marker recorded on ticket (REQ-LIFE-002)
                    if not cand_ticket.get("resolution_confirmation_requested"):
                        cand_ticket = None

                    # 2. Session scoping (unless explicitly referenced by ref_tid)
                    if cand_ticket:
                        ticket_sess = cand_ticket.get("session_id")
                        if not ticket_sess and isinstance(cand_ticket.get("decision"), dict):
                            ticket_sess = cand_ticket["decision"].get("session_id")
                        if not ref_tid and ticket_sess and ticket_sess != session_id:
                            cand_ticket = None

                    # 3. Customer ownership scoping
                    if cand_ticket and ticket_customer_id:
                        ticket_cust = cand_ticket.get("customer_id")
                        if ticket_cust and ticket_cust != ticket_customer_id:
                            cand_ticket = None

                if isinstance(cand_ticket, dict):
                    cand_tid = str(cand_ticket.get("ticket_id") or cand_ticket.get("id") or f"TICK-{session_id[:8]}")
                    cand_cat = cand_ticket.get("category", "support")
                    cand_owner = cand_ticket.get("current_owner", "bot")
                    cand_status = cand_ticket.get("status", "open")

                    # Customer resolution confirmation allowed ONLY on bot-owned support tickets
                    if cand_cat not in ("security", "fraud") and cand_owner == "bot":
                        if not customer_verified or not ticket_customer_id or ticket_customer_id.startswith("anon:"):
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
                            return TriageResult(
                                ticket_id=cand_tid,
                                session_id=session_id,
                                decision=anon_dec,
                                calming_ack=anon_refusal,
                                handoff_response={
                                    "status": "FORBIDDEN",
                                    "answer": anon_refusal,
                                    "ticket_id": cand_tid,
                                    "status_code": 403,
                                    "error": "anonymous_confirmation_forbidden",
                                },
                                idempotency_key=idempotency_key,
                                latency_ms=elapsed_ms,
                                status_code=200,
                                error=None,
                            )

                        # Verified customer: update ticket status to resolved_by_customer
                        try:
                            mcp_client.update_ticket_status(
                                ticket_id=cand_tid,
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
                            self.logger.log_decision(
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
                            return TriageResult(
                                ticket_id=cand_tid,
                                session_id=session_id,
                                decision=res_dec,
                                calming_ack=ack,
                                handoff_response={
                                    "status": "RESOLVED",
                                    "route": "resolved_by_customer",
                                    "answer": ack,
                                    "ticket_id": cand_tid,
                                    "status_code": 200,
                                },
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

            # REQ-AGENT-033 / REQ-LIFE-001: Deterministic issue continuation check on active holding ticket
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

            if isinstance(active_ticket, dict) and (active_ticket.get("ticket_id") or active_ticket.get("id")):
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

                actionable_intents = {
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
                }

                is_explicit_ref = bool((ref_tid and ref_tid == holding_tid) or _is_explicit_ticket_reference(message_text))

                if is_explicit_ref:
                    should_continue = True
                elif cur_intent in ("followup", "other"):
                    should_continue = _is_conversational_followup_for_held_ticket(message_text, active_ticket)
                elif held_intent and cur_intent == held_intent:
                    should_continue = True
                elif cur_intent in actionable_intents:
                    should_continue = False
                else:
                    should_continue = False

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

                if should_continue and is_held:
                    body_to_persist = tokenized_text or sanitized_text or message_text
                    ticket_customer_id = active_ticket.get("customer_id") or placeholder_cid

                    if pii_mapping:
                        self._save_redaction_map_if_needed(
                            ticket_id=customer_ticket_id,
                            ticket_uuid=holding_tid,
                            customer_id=ticket_customer_id,
                            mapping=pii_mapping,
                        )
                    try:
                        mcp_client.append_customer_followup(
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
                        details={
                            "holding_ticket_id": customer_ticket_id,
                            "holding_ticket_uuid": holding_tid,
                            "queue": holding_queue,
                            "classifier_predicted_intent": cur_intent,
                        },
                        customer_verified=bool(active_ticket.get("customer_verified", False)),
                        tokenized_text=tokenized_text,
                        pii_mapping=pii_mapping,
                    )
                    self.logger.log_decision(
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
                    return TriageResult(
                        ticket_id=customer_ticket_id,
                        session_id=session_id,
                        decision=followup_decision,
                        calming_ack=calming_ack,
                        handoff_response={
                            "status": "ESCALATED",
                            "route": "followup",
                            "answer": calming_ack,
                            "ticket_id": customer_ticket_id,
                            "holding_ticket_uuid": holding_tid,
                            "queue": holding_queue,
                            "handoff_done": True,
                        },
                        idempotency_key=idempotency_key,
                        latency_ms=elapsed_ms,
                        status_code=200,
                        error=None,
                    )

            # REQ-HIST-001 / REQ-HIST-002: Deterministic conversation history access handling
            if decision.predicted_intent == "conversation_history_access":
                parts_trace = traceparent.split("-")
                trace_id = parts_trace[1] if len(parts_trace) >= 2 else ""

                verified_cid = (
                    cust_ctx.customer_id
                    if (customer_verified and cust_ctx.customer_id and not str(cust_ctx.customer_id).startswith("anon:"))
                    else None
                )

                if is_cross_customer_history_request(message_text, verified_customer_id=verified_cid):
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
                    raw_memory = ""
                    try:
                        raw_memory = self.memory_client.retrieve_summary(
                            customer_id=verified_cid,
                            query=message_text,
                            timeout_s=0.300,
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
                        recent_tickets = mcp_client.list_customer_tickets(
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
                        for tk in recent_tickets:
                            ref = tk.get("issue_reference") or "TICK-UNKNOWN"
                            cat = tk.get("category", "support")
                            st = "under review" if cat in ("security", "fraud") else tk.get("status", "open")
                            ticket_lines.append(f"- #{ref} (status: {st})")
                        sections.append("Recent support tickets:\n" + "\n".join(ticket_lines))

                    sections.append("Full chat transcripts are kept in the Gemini Enterprise app's own chat history.")
                    reply_text = "\n\n".join(sections)

                # Set turn span attributes (REQ-HIST-002)
                turn_span.set_attribute("decision", "auto_rag")
                turn_span.set_attribute("tier", "tier0_rules")
                turn_span.set_attribute("queue", "none")
                turn_span.set_attribute("degraded", False)
                turn_span.set_attribute("predicted_intent", "conversation_history_access")

                # Structured INFO event logging (REQ-HIST-002)
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
                    details={"category": "conversation_history", "outcome": outcome},
                )
                self.logger.log_decision(
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
                return TriageResult(
                    ticket_id=t_id,
                    session_id=session_id,
                    decision=hist_dec,
                    calming_ack=reply_text,
                    handoff_response={
                        "status": "AUTO_RESOLVED",
                        "route": "conversation_history",
                        "answer": reply_text,
                        "ticket_id": t_id,
                        "status_code": 200,
                        "outcome": outcome,
                    },
                    idempotency_key=idempotency_key,
                    latency_ms=elapsed_ms,
                    status_code=200,
                    error=None,
                )

            # REQ-AGENT-036: Vertex AI Agent Engine Memory Bank read for verified customers
            if (
                customer_verified
                and ticket_customer_id
                and not str(ticket_customer_id).startswith("anon:")
                and not memory_summary
            ):
                try:
                    memory_summary = self.memory_client.retrieve_summary(
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

            def _clean_decision_dict(dec: RouterDecision) -> Dict[str, Any]:
                d = asdict(dec)
                d.pop("pii_mapping", None)
                d.pop("tokenized_text", None)
                return d

            calming_ack: Optional[str] = None
            handoff_resp: Optional[Dict[str, Any]] = None
            status_code: int = 200
            error_msg: Optional[str] = None

            # 2. Branch: Escalation path vs. Auto-RAG path vs. Ingress Guard
            if decision.decision == "blocked":
                # REQ-MA-007 / REQ-AGENT-028: Ingress card data guard (TDD §4.2 C4)
                # Never persist, never save redaction map, never call A2A/HumanHandoff, never write card to memory.
                blocked_reply = render_card_data_blocked_reply()
                calming_ack = blocked_reply
                status_code = 400
                error_msg = "card_data_blocked"
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
            elif decision.decision == "security_blocked":
                # REQ-MA-003: In-agent prompt injection / jailbreak security refusal.
                # Never persist, never save redaction map, never call A2A or HumanHandoff, zero ticket creation.
                sec_ack = render_security_blocked_reply()
                calming_ack = sec_ack
                status_code = 200
                error_msg = None
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
            elif decision.decision == "escalate":
                # REQ-AGENT-030: Shared HumanHandoff workflow (persist -> publish -> calming ack)
                category = decision.details.get("category")
                handoff_res = self.human_handoff.escalate(
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
                    decision_payload=_clean_decision_dict(decision),
                )
                if decision.reason == "model_armor_security_trigger":
                    calming_ack = (
                        f"Thank you for contacting support. We have logged ticket #{t_id} and our "
                        "support team is reviewing your inquiry. A representative will follow up with you shortly."
                    )
                else:
                    calming_ack = handoff_res.calming_ack

                if handoff_res.persisted:
                    self._save_redaction_map_if_needed(
                        ticket_id=t_id,
                        ticket_uuid=canonical_uuid,
                        customer_id=ticket_customer_id,
                        mapping=pii_mapping,
                    )
            elif decision.decision == "reject_oos":
                calming_ack = render_out_of_scope_refusal(ticket_id=t_id)
                upsert_res = self._safe_upsert(
                    ticket_id=t_id,
                    idempotency_key=idempotency_key,
                    customer_id=ticket_customer_id,
                    channel=channel,
                    body_redacted=body_to_persist,
                    decision=_clean_decision_dict(decision),
                )
                ticket_uuid = (
                    upsert_res.get("ticket_id")
                    if isinstance(upsert_res, dict) and upsert_res.get("ticket_id")
                    else canonical_uuid
                )
                if upsert_res is not None:
                    self._save_redaction_map_if_needed(
                        ticket_id=t_id,
                        ticket_uuid=ticket_uuid,
                        customer_id=ticket_customer_id,
                        mapping=pii_mapping,
                    )
            elif decision.details.get("category") in ("greeting", "pleasantry"):
                upsert_res = self._safe_upsert(
                    ticket_id=t_id,
                    idempotency_key=idempotency_key,
                    customer_id=ticket_customer_id,
                    channel=channel,
                    body_redacted=body_to_persist,
                    decision=_clean_decision_dict(decision),
                )
                ticket_uuid = (
                    upsert_res.get("ticket_id")
                    if isinstance(upsert_res, dict) and upsert_res.get("ticket_id")
                    else canonical_uuid
                )
                if upsert_res is not None:
                    self._save_redaction_map_if_needed(
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
            else:
                decision_snapshot = _clean_decision_dict(decision)
                decision_snapshot["ticket_id"] = t_id
                decision_snapshot["session_id"] = session_id
                upsert_ctx = contextvars.copy_context()
                a2a_failed_exc: Optional[Exception] = None
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as upsert_pool:
                    upsert_fut = upsert_pool.submit(
                        upsert_ctx.run,
                        self._safe_upsert,
                        ticket_id=t_id,
                        idempotency_key=idempotency_key,
                        customer_id=ticket_customer_id,
                        channel=channel,
                        body_redacted=body_to_persist,
                        decision=decision_snapshot,
                    )
                    try:
                        verified_cid = cust_ctx.customer_id if cust_ctx.verified else None
                        cct = self._mint_cct_if_verified(
                            customer_verified=customer_verified,
                            customer_id=verified_cid,
                            ticket_id=t_id,
                        )
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
                        )
                        try:
                            handoff_resp = self.a2a_client.handoff(payload)
                            # REQ-LIFE-002: only verified customers may confirm, so only ask them.
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
                            if isinstance(handoff_resp, dict) and handoff_resp.get("status") == "ESCALATED":
                                if handoff_resp.get("handoff_done"):
                                    # REQ-AGENT-030: FaqResolver completed HumanHandoff; do not publish again
                                    calming_ack = str(
                                        handoff_resp.get("calming_ack")
                                        or render_calming_acknowledgement(
                                            queue=str(handoff_resp.get("queue", "human-standard")),
                                            ticket_id=t_id,
                                        )
                                    )
                                else:
                                    # Fallback when FaqResolver does not handle handoff directly
                                    handoff_res = self.human_handoff.escalate(
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
                                        decision_payload=_clean_decision_dict(decision),
                                    )
                                    calming_ack = handoff_res.calming_ack
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
                    self._save_redaction_map_if_needed(
                        ticket_id=t_id,
                        ticket_uuid=ticket_uuid,
                        customer_id=ticket_customer_id,
                        mapping=pii_mapping,
                    )
                    if (
                        isinstance(handoff_resp, dict)
                        and handoff_resp.get("status") == "AUTO_RESOLVED"
                        and hasattr(self.mcp_client, "record_resolution_confirmation_requested")
                    ):
                        try:
                            self.mcp_client.record_resolution_confirmation_requested(
                                ticket_id=t_id,
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
                    # REQ-AGENT-030: Route degraded A2A failure through HumanHandoff
                    handoff_res = self.human_handoff.escalate(
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
                        decision_payload=_clean_decision_dict(decision),
                    )
                    calming_ack = handoff_res.calming_ack
                    handoff_resp = {
                        "status": "ESCALATED",
                        "queue": decision.queue,
                        "reason": "a2a_handoff_failed",
                        "calming_ack": calming_ack,
                        "handoff_done": True,
                    }

            elapsed_ms = (time.monotonic() - start_time) * 1000.0

            # 3. Emit structured PII-free decision log (REQ-AGENT-014)
            self.logger.log_decision(
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
                    self.memory_client.generate_turn_memories(
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
            if isinstance(handoff_resp, dict):
                handoff_resp = dict(handoff_resp)
                if handoff_resp.get("answer"):
                    handoff_resp["answer"] = restore_pii_tokens(str(handoff_resp["answer"]), pii_mapping)
                if handoff_resp.get("calming_ack"):
                    handoff_resp["calming_ack"] = restore_pii_tokens(str(handoff_resp["calming_ack"]), pii_mapping)
                if handoff_resp.get("message"):
                    handoff_resp["message"] = restore_pii_tokens(str(handoff_resp["message"]), pii_mapping)

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
