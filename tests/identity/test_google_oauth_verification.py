"""Unit and hermetic acceptance tests for Google OAuth 2.0 access token verification.

# REQ-IDENT-009: Gemini Enterprise OAuth Token Verification & Multi-Issuer Account Linking
# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution
# REQ-IDENT-004: Fail-Closed Anonymous Fallback & Authorization Boundaries
# REQ-IDENT-007: Identity Threat Mitigations (Anti-Spoofing, Replay, IDOR, Suspension)
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from ticket_router.agents.triage_router.adk_agent import (
    extract_oauth_access_token,
)
from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.identity import (
    GOOGLE_ISSUER,
    GOOGLE_TOKENINFO_URL,
    TokenVerificationError,
    VerifiedIdentity,
    clear_identity_cache,
    get_cached_identity,
    resolve_identity,
    verify_google_oauth_token,
)


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear in-memory identity cache before and after each test."""
    clear_identity_cache()
    yield
    clear_identity_cache()


class MockHttpResponse:
    def __init__(self, status_code: int, payload: Dict[str, Any]):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


class MockHttpSession:
    def __init__(self, responses: Optional[Dict[str, MockHttpResponse]] = None):
        self.responses = dict(responses or {})
        self.post_calls = []

    def post(self, url: str, data: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None, timeout: float = 3.0):
        self.post_calls.append({"url": url, "data": data, "params": params, "timeout": timeout})
        token = (data or {}).get("access_token")
        if token in self.responses:
            return self.responses[token]
        return MockHttpResponse(400, {"error": "invalid_token"})


def test_google_oauth_verify_and_resolve():
    """Acceptance test for REQ-IDENT-009: verify Google OAuth token and resolve customer."""
    # REQ-IDENT-009
    raw_token = "fake-oauth-valid-oauth-token-1001"
    sub_uid = "112824255405214937284"
    now = 1700000000.0

    session = MockHttpSession({
        raw_token: MockHttpResponse(200, {
            "sub": sub_uid,
            "aud": "my-client-app.apps.googleusercontent.com",
            "expires_in": "3600",
            "email_verified": "true",
        })
    })

    # 1. Verify token
    verified = verify_google_oauth_token(
        access_token=f"Bearer {raw_token}",
        expected_client_id="my-client-app.apps.googleusercontent.com",
        session=session,
        clock=lambda: now,
    )

    assert isinstance(verified, VerifiedIdentity)
    assert verified.issuer == GOOGLE_ISSUER
    assert verified.subject == sub_uid
    assert verified.email_verified is True
    assert verified.exp == now + 3600.0

    # Ensure access token is never sent in URL query params
    assert len(session.post_calls) == 1
    call = session.post_calls[0]
    assert call["url"] == GOOGLE_TOKENINFO_URL
    assert call["data"] == {"access_token": raw_token}
    assert call["params"] is None

    # 2. Resolve via ticket-db-mcp
    fake_ticket_db = InMemoryTicketDbMcpClient()
    fake_ticket_db.register_identity(
        issuer=GOOGLE_ISSUER,
        subject=sub_uid,
        customer_id="cust_1001",
        status="active",
        linked=True,
    )

    ctx = resolve_identity(
        token=None,
        ticket_db_client=fake_ticket_db,
        oauth_access_token=raw_token,
        expected_oauth_client_id="my-client-app.apps.googleusercontent.com",
        oauth_verifier=lambda tok, expected_client_id=None, clock=None: verified,
        clock=lambda: now,
    )

    assert ctx.verified is True
    assert ctx.customer_id == "cust_1001"
    assert ctx.issuer == GOOGLE_ISSUER
    assert ctx.reason == "ok"
    assert ctx.subject == sub_uid

    # 3. Verify caching under "oauth:" namespace
    cached = get_cached_identity(f"oauth:{raw_token}", clock=lambda: now)
    assert cached is not None
    cached_ident, cached_ctx = cached
    assert cached_ident.issuer == GOOGLE_ISSUER
    assert cached_ctx.customer_id == "cust_1001"


