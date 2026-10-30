"""Tests verifying FaqResolver forwards Customer Context Token (CCT) byte-identical.

# REQ-AUTH-003: CCT Transport and Verification at order-lookup-mcp (RLS from verified claims)
"""

from __future__ import annotations

import urllib.request
from typing import Any, Dict, List, Optional

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from ticket_router.agents.faq_resolver.a2a import (
    FAQ_RESOLVER_AGENT_CARD,
    handle_a2a_request,
)
from ticket_router.agents.faq_resolver.agent import FakeLLMClient, FaqResolverAgent
from ticket_router.agents.faq_resolver.app import create_fastapi_app
from ticket_router.agents.faq_resolver.mcp_client import (
    FakeOrderLookupClient,
    HttpOrderLookupClient,
)
from ticket_router.common.settings import Settings
from ticket_router.identity.customer_token import (
    LocalEcSigner,
    StaticPublicKeyResolver,
    mint_customer_context_token,
)
from ticket_router.mcp.order_lookup.server import create_app as create_order_lookup_app
from ticket_router.store.fake import FakeOrderRepo
from ticket_router.store.models import Order

KEY_PREFIX = (
    "projects/susmith-fde-ticket-router/locations/us-central1/"
    "keyRings/ticket-router-identity/cryptoKeys/customer-context-signer/"
)
KID = f"{KEY_PREFIX}cryptoKeyVersions/1"


