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
import json
import logging
import os
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

logger = logging.getLogger(__name__)
_SIGNER_INIT_LOCK = threading.Lock()


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

            # REQ-AGENT-033: Check for active ticket holding human queue for this session
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

            if (
                isinstance(active_ticket, dict)
                and (active_ticket.get("ticket_id") or active_ticket.get("id"))
            ):
                holding_tid = str(active_ticket.get("ticket_id") or active_ticket.get("id"))
                holding_queue = str(
                    active_ticket.get("assigned_queue")
                    or active_ticket.get("queue")
                    or "human-standard"
                )
                ticket_customer_id = str(active_ticket.get("customer_id") or placeholder_cid)
                body_to_persist = (
                    tokenized_text
                    if tokenized_text
                    else (sanitized_text if sanitized_text else message_text)
                )

                # REQ-AGENT-033: Derive human-readable ticket reference (never leak DB UUID to customers)
                held_decision = active_ticket.get("decision")
                if isinstance(held_decision, str):
                    try:
                        held_decision = json.loads(held_decision)
                    except Exception:
                        held_decision = None

                stored_tid = None
                if isinstance(held_decision, dict):
                    stored_tid = held_decision.get("ticket_id")

                held_sess = (
                    active_ticket.get("session_id")
                    or (isinstance(held_decision, dict) and held_decision.get("session_id"))
                    or session_id
                )

                def _is_uuid_str(val: Any) -> bool:
                    try:
                        uuid.UUID(str(val))
                        return True
                    except (ValueError, AttributeError, TypeError):
                        return False

                if not _is_uuid_str(holding_tid):
                    customer_ticket_id = holding_tid
                elif stored_tid and not _is_uuid_str(stored_tid):
                    customer_ticket_id = str(stored_tid)
                elif held_sess:
                    customer_ticket_id = f"TICK-{str(held_sess)[:8]}"
                else:
                    customer_ticket_id = f"TICK-{session_id[:8]}"

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

            # REQ-AGENT-027: Run identity resolution concurrently with policy evaluation
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
                    text=message_text,
                    session_id=session_id,
                    customer_id=placeholder_cid,
                    verified_facts=verified_facts,
                    ticket_id=t_id,
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
                and decision.decision != "blocked"
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
        )
        resp: Dict[str, Any] = {
            "ticket_id": res.ticket_id,
            "decision": res.decision.decision,
            "queue": res.decision.queue,
            "calming_ack": res.calming_ack,
            "intent": res.decision.predicted_intent,
        }
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
    ):
        """Generator stream query entrypoint for Gemini Enterprise compatibility."""
        yield self.query(
            query,
            session_id=session_id,
            customer_id_token=customer_id_token,
            customer_id=customer_id,
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
