"""Tests for Customer Context Token (CCT) minting at TriageRouterAgent.

# REQ-AUTH-002: Signed Customer Context Token (minting at TriageRouter)
# REQ-AUTH-003: CCT Transport and Verification at order-lookup-mcp (RLS from verified claims)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from ticket_router.agents.triage_router.a2a_client import (
    A2AHandoffPayload,
    HttpA2AClient,
    InMemoryA2AClient,
)
from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.agents.triage_router.policy import (
    RouterThresholds,
    TriagePolicy,
)
from ticket_router.common.observability import StructuredJsonLogger, is_pii_clean
from ticket_router.common.settings import Settings
from ticket_router.identity import ResolvedCustomerContext
from ticket_router.identity.customer_token import (
    LocalEcSigner,
    StaticPublicKeyResolver,
    _b64url_decode,
    verify_customer_context_token,
)
from ticket_router.models.router_s1_client import RouterPrediction, RouterS1Client

KEY_PREFIX = (
    "projects/susmith-fde-ticket-router/locations/us-central1/"
    "keyRings/ticket-router-identity/cryptoKeys/customer-context-signer/"
)
KID = f"{KEY_PREFIX}cryptoKeyVersions/1"


class _CountingSigner:
    """Wrapper around LocalEcSigner tracking invocation count."""

    def __init__(self, inner: LocalEcSigner) -> None:
        self._inner = inner
        self.kid = inner.kid
        self.calls = 0

    def sign(self, message: bytes) -> bytes:
        self.calls += 1
        return self._inner.sign(message)


def _auto_rag_policy(intent: str = "order_status") -> TriagePolicy:
    mock_s1 = MagicMock(spec=RouterS1Client)
    mock_s1.route.return_value = RouterPrediction(
        escalate=False,
        escalate_prob=0.03,
        sentiment="neutral",
        sentiment_prob=0.95,
        urgency="normal",
        urgency_prob=0.95,
        intent=intent,
        intent_prob=0.95,
    )
    return TriagePolicy(thresholds=RouterThresholds(), s1_client=mock_s1)


# REQ-AUTH-002, REQ-AUTH-003
def test_triage_mints_cct_only_for_verified_customers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mint CCT only when customer_verified=True and customer_id is non-empty and non-anon."""
    priv_key = ec.generate_private_key(ec.SECP256R1())
    pub_key = priv_key.public_key()
    signer = _CountingSigner(LocalEcSigner(private_key=priv_key, kid=KID))
    resolver = StaticPublicKeyResolver({KID: pub_key})

    settings = Settings.from_env(
        {
            "CCT_KEY_VERSION": KID,
            "CCT_ISSUER": "triage-router",
            "CCT_AUDIENCE": "order-lookup-mcp",
            "CCT_TTL_S": "300",
        }
    )

    current_identity: Dict[str, Any] = {
        "ctx": ResolvedCustomerContext(
            customer_id="cust_1001",
            verified=True,
            issuer="https://accounts.google.com",
            reason="ok",
        )
    }

    monkeypatch.setattr(
        "ticket_router.agents.triage_router.agent.resolve_identity",
        lambda **_kwargs: current_identity["ctx"],
    )

    a2a_client = InMemoryA2AClient()
    router = TriageRouterAgent(
        policy=_auto_rag_policy(),
        mcp_client=InMemoryTicketDbMcpClient(),
        a2a_client=a2a_client,
        cct_signer=signer,
        settings=settings,
    )

    # 1. Verified customer -> mints valid CCT with sub=customer_id, aud, tid, act
    res = router.triage(
        session_id="sess_ver_1",
        message_text="What is the status of my order ORD-1001?",
        ticket_id="TICK-MINT-101",
        oauth_access_token="valid-oauth-token",
    )
    assert res.decision.decision == "auto_rag"
    assert signer.calls == 1
    assert len(a2a_client.handoffs) == 1

    payload: A2AHandoffPayload = a2a_client.handoffs[0]
    assert payload.customer_context_token is not None
    token = payload.customer_context_token

    verified = verify_customer_context_token(
        token,
        resolver=resolver,
        audience="order-lookup-mcp",
        allowed_issuers=["triage-router"],
        key_prefix=KEY_PREFIX,
    )
    assert verified.customer_id == "cust_1001"
    assert verified.issuer == "triage-router"
    assert verified.ticket_id == "TICK-MINT-101"

    raw_claims = json.loads(_b64url_decode(token.split(".")[1]).decode("utf-8"))
    assert raw_claims["sub"] == "cust_1001"
    assert raw_claims["aud"] == "order-lookup-mcp"
    assert raw_claims["tid"] == "TICK-MINT-101"
    assert raw_claims["act"] == {"sub": "triage-router"}
    assert raw_claims["exp"] - raw_claims["iat"] == 300

    # Verify HttpA2AClient serializes customer_context_token in JSON payload
    mock_http_sess = MagicMock()
    mock_http_sess.post.return_value.status_code = 200
    mock_http_sess.post.return_value.json.return_value = {"status": "AUTO_RESOLVED", "route": "resolved"}
    http_a2a = HttpA2AClient(
        endpoint_url="https://faq-resolver.internal",
        token_provider=lambda _aud: "mock-oidc",
        session=mock_http_sess,
    )
    http_a2a.handoff(payload)
    sent_json = json.loads(mock_http_sess.post.call_args.kwargs["data"].decode("utf-8"))
    assert sent_json["customer_context_token"] == token

    # 2. Unverified customer (verified=False, customer_id=None) -> NO token
    a2a_client.handoffs.clear()
    current_identity["ctx"] = ResolvedCustomerContext(
        customer_id=None,
        verified=False,
        issuer=None,
        reason="missing_token",
    )
    router.triage(
        session_id="sess_unver_1",
        message_text="What is your return policy?",
        ticket_id="TICK-UNVER-1",
    )
    assert signer.calls == 1  # Unchanged
    assert len(a2a_client.handoffs) == 1
    assert a2a_client.handoffs[0].customer_context_token is None

    # 3. Anonymous customer_id ("anon:*") even if verified=True -> NO token
    a2a_client.handoffs.clear()
    current_identity["ctx"] = ResolvedCustomerContext(
        customer_id="anon:sess_anon_2",
        verified=True,
        issuer="https://accounts.google.com",
        reason="ok",
    )
    router.triage(
        session_id="sess_anon_2",
        message_text="What is your return policy?",
        ticket_id="TICK-ANON-2",
    )
    assert signer.calls == 1
    assert len(a2a_client.handoffs) == 1
    assert a2a_client.handoffs[0].customer_context_token is None

    # 4. Empty customer_id ("") even if verified=True -> NO token
    a2a_client.handoffs.clear()
    current_identity["ctx"] = ResolvedCustomerContext(
        customer_id="",
        verified=True,
        issuer="https://accounts.google.com",
        reason="ok",
    )
    router.triage(
        session_id="sess_empty_3",
        message_text="What is your return policy?",
        ticket_id="TICK-EMPTY-3",
    )
    assert signer.calls == 1
    assert len(a2a_client.handoffs) == 1
    assert a2a_client.handoffs[0].customer_context_token is None

    # 5. Escalated turn even for verified customer -> NO A2A handoff and NO CCT minted
    a2a_client.handoffs.clear()
    current_identity["ctx"] = ResolvedCustomerContext(
        customer_id="cust_1001",
        verified=True,
        issuer="https://accounts.google.com",
        reason="ok",
    )
    res_esc = router.triage(
        session_id="sess_esc_4",
        message_text="I want to speak to a human representative immediately!",
        ticket_id="TICK-ESC-4",
    )
    assert res_esc.decision.decision == "escalate"
    assert signer.calls == 1
    assert len(a2a_client.handoffs) == 0