def test_google_oauth_precedence():
    """Verify that customer_id_token takes strict precedence over oauth_access_token."""
    # REQ-IDENT-003, REQ-IDENT-009
    fake_ticket_db = InMemoryTicketDbMcpClient()
    fake_ticket_db.register_identity(
        issuer="https://securetoken.google.com/susmith-fde-ticket-router",
        subject="firebase_sub_1001",
        customer_id="cust_1001",
        status="active",
        linked=True,
    )

    id_token = "valid.firebase.jwt"
    oauth_token = "fake-oauth-oauth-token"

    mock_firebase_verified = VerifiedIdentity(
        issuer="https://securetoken.google.com/susmith-fde-ticket-router",
        subject="firebase_sub_1001",
        email_verified=True,
        exp=2000000000.0,
    )

    # Monkeypatch verify_customer_token to return mock_firebase_verified
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "ticket_router.identity.resolver.verify_customer_token",
            lambda **kwargs: mock_firebase_verified,
        )
        ctx = resolve_identity(
            token=id_token,
            ticket_db_client=fake_ticket_db,
            oauth_access_token=oauth_token,
        )

    assert ctx.verified is True
    assert ctx.customer_id == "cust_1001"
    assert ctx.issuer == "https://securetoken.google.com/susmith-fde-ticket-router"


def test_google_oauth_client_id_validation():
    """Verify client_id replay mitigation accepts expected audience and rejects others."""
    # REQ-IDENT-009
    raw_token = "fake-oauth-test-replay"
    now = 1700000000.0

    session = MockHttpSession({
        raw_token: MockHttpResponse(200, {
            "sub": "sub_xyz",
            "aud": "client-A.apps.googleusercontent.com",
            "azp": "client-A.apps.googleusercontent.com",
            "expires_in": "1800",
        })
    })

    # Matching client ID succeeds
    verified = verify_google_oauth_token(
        access_token=raw_token,
        expected_client_id="client-A.apps.googleusercontent.com",
        session=session,
        clock=lambda: now,
    )
    assert verified.subject == "sub_xyz"

    # Mismatched client ID raises TokenVerificationError
    with pytest.raises(TokenVerificationError, match="client_id mismatch"):
        verify_google_oauth_token(
            access_token=raw_token,
            expected_client_id="client-B.apps.googleusercontent.com",
            session=session,
            clock=lambda: now,
        )


def test_google_oauth_fail_closed_scenarios():
    """Verify that expired, malformed, unlinked, or suspended tokens all fail closed."""
    # REQ-IDENT-004, REQ-IDENT-009
    now = 1700000000.0

    # 1. Expired token (expires_in = 0 or negative)
    sess_expired = MockHttpSession({
        "tok_exp": MockHttpResponse(200, {
            "sub": "sub_1",
            "expires_in": 0,
        })
    })
    with pytest.raises(TokenVerificationError, match="expired or missing expires_in"):
        verify_google_oauth_token("tok_exp", session=sess_expired, clock=lambda: now)

    # 2. HTTP 400 from Google tokeninfo
    sess_400 = MockHttpSession({
        "tok_bad": MockHttpResponse(400, {"error_description": "Invalid Value"})
    })
    with pytest.raises(TokenVerificationError, match="rejected token"):
        verify_google_oauth_token("tok_bad", session=sess_400, clock=lambda: now)

    # 3. Missing sub claim
    sess_nosub = MockHttpSession({
        "tok_nosub": MockHttpResponse(200, {
            "aud": "some-client",
            "expires_in": 3600,
        })
    })
    with pytest.raises(TokenVerificationError, match="missing non-empty subject"):
        verify_google_oauth_token("tok_nosub", session=sess_nosub, clock=lambda: now)

    # 4. Unlinked account resolves to fail-closed anonymous
    fake_ticket_db = InMemoryTicketDbMcpClient()
    verified_unlinked = VerifiedIdentity(
        issuer=GOOGLE_ISSUER,
        subject="sub_unlinked",
        email_verified=True,
        exp=now + 3600.0,
    )
    ctx_unlinked = resolve_identity(
        token=None,
        ticket_db_client=fake_ticket_db,
        oauth_access_token="tok_unlinked",
        oauth_verifier=lambda *a, **k: verified_unlinked,
        clock=lambda: now,
    )
    assert ctx_unlinked.verified is False
    assert ctx_unlinked.customer_id is None
    assert ctx_unlinked.reason == "unlinked"

    # 5. Suspended customer resolves to fail-closed
    fake_ticket_db.register_identity(
        issuer=GOOGLE_ISSUER,
        subject="sub_suspended",
        customer_id="cust_suspended",
        status="suspended",
        linked=True,
    )
    verified_suspended = VerifiedIdentity(
        issuer=GOOGLE_ISSUER,
        subject="sub_suspended",
        email_verified=True,
        exp=now + 3600.0,
    )
    ctx_suspended = resolve_identity(
        token=None,
        ticket_db_client=fake_ticket_db,
        oauth_access_token="tok_suspended",
        oauth_verifier=lambda *a, **k: verified_suspended,
        clock=lambda: now,
    )
    assert ctx_suspended.verified is False
    assert ctx_suspended.customer_id is None
    assert ctx_suspended.reason == "suspended"