class _FakeHttpResponse:
    def __init__(self, payload: Dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Dict[str, Any]:
        return self._payload


class _RecordingHttpSession:
    """Fake requests.Session recording exact URLs, headers, and bodies."""

    def __init__(self, response_payload: Optional[Dict[str, Any]] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._response_payload = response_payload or {
            "order_id": "ORD-1001",
            "status": "SHIPPED",
            "items": [{"sku": "SKU-1", "name": "Wireless Headphones"}],
            "tracking_number": "TRK-001",
            "promised_delivery": "2026-11-02",
            "delivery_address_redacted": "[Seattle, WA]",
        }

    def post(
        self,
        url: str,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> _FakeHttpResponse:
        self.calls.append(
            {
                "url": url,
                "data": data,
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        if url.endswith("/tools/list_orders"):
            return _FakeHttpResponse({"orders": [self._response_payload]})
        return _FakeHttpResponse(self._response_payload)


# REQ-AUTH-003
def test_forward_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify A2A customer_context_token arrives byte-identical in X-Customer-Context (plus absence case)."""
    # Schema advertises optional customer_context_token
    props = FAQ_RESOLVER_AGENT_CARD["input_schema"]["properties"]
    assert "customer_context_token" in props

    priv_key = ec.generate_private_key(ec.SECP256R1())
    signer = LocalEcSigner(private_key=priv_key, kid=KID)
    jws_token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id="TICK-FWD-1",
    )

    # Also test an opaque non-JWT token string to prove FaqResolver never decodes or re-signs
    opaque_token = "opaque-non-jwt.ExactBytes_Az09-~!@#$%^&*().raw_sig_segment"

    for exact_token in (jws_token, opaque_token):
        # 1. get_order path via A2A handler -> FaqResolverAgent -> HttpOrderLookupClient
        fake_session = _RecordingHttpSession()
        http_order_client = HttpOrderLookupClient(
            base_url="http://order-lookup-mcp:8080",
            session=fake_session,
            token_provider=lambda _aud: "mock-oidc-token",
        )
        faq_agent = FaqResolverAgent(
            search_client=None,
            reranker=None,
            order_client=http_order_client,
            llm_client=FakeLLMClient(
                default_answer="Order ORD-1001 has shipped.",
                default_doc_id="faq-orders",
                default_quote="shipped",
            ),
        )

        a2a_payload = {
            "ticket_id": "TICK-FWD-1",
            "session_id": "sess-fwd-1",
            "redacted_message": "What is the status of order ORD-1001?",
            "customer_id": "cust_1001",
            "customer_verified": True,
            "identity_issuer": "https://accounts.google.com",
            "predicted_intent": "order_status",
            "customer_context_token": exact_token,
        }
        handle_a2a_request(a2a_payload, agent=faq_agent)

        assert len(fake_session.calls) == 1
        get_call = fake_session.calls[0]
        assert get_call["url"] == "http://order-lookup-mcp:8080/tools/get_order"
        sent_headers = get_call["headers"]
        assert "X-Customer-Context" in sent_headers
        assert sent_headers["X-Customer-Context"] == exact_token
        assert sent_headers["X-Customer-Context"].encode("utf-8") == exact_token.encode("utf-8")
        # Legacy headers retained during CCT_MODE=accept rollout
        assert sent_headers["X-Customer-ID"] == "cust_1001"
        assert sent_headers["X-Customer-Verified"] == "true"

        # 2. list_orders path via A2A handler -> FaqResolverAgent -> HttpOrderLookupClient
        fake_session.calls.clear()
        list_payload = {
            "ticket_id": "TICK-FWD-2",
            "session_id": "sess-fwd-2",
            "redacted_message": "Where are my recent orders?",
            "customer_id": "cust_1001",
            "customer_verified": True,
            "identity_issuer": "https://accounts.google.com",
            "predicted_intent": "order_status",
            "customer_context_token": exact_token,
        }
        handle_a2a_request(list_payload, agent=faq_agent)

        assert len(fake_session.calls) == 1
        list_call = fake_session.calls[0]
        assert list_call["url"] == "http://order-lookup-mcp:8080/tools/list_orders"
        list_headers = list_call["headers"]
        assert "X-Customer-Context" in list_headers
        assert list_headers["X-Customer-Context"] == exact_token
        assert list_headers["X-Customer-Context"].encode("utf-8") == exact_token.encode("utf-8")
        assert list_headers["X-Customer-ID"] == "cust_1001"
        assert list_headers["X-Customer-Verified"] == "true"

    # 3. Absence case (omitted and explicit None): behaves identically to legacy path (no X-Customer-Context)
    for absent_payload_extra in ({}, {"customer_context_token": None}):
        fake_session_absent = _RecordingHttpSession()
        http_client_absent = HttpOrderLookupClient(
            base_url="http://order-lookup-mcp:8080",
            session=fake_session_absent,
            token_provider=lambda _aud: "mock-oidc-token",
        )
        faq_agent_absent = FaqResolverAgent(
            search_client=None,
            reranker=None,
            order_client=http_client_absent,
            llm_client=FakeLLMClient(
                default_answer="Order ORD-1001 has shipped.",
                default_doc_id="faq-orders",
                default_quote="shipped",
            ),
        )

        handle_a2a_request(
            {
                "ticket_id": "TICK-ABS-1",
                "session_id": "sess-abs-1",
                "redacted_message": "What is the status of order ORD-1001?",
                "customer_id": "cust_1001",
                "customer_verified": True,
                "predicted_intent": "order_status",
                **absent_payload_extra,
            },
            agent=faq_agent_absent,
        )
        assert len(fake_session_absent.calls) == 1
        absent_headers = fake_session_absent.calls[0]["headers"]
        assert "X-Customer-Context" not in absent_headers
        assert absent_headers["X-Customer-ID"] == "cust_1001"
        assert absent_headers["X-Customer-Verified"] == "true"

        fake_session_absent.calls.clear()
        handle_a2a_request(
            {
                "ticket_id": "TICK-ABS-2",
                "session_id": "sess-abs-2",
                "redacted_message": "Show my recent orders",
                "customer_id": "cust_1001",
                "customer_verified": True,
                "predicted_intent": "order_status",
                **absent_payload_extra,
            },
            agent=faq_agent_absent,
        )
        assert len(fake_session_absent.calls) == 1
        absent_list_headers = fake_session_absent.calls[0]["headers"]
        assert "X-Customer-Context" not in absent_list_headers
        assert absent_list_headers["X-Customer-ID"] == "cust_1001"
        assert absent_list_headers["X-Customer-Verified"] == "true"


# REQ-AUTH-003
def test_forward_through_fastapi_app_and_fake_client_to_require_mode_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify end-to-end transport through FaqResolver FastAPI /a2a/handoff into order-lookup-mcp (CCT_MODE=require)."""
    priv_key = ec.generate_private_key(ec.SECP256R1())
    pub_key = priv_key.public_key()
    signer = LocalEcSigner(private_key=priv_key, kid=KID)
    resolver = StaticPublicKeyResolver({KID: pub_key})

    cct = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id="TICK-E2E-1",
    )

    # Verify FakeOrderLookupClient also records X-Customer-Context unchanged on get_order & list_orders
    fake_order_client = FakeOrderLookupClient()
    fake_order_client.add_order(order_id="ORD-1001", customer_id="cust_1001", status="DELIVERED")
    fake_order_client.get_order(
        order_id="ORD-1001",
        customer_id="cust_1001",
        customer_context_token=cct,
    )
    fake_order_client.list_orders(
        customer_id="cust_1001",
        limit=5,
        customer_context_token=cct,
    )
    assert fake_order_client.call_history[0]["headers"]["X-Customer-Context"] == cct
    assert fake_order_client.call_history[1]["headers"]["X-Customer-Context"] == cct

    # Stand up in-process order-lookup-mcp in strict CCT_MODE="require"
    repo = FakeOrderRepo()
    repo.add_order(
        Order(
            order_id="ORD-1001",
            customer_id="cust_1001",
            order_status="DELIVERED",
            items=[{"sku": "SKU-1", "name": "Headphones"}],
            total_cents=9900,
        )
    )
    order_mcp_app = create_order_lookup_app(
        order_repo=repo,
        cct_mode="require",
        cct_resolver=resolver,
        cct_audience="order-lookup-mcp",
        cct_allowed_issuers=["triage-router"],
        cct_key_prefix=KEY_PREFIX,
    )
    order_mcp_client = TestClient(order_mcp_app)

    class _TestClientAdapterSession:
        def post(
            self,
            url: str,
            data: bytes,
            headers: Dict[str, str],
            timeout: float,
        ) -> _FakeHttpResponse:
            path = "/" + url.split("://", 1)[1].split("/", 1)[1]
            resp = order_mcp_client.post(path, content=data, headers=headers)
            return _FakeHttpResponse(resp.json(), status_code=resp.status_code)

    http_order_client = HttpOrderLookupClient(
        base_url="http://order-lookup-mcp",
        session=_TestClientAdapterSession(),
        token_provider=lambda _aud: None,
    )
    faq_agent = FaqResolverAgent(
        search_client=None,
        reranker=None,
        order_client=http_order_client,
        llm_client=FakeLLMClient(
            default_answer="Your order ORD-1001 was delivered.",
            default_doc_id="faq-orders",
            default_quote="delivered",
        ),
    )
    settings = Settings.from_env({"REQUIRE_REAL_DEPS": "0"})
    faq_http_app = create_fastapi_app(agent=faq_agent, settings=settings)
    faq_test_client = TestClient(faq_http_app)

    resp = faq_test_client.post(
        "/a2a/handoff",
        json={
            "ticket_id": "TICK-E2E-1",
            "session_id": "sess-e2e-1",
            "redacted_message": "What is the status of order ORD-1001?",
            "customer_id": "cust_1001",
            "customer_verified": True,
            "predicted_intent": "order_status",
            "customer_context_token": cct,
        },
    )
    assert resp.status_code == 200
    _ = urllib.request
    assert len(faq_agent.llm_client.history) >= 1  # type: ignore[union-attr]
    recorded_order = faq_agent.llm_client.history[0]["order_info"]  # type: ignore[union-attr]
    assert recorded_order["order_id"] == "ORD-1001"
    assert recorded_order["status"] == "DELIVERED"
