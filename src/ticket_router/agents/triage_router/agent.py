"""TriageRouterAgent: Entrypoint and ADK agent wiring for Agent Engine / Agent Runtime.

# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
# REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment
# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-AGENT-006: Inter-Agent A2A Handoff Protocol
"""

from __future__ import annotations

import json
import logging
import os
import time
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
)
from ticket_router.agents.triage_router.templates import (
    render_calming_acknowledgement,
    render_out_of_scope_refusal,
)
from ticket_router.common.observability import (
    StructuredJsonLogger,
    generate_traceparent,
    get_current_traceparent,
    start_span,
)
from ticket_router.common.settings import Settings
from ticket_router.config.models import model_id, model_location
from ticket_router.identity import resolve_identity
from ticket_router.models.router_s1_client import RouterPrediction, RouterS1Client
from ticket_router.security.model_armor import (
    ModelArmorClient,
    ModelArmorClientProtocol,
)

logger = logging.getLogger(__name__)


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


def invoke_tier2_gemini(
    text: str,
    target_model: str,
    project: Optional[str] = None,
    location: Optional[str] = None,
) -> RouterPrediction:
    """Execute Tier 2 fallback inference using Gemini Flash (REQ-AGENT-002, REQ-AGENT-013)."""
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

        call_kwargs: Dict[str, Any] = {
            "model": target_model,
            "contents": prompt,
        }
        if sys_inst:
            gen_cfg: Any = None
            try:
                from google.genai import types as genai_types  # type: ignore[import-untyped]
                if hasattr(genai_types, "GenerateContentConfig"):
                    gen_cfg = genai_types.GenerateContentConfig(system_instruction=sys_inst)
                else:
                    gen_cfg = {"system_instruction": sys_inst}
            except Exception:
                gen_cfg = {"system_instruction": sys_inst}
            call_kwargs["config"] = gen_cfg

        resp = client.models.generate_content(**call_kwargs)

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
    ) -> None:
        self._policy = policy
        self._mcp_client = mcp_client
        self._a2a_client = a2a_client
        self._model_armor_client = model_armor_client
        self.logger = logger or StructuredJsonLogger(name="triage_router")
        self.lazy_init = lazy_init
        self._expected_oauth_client_id: Optional[str] = os.environ.get("GE_OAUTH_CLIENT_ID") or None
        if not lazy_init:
            self._ensure_initialized()

    def _ensure_initialized(self) -> None:
        """Initialize dependencies; validates environment settings when require_real_deps is True."""
        if not self.lazy_init and self._policy is not None and self._mcp_client is not None and self._a2a_client is not None:
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

        if customer_id is not None:
            logger.warning(
                "customer_id argument is deprecated and ignored; pass customer_id_token instead."
            )

        with start_span(
            "triage_router_turn",
            attributes={"session_id": session_id, "ticket_id": t_id, "channel": channel},
            traceparent=traceparent,
        ) as turn_span:
            # Resolve customer identity from token via ticket-db-mcp (REQ-IDENT-003, REQ-IDENT-004, REQ-IDENT-009)
            cust_ctx = resolve_identity(
                token=customer_id_token,
                ticket_db_client=self.mcp_client,
                oauth_access_token=oauth_access_token,
                expected_oauth_client_id=self._expected_oauth_client_id,
            )
            ticket_customer_id = cust_ctx.customer_id if cust_ctx.verified else f"anon:{session_id}"
            customer_verified = cust_ctx.verified
            identity_issuer = cust_ctx.issuer if cust_ctx.verified else None
            identity_reason = cust_ctx.reason

            # 1. Evaluate tiered policy
            decision = self.policy.evaluate(
                text=message_text,
                session_id=session_id,
                customer_id=ticket_customer_id,
                verified_facts=verified_facts,
                ticket_id=t_id,
            )
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

            # Determine body_redacted (use Model Armor de-identified text if present)
            body_to_persist = message_text
            if decision.details.get("sanitized_text"):
                body_to_persist = decision.details["sanitized_text"]

            calming_ack: Optional[str] = None
            handoff_resp: Optional[Dict[str, Any]] = None

            # 2. Branch: Escalation path vs. Auto-RAG path
            if decision.decision == "escalate":
                if decision.reason == "model_armor_security_trigger":
                    calming_ack = (
                        f"Thank you for contacting support. We have logged ticket #{t_id} and our "
                        "support team is reviewing your inquiry. A representative will follow up with you shortly."
                    )
                else:
                    calming_ack = render_calming_acknowledgement(
                        queue=decision.queue,
                        ticket_id=t_id,
                        category=decision.details.get("category"),
                    )

                self.mcp_client.upsert_ticket(
                    idempotency_key=idempotency_key,
                    customer_id=ticket_customer_id,
                    channel=channel,
                    body_redacted=body_to_persist,
                    decision=asdict(decision),
                )
            elif decision.decision == "reject_oos":
                calming_ack = render_out_of_scope_refusal(ticket_id=t_id)
                self.mcp_client.upsert_ticket(
                    idempotency_key=idempotency_key,
                    customer_id=ticket_customer_id,
                    channel=channel,
                    body_redacted=body_to_persist,
                    decision=asdict(decision),
                )
            else:
                self.mcp_client.upsert_ticket(
                    idempotency_key=idempotency_key,
                    customer_id=ticket_customer_id,
                    channel=channel,
                    body_redacted=body_to_persist,
                    decision=asdict(decision),
                )

                payload = A2AHandoffPayload(
                    ticket_id=t_id,
                    session_id=session_id,
                    predicted_intent=decision.predicted_intent,
                    redacted_message=body_to_persist,
                    memory_summary=memory_summary,
                    customer_id=cust_ctx.customer_id if cust_ctx.verified else None,
                    customer_verified=customer_verified,
                    identity_issuer=identity_issuer,
                    traceparent=get_current_traceparent() or traceparent,
                )
                try:
                    handoff_resp = self.a2a_client.handoff(payload)
                    if isinstance(handoff_resp, dict) and handoff_resp.get("status") == "ESCALATED":
                        calming_ack = str(
                            handoff_resp.get("calming_ack")
                            or render_calming_acknowledgement(
                                queue=str(handoff_resp.get("queue", "human-standard")),
                                ticket_id=t_id,
                            )
                        )
                except Exception as exc:
                    decision = replace(
                        decision,
                        decision="escalate",
                        escalate=True,
                        queue="human-standard",
                        degraded=True,
                        details={**decision.details, "a2a_error": type(exc).__name__},
                    )
                    calming_ack = render_calming_acknowledgement(
                        queue=decision.queue,
                        ticket_id=t_id,
                    )
                    handoff_resp = {
                        "status": "ESCALATED",
                        "queue": decision.queue,
                        "reason": "a2a_handoff_failed",
                        "calming_ack": calming_ack,
                    }
                    try:
                        self.mcp_client.upsert_ticket(
                            idempotency_key=f"{idempotency_key}:a2a_degraded",
                            customer_id=ticket_customer_id,
                            channel=channel,
                            body_redacted=message_text,
                            decision=asdict(decision),
                        )
                    except Exception:
                        pass

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

            return TriageResult(
                ticket_id=t_id,
                session_id=session_id,
                decision=decision,
                calming_ack=calming_ack,
                handoff_response=handoff_resp,
                idempotency_key=idempotency_key,
                latency_ms=elapsed_ms,
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
        return {
            "ticket_id": res.ticket_id,
            "decision": res.decision.decision,
            "queue": res.decision.queue,
            "calming_ack": res.calming_ack,
            "intent": res.decision.predicted_intent,
        }

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
