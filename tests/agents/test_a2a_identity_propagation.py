"""Acceptance tests for End-to-End A2A and MCP Identity Trust Chain Propagation.

# REQ-IDENT-005: Cryptographic Trust Chain Propagation across A2A & MCP
# REQ-AGENT-006: Inter-Agent A2A Handoff Protocol
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
"""

from __future__ import annotations

import time
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from ticket_router.agents.faq_resolver.a2a import handle_a2a_request
from ticket_router.agents.faq_resolver.agent import FakeLLMClient, FaqResolverAgent
from ticket_router.agents.faq_resolver.mcp_client import FakeOrderLookupClient, HttpOrderLookupClient
from ticket_router.agents.triage_router.a2a_client import A2AHandoffPayload, InMemoryA2AClient
from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.agents.triage_router.policy import RouterPrediction, RouterThresholds, TriagePolicy
from ticket_router.identity import clear_identity_cache
from ticket_router.models.router_s1_client import RouterS1Client

PROJECT_ID = "susmith-fde-ticket-router"
ISSUER = f"https://securetoken.google.com/{PROJECT_ID}"


@pytest.fixture(autouse=True)
def reset_cache():
    clear_identity_cache()
    yield
    clear_identity_cache()


def test_identity_trust_chain(monkeypatch):
    """Verify cryptographic trust chain propagation from TriageRouter to FaqResolver and MCP.

    # REQ-IDENT-005
    # REQ-AGENT-006
    # REQ-AGENT-010
    - Verified identity propagates through A2A and sends X-Customer-ID / X-Customer-Verified headers to MCP.
    - Unverified / anonymous identity is denied order lookup and receives polite sign-in prompt with human handoff.
    """
    now = time.time()

    # 1. Mock token verification
    def mock_verify_firebase(token: str, request: Any = None, audience: str = PROJECT_ID):
        if token == "token_alice_verified":
            return {
                "iss": ISSUER,
                "aud": PROJECT_ID,
                "sub": "alice_uid",
                "email": "alice@example.com",
                "email_verified": True,
                "exp": now + 3600.0,
            }
        raise ValueError("Invalid token")

    monkeypatch.setattr("google.oauth2.id_token.verify_firebase_token", mock_verify_firebase)

    # 2. Database client setup
    db_client = InMemoryTicketDbMcpClient()
    db_client.register_identity(
        issuer=ISSUER,
        subject="alice_uid",
        customer_id="cust_1001",
        status="active",
        linked=True,
    )

    # -------------------------------------------------------------------------
    # Branch 1: Verified Customer Flow
    # -------------------------------------------------------------------------
    # Setup FaqResolver with mocked order client
    mock_order_client = FakeOrderLookupClient()
    mock_order_client.add_order(
        order_id="ORD-9999",
        customer_id="cust_1001",
        status="DELIVERED",
        items=["Noise Cancelling Headphones"],
    )

    faq_agent = FaqResolverAgent(
        search_client=None,
        reranker=None,
        order_client=mock_order_client,
        llm_client=FakeLLMClient(
            default_answer="Your order ORD-9999 has been delivered.",
            default_quote="delivered",
            default_doc_id="faq-orders",
        ),
    )

    # TriageRouter dispatching A2A to FaqResolver
    a2a_handoffs: list[A2AHandoffPayload] = []

    def fake_a2a_dispatch(payload: A2AHandoffPayload) -> Dict[str, Any]:
        a2a_handoffs.append(payload)
        # Call FaqResolver A2A handler directly
        return handle_a2a_request(
            payload={
                "ticket_id": payload.ticket_id,
                "session_id": payload.session_id,
                "redacted_message": payload.redacted_message,
                "customer_id": payload.customer_id,
                "customer_verified": payload.customer_verified,
                "identity_issuer": payload.identity_issuer,
                "predicted_intent": payload.predicted_intent,
            },
            agent=faq_agent,
        )

    triage_a2a_client = InMemoryA2AClient(response_generator=fake_a2a_dispatch)

    # Mock RouterS1Client for definitive auto-RAG prediction
    mock_s1 = MagicMock(spec=RouterS1Client)
    mock_s1.route.return_value = RouterPrediction(
        escalate=False,
        escalate_prob=0.04,
        sentiment="neutral",
        sentiment_prob=0.95,
        urgency="normal",
        urgency_prob=0.95,
        intent="shipping",
        intent_prob=0.95,
    )

    policy = TriagePolicy(
        thresholds=RouterThresholds(tau_esc=0.35),
        s1_client=mock_s1,
    )
    triage_router = TriageRouterAgent(
        policy=policy,
        mcp_client=db_client,
        a2a_client=triage_a2a_client,
    )

    # Turn 1: Alice provides valid token
    res_verified = triage_router.triage(
        session_id="session_alice",
        message_text="What is the delivery status of my order ORD-9999?",
        customer_id_token="token_alice_verified",
    )

    # Verify TriageRouter outcomes
    assert res_verified.decision.decision == "auto_rag"
    assert res_verified.decision.customer_verified is True
    assert res_verified.decision.identity_reason == "ok"
    assert res_verified.decision.identity_issuer == ISSUER

    # Verify ticket row in MCP
    ticket_row = list(db_client.tickets.values())[0]
    assert ticket_row["customer_id"] == "cust_1001"
    assert ticket_row["decision"]["customer_verified"] is True

    # Verify A2A handoff payload contains verified identity
    assert len(a2a_handoffs) == 1
    a2a_payload = a2a_handoffs[0]
    assert a2a_payload.customer_id == "cust_1001"
    assert a2a_payload.customer_verified is True
    assert a2a_payload.identity_issuer == ISSUER

    # Verify Order Lookup client was invoked with verified customer_id
    assert len(mock_order_client.call_history) == 1
    call = mock_order_client.call_history[0]
    assert call["order_id"] == "ORD-9999"
    assert call["customer_id"] == "cust_1001"
    assert call["headers"]["X-Customer-ID"] == "cust_1001"
    assert call["headers"]["X-Customer-Verified"] == "true"

    # Verify HttpOrderLookupClient sends X-Customer-Verified and X-Customer-ID headers
    mock_http_session = MagicMock()
    mock_http_session.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"order_id": "ORD-9999", "status": "DELIVERED"},
        raise_for_status=lambda: None,
    )
    http_order_client = HttpOrderLookupClient(
        base_url="http://order-lookup-mcp",
        session=mock_http_session,
    )
    http_order_client.get_order(order_id="ORD-9999", customer_id="cust_1001")
    http_call_kwargs = mock_http_session.post.call_args.kwargs
    sent_headers = http_call_kwargs["headers"]
    assert sent_headers["X-Customer-ID"] == "cust_1001"
    assert sent_headers["X-Customer-Verified"] == "true"

    # -------------------------------------------------------------------------
    # Branch 2: Unverified / Anonymous Customer Flow
    # -------------------------------------------------------------------------
    mock_order_client.call_history.clear()
    a2a_handoffs.clear()

    # Turn 2: Anonymous user with NO token asks for specific order status
    res_unverified = triage_router.triage(
        session_id="session_anon_42",
        message_text="Check the tracking details for order ORD-9999 please",
        customer_id_token=None,
    )

    # Verify TriageRouter outcomes
    assert res_unverified.decision.customer_verified is False
    assert res_unverified.decision.identity_reason == "missing_token"

    # Anonymous ticket row
    ticket_row2 = list(db_client.tickets.values())[-1]
    assert ticket_row2["customer_id"] == "anon:session_anon_42"
    assert ticket_row2["decision"]["customer_verified"] is False

    # Verify A2A payload has unverified identity
    assert len(a2a_handoffs) == 1
    a2a_payload2 = a2a_handoffs[0]
    assert a2a_payload2.customer_id is None
    assert a2a_payload2.customer_verified is False

    # FaqResolver MUST NOT call order lookup for unverified customer
    assert len(mock_order_client.call_history) == 0

    # FaqResolver response must return polite sign-in prompt with human handoff option
    assert res_unverified.handoff_response is not None
    answer_text = str(res_unverified.handoff_response.get("answer", ""))
    assert "sign in" in answer_text.lower()
    assert "support specialist" in answer_text.lower() or "human" in answer_text.lower()
