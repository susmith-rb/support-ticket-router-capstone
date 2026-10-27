"""Unit and hermetic acceptance tests for Customer Identity Platform token verification.

# REQ-IDENT-001: Cloud Identity Platform IdP Configuration & Token Format
# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution via ticket-db-mcp
# REQ-IDENT-004: Fail-Closed Anonymous Fallback & Authorization Boundaries
# REQ-IDENT-007: Identity Threat Mitigations (Anti-Spoofing, Replay, IDOR, Suspension)
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from ticket_router.agents.triage_router.adk_agent import (
    TriageRouterAdkAgent,
    extract_customer_id_token,
)
from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.agents.triage_router.policy import RouterPrediction, RouterThresholds, TriagePolicy
from ticket_router.identity import (
    CustomerContext,
    VerifiedIdentity,
    clear_identity_cache,
    resolve_identity,
    verify_customer_token,
)
from ticket_router.models.router_s1_client import RouterS1Client

PROJECT_ID = "susmith-fde-ticket-router"
ISSUER = f"https://securetoken.google.com/{PROJECT_ID}"


def _make_mock_firebase_verifier(claims_map: Dict[str, Dict[str, Any]]):
    """Factory creating hermetic verify_firebase_token mock returning configured claims."""

    def mock_verify(token: str, request: Any = None, audience: str = PROJECT_ID):
        if token in claims_map:
            claims = dict(claims_map[token])
            # If audience check simulated
            if claims.get("aud") != audience:
                raise ValueError(f"Audience mismatch: expected {audience}, got {claims.get('aud')}")
            return claims
        raise ValueError("Invalid Firebase JWT signature or token format")

    return mock_verify


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear in-memory identity cache before each test."""
    clear_identity_cache()
    yield
    clear_identity_cache()


# -----------------------------------------------------------------------------
# REQ-IDENT-001: Cloud Identity Platform IdP Configuration & Token Format
# -----------------------------------------------------------------------------

def test_idp_token_claims_and_format(monkeypatch):
    """Verify Firebase ID token claims format (iss, aud, sub, exp, email_verified)."""
    # REQ-IDENT-001
    now = 1700000000.0
    valid_claims = {
        "iss": ISSUER,
        "aud": PROJECT_ID,
        "sub": "user_uid_1001",
        "email": "cust1001@example.com",
        "email_verified": False,  # recorded, NOT required
        "exp": now + 3600.0,
        "iat": now - 60.0,
    }
    mock_verifier = _make_mock_firebase_verifier({"token_1001": valid_claims})
    monkeypatch.setattr("google.oauth2.id_token.verify_firebase_token", mock_verifier)

    identity = verify_customer_token(
        token="token_1001",
        project_id=PROJECT_ID,
        clock=lambda: now,
    )

    assert isinstance(identity, VerifiedIdentity)
    assert identity.issuer == ISSUER
    assert identity.subject == "user_uid_1001"
    assert identity.email_verified is False
    assert identity.exp == now + 3600.0


# -----------------------------------------------------------------------------
# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution via ticket-db-mcp
# -----------------------------------------------------------------------------

def test_verify_and_resolve_customer(monkeypatch):
    """Verify customer ID token verification and database customer_id resolution."""
    # REQ-IDENT-003
    now = 1700000000.0
    valid_claims = {
        "iss": ISSUER,
        "aud": PROJECT_ID,
        "sub": "user_uid_1001",
        "email_verified": True,
        "exp": now + 3600.0,
    }
    mock_verifier = _make_mock_firebase_verifier({"token_active": valid_claims})
    monkeypatch.setattr("google.oauth2.id_token.verify_firebase_token", mock_verifier)

    db_client = InMemoryTicketDbMcpClient()
    db_client.register_identity(
        issuer=ISSUER,
        subject="user_uid_1001",
        customer_id="cust_1001",
        status="active",
        linked=True,
    )

    context = resolve_identity(
        token="token_active",
        ticket_db_client=db_client,
        project_id=PROJECT_ID,
        clock=lambda: now,
    )

    assert isinstance(context, CustomerContext)
    assert context.verified is True
    assert context.customer_id == "cust_1001"
    assert context.issuer == ISSUER
    assert context.subject == "user_uid_1001"
    assert context.reason == "ok"
    assert context.email_verified is True


# -----------------------------------------------------------------------------
# REQ-IDENT-004: Fail-Closed Anonymous Fallback & Authorization Boundaries
# -----------------------------------------------------------------------------