# REQ-AUTH-002
def test_minting_disabled_when_cct_key_version_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When settings.cct_key_version is empty, minting is disabled even for verified customers."""
    priv_key = ec.generate_private_key(ec.SECP256R1())
    signer = _CountingSigner(LocalEcSigner(private_key=priv_key, kid=KID))

    monkeypatch.delenv("CCT_KEY_VERSION", raising=False)
    monkeypatch.setattr(
        "ticket_router.agents.triage_router.agent.resolve_identity",
        lambda **_kwargs: ResolvedCustomerContext(
            customer_id="cust_1001",
            verified=True,
            issuer="https://accounts.google.com",
            reason="ok",
        ),
    )

    a2a_client = InMemoryA2AClient()
    router = TriageRouterAgent(
        policy=_auto_rag_policy(),
        mcp_client=InMemoryTicketDbMcpClient(),
        a2a_client=a2a_client,
        cct_signer=signer,
        settings=Settings.from_env({"CCT_KEY_VERSION": ""}),
    )

    res = router.triage(
        session_id="sess_disabled",
        message_text="Where is my order ORD-1001?",
        ticket_id="TICK-DISABLED-1",
    )
    assert res.decision.decision == "auto_rag"
    assert signer.calls == 0
    assert len(a2a_client.handoffs) == 1
    assert a2a_client.handoffs[0].customer_context_token is None


# REQ-AUTH-002
def test_default_kms_signer_lazily_constructed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default KmsSigner(settings.cct_key_version) is constructed lazily on first verified handoff."""
    priv_key = ec.generate_private_key(ec.SECP256R1())
    pub_key = priv_key.public_key()
    resolver = StaticPublicKeyResolver({KID: pub_key})

    constructed_kids: list[str] = []

    class _FakeKmsSigner:
        def __init__(self, key_version_name: str, session: Optional[Any] = None) -> None:
            constructed_kids.append(key_version_name)
            self.kid = key_version_name
            self._local = LocalEcSigner(private_key=priv_key, kid=key_version_name)

        def sign(self, message: bytes) -> bytes:
            return self._local.sign(message)

    monkeypatch.setenv("CCT_KEY_VERSION", KID)
    monkeypatch.setattr("ticket_router.agents.triage_router.agent.KmsSigner", _FakeKmsSigner)
    monkeypatch.setattr(
        "ticket_router.agents.triage_router.agent.resolve_identity",
        lambda **_kwargs: ResolvedCustomerContext(
            customer_id="cust_1002",
            verified=True,
            issuer="https://accounts.google.com",
            reason="ok",
        ),
    )

    a2a_client = InMemoryA2AClient()
    router = TriageRouterAgent(
        policy=_auto_rag_policy(),
        mcp_client=InMemoryTicketDbMcpClient(),
        a2a_client=a2a_client,
        lazy_init=True,
    )
    # Not constructed at instantiation time
    assert constructed_kids == []

    router.triage(
        session_id="sess_lazy_1",
        message_text="Where is my order ORD-1002?",
        ticket_id="TICK-LAZY-1",
    )
    router.triage(
        session_id="sess_lazy_2",
        message_text="Where is my order ORD-1002?",
        ticket_id="TICK-LAZY-2",
    )

    # Constructed once and reused across turns
    assert constructed_kids == [KID]
    assert len(a2a_client.handoffs) == 2
    tok = a2a_client.handoffs[0].customer_context_token
    assert tok is not None
    verified = verify_customer_context_token(
        tok,
        resolver=resolver,
        audience="order-lookup-mcp",
        allowed_issuers=["triage-router"],
        key_prefix=KEY_PREFIX,
    )
    assert verified.customer_id == "cust_1002"
    assert verified.ticket_id == "TICK-LAZY-1"