class DummySession:
    def __init__(self, state: Dict[str, Any]):
        self.id = "session-test-oauth"
        self.state = state


class DummyContext:
    def __init__(self, session: DummySession, text: str = "Where is my order?"):
        self.session = session
        self.user_content = text
        self.invocation_id = "inv-123"
        self.branch = None


class CaptureA2AClient:
    def __init__(self):
        self.last_payload = None

    def handoff(self, payload):
        self.last_payload = payload
        return {"status": "ok", "answer": "Your package is on its way."}


class ImmediatePolicy:
    def evaluate(self, **kwargs):
        from ticket_router.agents.triage_router.policy import RouterDecision
        return RouterDecision(
            ticket_id=kwargs.get("ticket_id") or "TICK-1",
            session_id=kwargs.get("session_id") or "sess-1",
            decision="auto_rag",
            tier="tier1_s1",
            queue="none",
            predicted_intent="shipping_policy",
            escalate=False,
            sentiment="neutral",
            urgency="normal",
            reason="faq",
        )


def test_adk_session_extraction_and_propagation(monkeypatch):
    """Verify ADK session state extraction across formats and A2A payload propagation."""
    # REQ-IDENT-009, REQ-AGENT-006

    # Case 1: temp:support-customer-oauth string
    ctx1 = DummyContext(DummySession({"temp:support-customer-oauth": "fake-oauth-token-1"}))
    assert extract_oauth_access_token(ctx1) == "fake-oauth-token-1"

    # Case 2: support-customer-oauth string
    ctx2 = DummyContext(DummySession({"support-customer-oauth": "fake-oauth-token-2"}))
    assert extract_oauth_access_token(ctx2) == "fake-oauth-token-2"

    # Case 3: dict with access_token
    ctx3 = DummyContext(DummySession({"temp:support-customer-oauth": {"access_token": "fake-oauth-token-3"}}))
    assert extract_oauth_access_token(ctx3) == "fake-oauth-token-3"

    # Case 4: custom GE_OAUTH_AUTH_ID
    monkeypatch.setenv("GE_OAUTH_AUTH_ID", "custom-oauth-id")
    ctx4 = DummyContext(DummySession({"temp:custom-oauth-id": "fake-oauth-token-4"}))
    assert extract_oauth_access_token(ctx4) == "fake-oauth-token-4"

    # Case 5: End-to-end TriageRouterAgent with OAuth resolution and A2A propagation
    fake_ticket_db = InMemoryTicketDbMcpClient()
    fake_ticket_db.register_identity(
        issuer=GOOGLE_ISSUER,
        subject="google_sub_1001",
        customer_id="cust_1001",
        status="active",
        linked=True,
    )

    capture_a2a = CaptureA2AClient()

    agent = TriageRouterAgent(
        policy=ImmediatePolicy(),
        mcp_client=fake_ticket_db,
        a2a_client=capture_a2a,
        lazy_init=False,
    )

    # Mock token verification
    verified_oauth = VerifiedIdentity(
        issuer=GOOGLE_ISSUER,
        subject="google_sub_1001",
        email_verified=True,
        exp=1800000000.0,
    )
    monkeypatch.setattr(
        "ticket_router.identity.resolver.verify_google_oauth_token",
        lambda *a, **k: verified_oauth,
    )

    res = agent.triage(
        session_id="sess-oauth-1001",
        message_text="When will my package arrive?",
        oauth_access_token="fake-oauth-token-e2e",
    )

    assert res.decision.customer_verified is True
    assert res.decision.identity_issuer == GOOGLE_ISSUER
    assert capture_a2a.last_payload is not None
    assert capture_a2a.last_payload.customer_id == "cust_1001"
    assert capture_a2a.last_payload.customer_verified is True
    assert capture_a2a.last_payload.identity_issuer == GOOGLE_ISSUER