def test_fail_closed_anonymous_fallback(monkeypatch):
    """Verify system fails closed to anonymous without dropping tickets or breaking escalation."""
    # REQ-IDENT-004
    now = 1700000000.0
    claims = {
        "unlinked_token": {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "user_uid_unlinked",
            "exp": now + 3600.0,
        },
        "expired_token": {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "user_uid_expired",
            "exp": now - 100.0,
        },
    }
    mock_verifier = _make_mock_firebase_verifier(claims)
    monkeypatch.setattr("google.oauth2.id_token.verify_firebase_token", mock_verifier)

    db_client = InMemoryTicketDbMcpClient()

    # 1. Missing token -> fail closed
    ctx_missing = resolve_identity(None, db_client, project_id=PROJECT_ID, clock=lambda: now)
    assert ctx_missing.verified is False
    assert ctx_missing.customer_id is None
    assert ctx_missing.reason == "missing_token"

    # 2. Invalid token -> fail closed
    ctx_invalid = resolve_identity("corrupt_token_xyz", db_client, project_id=PROJECT_ID, clock=lambda: now)
    assert ctx_invalid.verified is False
    assert ctx_invalid.customer_id is None
    assert ctx_invalid.reason == "invalid_token"

    # 3. Expired token -> fail closed
    ctx_expired = resolve_identity("expired_token", db_client, project_id=PROJECT_ID, clock=lambda: now)
    assert ctx_expired.verified is False
    assert ctx_expired.customer_id is None
    assert ctx_expired.reason == "expired"

    # 4. Unlinked token -> fail closed
    ctx_unlinked = resolve_identity("unlinked_token", db_client, project_id=PROJECT_ID, clock=lambda: now)
    assert ctx_unlinked.verified is False
    assert ctx_unlinked.customer_id is None
    assert ctx_unlinked.reason == "unlinked"

    # 5. TriageRouter turn with anonymous caller:
    # Preserves add-only escalation invariant (REQ-AGENT-003); creates ticket with anon:<session_id>
    policy = TriagePolicy(
        thresholds=RouterThresholds(tau_esc=0.35),
        s1_client=RouterS1Client(),
        tier2_invoker=lambda text: RouterPrediction(
            escalate=True,
            escalate_prob=0.95,
            sentiment="angry",
            urgency="immediate",
            intent="abuse",
        ),
    )
    router = TriageRouterAgent(policy=policy, mcp_client=db_client)

    result = router.triage(
        session_id="session_anon_99",
        message_text="You idiot scammer, give me my money back right now!",
        customer_id_token=None,
    )

    # Invariants verification
    assert result.decision.decision == "escalate"
    assert result.decision.queue == "human-standard"
    assert result.decision.customer_verified is False
    assert result.decision.identity_reason == "missing_token"
    assert result.calming_ack is not None

    # Ticket upserted with anon:<session_id>
    assert len(db_client.tickets) == 1
    ticket = list(db_client.tickets.values())[0]
    assert ticket["customer_id"] == "anon:session_anon_99"
    assert ticket["decision"]["customer_verified"] is False
    assert ticket["decision"]["identity_reason"] == "missing_token"


# -----------------------------------------------------------------------------
# REQ-IDENT-007: Identity Threat Mitigations
# -----------------------------------------------------------------------------