# REQ-AUTH-002
def test_signer_exception_fails_safe_and_logs_without_pii_or_token(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Signer exceptions log ticket_id + error_type only, send no token, and continue the turn."""

    class _FailingSigner:
        kid = KID

        def sign(self, message: bytes) -> bytes:
            raise RuntimeError("KMS asymmetricSign failed for cust_secret_999")

    monkeypatch.setattr(
        "ticket_router.agents.triage_router.agent.resolve_identity",
        lambda **_kwargs: ResolvedCustomerContext(
            customer_id="cust_secret_999",
            verified=True,
            issuer="https://accounts.google.com",
            reason="ok",
        ),
    )

    a2a_client = InMemoryA2AClient(
        response_generator=lambda _p: {
            "status": "AUTO_RESOLVED",
            "route": "resolved",
            "answer": "Please sign in or contact support.",
        }
    )
    router = TriageRouterAgent(
        policy=_auto_rag_policy(),
        mcp_client=InMemoryTicketDbMcpClient(),
        a2a_client=a2a_client,
        cct_signer=_FailingSigner(),
        settings=Settings.from_env({"CCT_KEY_VERSION": KID}),
    )

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        res = router.triage(
            session_id="sess_fail_1",
            message_text="What is the status of order ORD-9000?",
            ticket_id="TICK-FAILSAFE-1",
        )

    # Turn proceeds cleanly without crashing or failing A2A handoff
    assert res.decision.decision == "auto_rag"
    assert res.handoff_response is not None
    assert res.handoff_response["status"] == "AUTO_RESOLVED"
    assert len(a2a_client.handoffs) == 1
    assert a2a_client.handoffs[0].customer_context_token is None

    # Warning logged with ticket_id and exception type ONLY (no customer_id, no raw error message)
    warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warn_records) == 1
    warn_text = warn_records[0].getMessage()
    parsed = json.loads(warn_text)
    assert parsed["ticket_id"] == "TICK-FAILSAFE-1"
    assert parsed["error_type"] == "RuntimeError"
    assert is_pii_clean(parsed)
    assert "cust_secret_999" not in caplog.text
    assert "asymmetricSign failed" not in caplog.text


# REQ-AUTH-002
def test_minted_token_never_appears_in_captured_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Minted CCT and customer_id never appear in Python logs or StructuredJsonLogger."""
    priv_key = ec.generate_private_key(ec.SECP256R1())
    signer = LocalEcSigner(private_key=priv_key, kid=KID)

    monkeypatch.setattr(
        "ticket_router.agents.triage_router.agent.resolve_identity",
        lambda **_kwargs: ResolvedCustomerContext(
            customer_id="cust_private_777",
            verified=True,
            issuer="https://accounts.google.com",
            reason="ok",
        ),
    )

    structured_logger = StructuredJsonLogger(name="triage_cct_test")
    a2a_client = InMemoryA2AClient()
    router = TriageRouterAgent(
        policy=_auto_rag_policy(),
        mcp_client=InMemoryTicketDbMcpClient(),
        a2a_client=a2a_client,
        logger=structured_logger,
        cct_signer=signer,
        settings=Settings.from_env({"CCT_KEY_VERSION": KID}),
    )

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        router.triage(
            session_id="sess_nolog_1",
            message_text="Where is my order ORD-7777?",
            ticket_id="TICK-NOLOG-1",
        )

    assert len(a2a_client.handoffs) == 1
    minted_token = a2a_client.handoffs[0].customer_context_token
    assert minted_token is not None
    header_seg, claims_seg, sig_seg = minted_token.split(".")

    # Token (full or segments) and customer_id must never appear in captured logs
    assert len(caplog.records) >= 1
    assert minted_token not in caplog.text
    assert claims_seg not in caplog.text
    assert sig_seg not in caplog.text
    assert "cust_private_777" not in caplog.text
    for rec in caplog.records:
        msg = rec.getMessage()
        assert minted_token not in msg
        assert claims_seg not in msg
        assert sig_seg not in msg
        assert "cust_private_777" not in msg
    _ = header_seg
