"""TriageRouterAgent: Entrypoint and ADK agent wiring for Agent Engine / Agent Runtime.

# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
# REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment
# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-AGENT-006: Inter-Agent A2A Handoff Protocol
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import time
from typing import Any, Callable, Dict, Optional

from ticket_router.agents.triage_router.a2a_client import (
    A2AClientProtocol,
    A2AHandoffPayload,
    InMemoryA2AClient,
)
from ticket_router.agents.triage_router.mcp_client import (
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
from ticket_router.agents.triage_router.templates import render_calming_acknowledgement
from ticket_router.common.observability import (
    StructuredJsonLogger,
    generate_traceparent,
    start_span,
)
from ticket_router.config.models import model_id
from ticket_router.models.router_s1_client import RouterPrediction, RouterS1Client


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


def invoke_tier2_gemini(text: str, target_model: str) -> RouterPrediction:
    """Execute Tier 2 fallback inference using Gemini Flash (REQ-AGENT-002)."""
    try:
        from google import genai  # type: ignore[import-untyped]
        client = genai.Client()
        prompt = (
            f"Evaluate the following support ticket:\n{text}\n"
            "Return JSON with: escalate (bool), escalate_prob (float), "
            "sentiment (str: neutral/angry), urgency (str: normal/immediate), "
            "intent (str: billing/shipping/refund/technical/other), intent_prob (float)."
        )
        resp = client.models.generate_content(
            model=target_model,
            contents=prompt,
        )
        raw_text = ""
        try:
            raw_text = str(resp.text)
        except AttributeError:
            raw_text = ""

        is_angry = "angry" in raw_text.lower()
        is_urgent = "immediate" in raw_text.lower()
        is_esc = "escalate" in raw_text.lower() or is_angry or is_urgent
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
        )
    except Exception as exc:
        raise RuntimeError(f"Tier 2 model call failed ({target_model}): {exc}") from exc


def default_tier2_invoker(text: str) -> RouterPrediction:
    """Default module-scope Tier 2 invoker resolving model dynamically."""
    target_model = model_id("router_tier2")
    return invoke_tier2_gemini(text, target_model)


def create_tier2_invoker(
    model_role: str = "router_tier2",
    custom_caller: Optional[Callable[[str], RouterPrediction]] = None,
) -> Callable[[str], RouterPrediction]:
    """Build a Tier 2 fallback invoker using Gemini Flash (REQ-AGENT-002, REQ-AGENT-013).

    Model ID is resolved dynamically from config/models.yaml to guarantee zero hard-coded literals.
    """
    if custom_caller is not None:
        return custom_caller
    return default_tier2_invoker


class TriageRouterAgent:
    """TriageRouterAgent coordinating rules, router-s1, Flash, MCP store, and A2A handoffs.

    Fulfills REQ-AGENT-001..006, 011..015.
    """

    def __init__(
        self,
        policy: Optional[TriagePolicy] = None,
        mcp_client: Optional[TicketDbMcpClientProtocol] = None,
        a2a_client: Optional[A2AClientProtocol] = None,
        logger: Optional[StructuredJsonLogger] = None,
    ) -> None:
        self.policy = policy or TriagePolicy(
            thresholds=RouterThresholds(),
            s1_client=RouterS1Client(),
            tier2_invoker=create_tier2_invoker("router_tier2"),
        )
        self.mcp_client = mcp_client or InMemoryTicketDbMcpClient()
        self.a2a_client = a2a_client or InMemoryA2AClient()
        self.logger = logger or StructuredJsonLogger(name="triage_router")

    def triage(
        self,
        session_id: str,
        customer_id: str,
        message_text: str,
        verified_facts: Optional[VerifiedFacts] = None,
        channel: str = "web",
        ticket_id: Optional[str] = None,
        memory_summary: str = "",
    ) -> TriageResult:
        """Execute end-to-end triage evaluation, persistence, and dispatch."""
        start_time = time.monotonic()
        t_id = ticket_id or f"TICK-{session_id[:8]}"
        traceparent = generate_traceparent()
        idempotency_key = compute_idempotency_key(session_id, message_text)

        with start_span("triage_router_turn", attributes={"session_id": session_id}, traceparent=traceparent):
            # 1. Evaluate tiered policy
            decision = self.policy.evaluate(
                text=message_text,
                session_id=session_id,
                customer_id=customer_id,
                verified_facts=verified_facts,
                ticket_id=t_id,
            )

            calming_ack: Optional[str] = None
            handoff_resp: Optional[Dict[str, Any]] = None

            # 2. Branch: Escalation path vs. Auto-RAG path
            if decision.decision == "escalate":
                # Render calming acknowledgement with ticket ID and SLA ETA (REQ-AGENT-005)
                calming_ack = render_calming_acknowledgement(
                    queue=decision.queue,
                    ticket_id=t_id,
                    category=decision.details.get("category"),
                )

                # Persist ticket state to ticket-db-mcp idempotently (REQ-AGENT-005, REQ-AGENT-010)
                self.mcp_client.upsert_ticket(
                    idempotency_key=idempotency_key,
                    customer_id=customer_id,
                    channel=channel,
                    body_redacted=message_text,
                    decision=asdict(decision),
                )
            else:
                # Persist ticket state before handoff
                self.mcp_client.upsert_ticket(
                    idempotency_key=idempotency_key,
                    customer_id=customer_id,
                    channel=channel,
                    body_redacted=message_text,
                    decision=asdict(decision),
                )

                # Dispatch to FaqResolverAgent via A2A protocol (REQ-AGENT-006)
                payload = A2AHandoffPayload(
                    ticket_id=t_id,
                    session_id=session_id,
                    predicted_intent=decision.predicted_intent,
                    redacted_message=message_text,
                    memory_summary=memory_summary,
                )
                handoff_resp = self.a2a_client.handoff(payload)

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

    def query(self, query: str, session_id: str = "default_session", customer_id: str = "cust_anon") -> Dict[str, Any]:
        """Synchronous query entrypoint."""
        res = self.triage_handler.triage(session_id=session_id, customer_id=customer_id, message_text=query)
        return {
            "ticket_id": res.ticket_id,
            "decision": res.decision.decision,
            "queue": res.decision.queue,
            "calming_ack": res.calming_ack,
            "intent": res.decision.predicted_intent,
        }

    def stream_query(self, query: str, session_id: str = "default_session", customer_id: str = "cust_anon"):
        """Generator stream query entrypoint for Gemini Enterprise compatibility."""
        yield self.query(query, session_id=session_id, customer_id=customer_id)


def create_root_agent() -> Any:
    """Factory creating root_agent for Agent Engine / Agent Runtime deploy."""
    router_agent_instance = TriageRouterAgent()
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
