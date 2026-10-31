"""Tests for order-lookup-mcp caller authorization of FaqResolver SPIFFE principal.

# REQ-AUTH-009: order-lookup-mcp caller authorization of FaqResolver's SPIFFE principal + customer delegation
Verifies:
1. When ALLOWED_CALLER_SPIFFE_IDS is set, caller must present a forwarded ID token whose `sub` claim
   matches an allowed SPIFFE principal.
2. If caller token is missing, malformed, or has an unauthorized `sub`, request is rejected with
   403 {"error": "caller_not_authorized"} BEFORE customer context (CCT) is evaluated.
3. When caller presents matching SPIFFE principal, request proceeds to CCT verification.
4. When ALLOWED_CALLER_SPIFFE_IDS is empty/unset, staging mode warns once and permits caller through.
5. Invariant: zero token logging (bearer tokens, CCTs, or signatures are never written to logs).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from ticket_router.identity.customer_token import (
    LocalEcSigner,
    StaticPublicKeyResolver,
    mint_customer_context_token,
)
from ticket_router.mcp.order_lookup.server import create_app
from ticket_router.store.fake import FakeOrderRepo
from ticket_router.store.models import Order

FAQ_SPIFFE_ID = (
    "spiffe://agents.global.org-349636765856.system.id.goog/resources/container/"
    "projects/620485621180/locations/us-central1/clusters/support-agents/ns/faq-resolver/sa/ksa-faq-agent"
)
UNAUTHORIZED_SPIFFE_ID = (
    "spiffe://agents.global.org-349636765856.system.id.goog/resources/container/"
    "projects/620485621180/locations/us-central1/clusters/support-agents/ns/other/sa/ksa-other"
)
KEY_PREFIX = "projects/test/locations/us-central1/keyRings/ring/cryptoKeys/cct-signer/"
KID = f"{KEY_PREFIX}cryptoKeyVersions/1"


def make_mock_forwarded_token(sub: str) -> str:
    """Construct mock Google-forwarded token with stripped signature."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub, "iss": "https://accounts.google.com"}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.SIGNATURE_REMOVED_BY_GOOGLE"


@pytest.fixture
def cct_fixture() -> tuple[LocalEcSigner, StaticPublicKeyResolver]:
    priv_key = ec.generate_private_key(ec.SECP256R1())
    pub_key = priv_key.public_key()
    signer = LocalEcSigner(private_key=priv_key, kid=KID)
    resolver = StaticPublicKeyResolver({KID: pub_key})
    return signer, resolver


def _create_app_with_repo(
    cct_resolver: Any = None,
    allowed_spiffe: list[str] | None = None,
    cct_mode: str = "require",
):
    repo = FakeOrderRepo()
    repo.add_order(
        Order(
            order_id="ORD-1001",
            customer_id="cust_1001",
            order_status="PROCESSING",
            items=[{"sku": "ITEM-1", "name": "Pixel 9 Pro"}],
            total_cents=99900,
        )
    )
    return create_app(
        order_repo=repo,
        cct_mode=cct_mode,
        cct_resolver=cct_resolver,
        cct_audience="order-lookup-mcp",
        cct_allowed_issuers=["triage-router"],
        cct_key_prefix=KEY_PREFIX,
        allowed_caller_spiffe_ids=allowed_spiffe,
    )


# REQ-AUTH-009
def test_requires_faq_spiffe_sub_before_cct(cct_fixture: tuple[LocalEcSigner, StaticPublicKeyResolver]) -> None:
    """Verify order-lookup-mcp enforces caller SPIFFE sub check BEFORE customer context (CCT)."""
    signer, resolver = cct_fixture
    valid_cct = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )

    app = _create_app_with_repo(
        cct_resolver=resolver,
        allowed_spiffe=[FAQ_SPIFFE_ID],
        cct_mode="require",
    )
    client = TestClient(app)

    # 1. Missing Authorization header: 403 caller_not_authorized EVEN WITH a valid CCT
    resp_no_auth = client.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={"X-Customer-Context": valid_cct},
    )
    assert resp_no_auth.status_code == 403
    assert resp_no_auth.json() == {"error": "caller_not_authorized"}

    # 2. Unauthorized caller SPIFFE ID: 403 caller_not_authorized EVEN WITH a valid CCT
    bad_token = make_mock_forwarded_token(UNAUTHORIZED_SPIFFE_ID)
    resp_bad_sub = client.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={
            "Authorization": f"Bearer {bad_token}",
            "X-Customer-Context": valid_cct,
        },
    )
    assert resp_bad_sub.status_code == 403
    assert resp_bad_sub.json() == {"error": "caller_not_authorized"}

    # 3. Malformed bearer token: 403 caller_not_authorized
    resp_malformed = client.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={
            "Authorization": "Bearer not-a-valid-jwt",
            "X-Customer-Context": valid_cct,
        },
    )
    assert resp_malformed.status_code == 403
    assert resp_malformed.json() == {"error": "caller_not_authorized"}

    # 4. Valid caller SPIFFE ID: Passes caller authorization, proceeds to CCT check
    good_token = make_mock_forwarded_token(FAQ_SPIFFE_ID)
    resp_valid = client.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={
            "Authorization": f"Bearer {good_token}",
            "X-Customer-Context": valid_cct,
        },
    )
    assert resp_valid.status_code == 200
    assert resp_valid.json()["order_id"] == "ORD-1001"
    assert resp_valid.json()["status"] == "PROCESSING"


# REQ-AUTH-009
def test_unconfigured_spiffe_ids_permits_staging(cct_fixture: tuple[LocalEcSigner, StaticPublicKeyResolver], caplog) -> None:
    """Verify empty/unconfigured ALLOWED_CALLER_SPIFFE_IDS logs warning and allows requests (staging mode)."""
    import ticket_router.mcp.order_lookup.server as order_lookup_server

    # The staging warning is emitted once per process; reset it so test order doesn't matter.
    order_lookup_server._ALLOWED_CALLERS_WARNING_LOGGED = False
    signer, resolver = cct_fixture
    valid_cct = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )

    app = _create_app_with_repo(
        cct_resolver=resolver,
        allowed_spiffe=[],
        cct_mode="require",
    )
    client = TestClient(app)

    with caplog.at_level(logging.WARNING):
        resp = client.post(
            "/tools/get_order",
            json={"order_id": "ORD-1001"},
            headers={"X-Customer-Context": valid_cct},
        )
    assert resp.status_code == 200
    assert resp.json()["order_id"] == "ORD-1001"
    assert any("ALLOWED_CALLER_SPIFFE_IDS is empty" in rec.message for rec in caplog.records)


# REQ-AUTH-009
def test_zero_token_logging(cct_fixture: tuple[LocalEcSigner, StaticPublicKeyResolver], caplog) -> None:
    """Verify neither bearer tokens nor CCTs appear in server logs."""
    signer, resolver = cct_fixture
    secret_cct = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )
    bearer_token = make_mock_forwarded_token(FAQ_SPIFFE_ID)

    app = _create_app_with_repo(
        cct_resolver=resolver,
        allowed_spiffe=[FAQ_SPIFFE_ID],
        cct_mode="require",
    )
    client = TestClient(app)

    with caplog.at_level(logging.DEBUG):
        resp = client.post(
            "/tools/get_order",
            json={"order_id": "ORD-1001"},
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "X-Customer-Context": secret_cct,
            },
        )
    assert resp.status_code == 200

    all_logs = " ".join(rec.message for rec in caplog.records)
    assert bearer_token not in all_logs
    assert secret_cct not in all_logs
    assert "SIGNATURE_REMOVED_BY_GOOGLE" not in all_logs