def test_threat_mitigations(monkeypatch):
    """Verify mitigation of caller spoofing, replay, IDOR, suspension, and cache expiry."""
    # REQ-IDENT-007
    base_time = 1700000000.0
    current_time = [base_time]

    def mock_clock() -> float:
        return current_time[0]

    claims = {
        "token_valid": {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "user_uid_valid",
            "exp": base_time + 600.0,
        },
        "token_expired": {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "user_uid_exp",
            "exp": base_time - 10.0,
        },
        "token_wrong_aud": {
            "iss": ISSUER,
            "aud": "attacker-project-id",
            "sub": "user_uid_replay",
            "exp": base_time + 600.0,
        },
        "token_wrong_iss": {
            "iss": "https://securetoken.google.com/malicious-project",
            "aud": PROJECT_ID,
            "sub": "user_uid_malicious",
            "exp": base_time + 600.0,
        },
        "token_missing_sub": {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "",
            "exp": base_time + 600.0,
        },
        "token_suspended": {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "user_uid_suspended",
            "exp": base_time + 600.0,
        },
        "token_unlinked": {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "user_uid_not_in_db",
            "exp": base_time + 600.0,
        },
    }

    mock_verifier = _make_mock_firebase_verifier(claims)
    monkeypatch.setattr("google.oauth2.id_token.verify_firebase_token", mock_verifier)

    db_client = InMemoryTicketDbMcpClient()
    db_client.register_identity(ISSUER, "user_uid_valid", "cust_1001", status="active", linked=True)
    db_client.register_identity(ISSUER, "user_uid_suspended", "cust_1002", status="suspended", linked=True)

    # 1. Valid token -> ok
    ctx = resolve_identity("token_valid", db_client, project_id=PROJECT_ID, clock=mock_clock)
    assert ctx.reason == "ok"
    assert ctx.verified is True
    assert ctx.customer_id == "cust_1001"

    # 2. Expired token -> expired (fails closed)
    ctx = resolve_identity("token_expired", db_client, project_id=PROJECT_ID, clock=mock_clock)
    assert ctx.reason == "expired"
    assert ctx.verified is False
    assert ctx.customer_id is None

    # 3. Wrong aud -> invalid_token (cross-project replay defense)
    ctx = resolve_identity("token_wrong_aud", db_client, project_id=PROJECT_ID, clock=mock_clock)
    assert ctx.reason == "invalid_token"
    assert ctx.verified is False

    # 4. Wrong iss -> invalid_token (spoofed issuer defense)
    ctx = resolve_identity("token_wrong_iss", db_client, project_id=PROJECT_ID, clock=mock_clock)
    assert ctx.reason == "invalid_token"
    assert ctx.verified is False

    # 5. Missing sub -> invalid_token
    ctx = resolve_identity("token_missing_sub", db_client, project_id=PROJECT_ID, clock=mock_clock)
    assert ctx.reason == "invalid_token"
    assert ctx.verified is False

    # 6. Suspended customer -> suspended (fails closed)
    ctx = resolve_identity("token_suspended", db_client, project_id=PROJECT_ID, clock=mock_clock)
    assert ctx.reason == "suspended"
    assert ctx.verified is False
    assert ctx.customer_id is None

    # 7. Unlinked customer -> unlinked (fails closed)
    ctx = resolve_identity("token_unlinked", db_client, project_id=PROJECT_ID, clock=mock_clock)
    assert ctx.reason == "unlinked"
    assert ctx.verified is False
    assert ctx.customer_id is None

    # 8. Threat mitigation: Caller-asserted session.user_id spoof ignored
    class MockSession:
        def __init__(self, session_id: str, user_id: str, state: Dict[str, Any]):
            self.id = session_id
            self.user_id = user_id
            self.state = state

    class MockContext:
        def __init__(self, session_id: str, user_id: str, state: Dict[str, Any]):
            self.session = MockSession(session_id, user_id, state)
            self.user_content = "Please refund my payment"

    # Attacker asserts session.user_id='cust_victim' without a token
    ctx_spoof = MockContext("s-spoof-1", user_id="cust_victim", state={})
    extracted_token = extract_customer_id_token(ctx_spoof)
    assert extracted_token is None  # Never reads from user_id

    router = TriageRouterAgent(mcp_client=db_client)
    agent = TriageRouterAdkAgent(router=router)
    agent.query(
        message="Please refund my payment",
        session_id="s-spoof-1",
        customer_id="cust_victim",  # Caller asserts victim customer_id
    )
    # Ticket customer_id must NOT be the victim!
    upserted_ticket = db_client.tickets[list(db_client.tickets.keys())[-1]]
    assert upserted_ticket["customer_id"] == "anon:s-spoof-1"
    assert upserted_ticket["customer_id"] != "cust_victim"

    # 9. Threat mitigation: customer_id injected into session state is ignored
    ctx_state_injection = MockContext(
        "s-spoof-2",
        user_id="cust_anon",
        state={"customer_id": "cust_admin", "customer_verified": True},
    )
    assert extract_customer_id_token(ctx_state_injection) is None

    router.triage(
        session_id="s-spoof-2",
        message_text="Help me",
        customer_id_token=None,
    )
    upserted_ticket2 = db_client.tickets[list(db_client.tickets.keys())[-1]]
    assert upserted_ticket2["customer_id"] == "anon:s-spoof-2"
    assert upserted_ticket2["customer_id"] != "cust_admin"

    # 10. Cache hit within TTL
    db_spy = MagicMock(wraps=db_client)
    # First resolution: populates cache
    ctx_c1 = resolve_identity("token_valid", db_spy, project_id=PROJECT_ID, clock=mock_clock)
    assert ctx_c1.verified is True
    # Second resolution within TTL: served from cache, db_spy NOT called again
    db_spy.resolve_customer.reset_mock()
    ctx_c2 = resolve_identity("token_valid", db_spy, project_id=PROJECT_ID, clock=mock_clock)
    assert ctx_c2.verified is True
    assert db_spy.resolve_customer.call_count == 0

    # 11. Cache miss after exp / TTL
    # Advance clock past exp (base_time + 600)
    current_time[0] = base_time + 650.0
    ctx_after_exp = resolve_identity("token_valid", db_spy, project_id=PROJECT_ID, clock=mock_clock)
    # Token is now expired, fails closed
    assert ctx_after_exp.reason == "expired"
    assert ctx_after_exp.verified is False
