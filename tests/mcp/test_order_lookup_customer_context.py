"""Tests for Customer Context Token (CCT) verification matrix at order-lookup-mcp.

# REQ-AUTH-002: Signed Customer Context Token (minting at TriageRouter)
# REQ-AUTH-003: CCT Transport and Verification at order-lookup-mcp (RLS from verified claims)
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from ticket_router.identity.customer_token import (
    LocalEcSigner,
    StaticPublicKeyResolver,
    mint_customer_context_token,
)
from ticket_router.mcp.order_lookup.server import create_app
from ticket_router.store.fake import FakeOrderRepo
from ticket_router.store.models import Order

KEY_PREFIX = "projects/susmith-fde-ticket-router/locations/us-central1/keyRings/ticket-router-identity/cryptoKeys/customer-context-signer/"
KID = f"{KEY_PREFIX}cryptoKeyVersions/1"


@pytest.fixture
def auth_fixture():
    priv_key = ec.generate_private_key(ec.SECP256R1())
    pub_key = priv_key.public_key()
    signer = LocalEcSigner(private_key=priv_key, kid=KID)
    resolver = StaticPublicKeyResolver({KID: pub_key})
    return signer, resolver


def _build_test_app(
    resolver,
    cct_mode: str = "accept",
    require_verified_header: bool = True,
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
    repo.add_order(
        Order(
            order_id="ORD-1002",
            customer_id="cust_1002",
            order_status="DELIVERED",
            items=[{"sku": "ITEM-2", "name": "Pixel Watch 3"}],
            total_cents=34900,
        )
    )
    app = create_app(
        order_repo=repo,
        require_verified_header=require_verified_header,
        cct_mode=cct_mode,
        cct_resolver=resolver,
        cct_audience="order-lookup-mcp",
        cct_allowed_issuers=["triage-router"],
        cct_key_prefix=KEY_PREFIX,
    )
    return app, repo


# REQ-AUTH-003
def test_cct_verification_matrix(auth_fixture):
    """Test full verification matrix: accept, require, off, and BOLA body guard."""
    signer, resolver = auth_fixture

    valid_token_cust1 = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id="TICK-001",
    )
    valid_token_cust2 = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1002",
        ticket_id="TICK-002",
    )

    # 1. accept + valid token -> 200 and repo called with sub
    app_accept, _ = _build_test_app(resolver, cct_mode="accept")
    client_accept = TestClient(app_accept)

    resp = client_accept.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={"X-Customer-Context": valid_token_cust1},
    )
    assert resp.status_code == 200
    assert resp.json()["order_id"] == "ORD-1001"
    assert resp.json()["status"] == "PROCESSING"

    # Also verify valid token for cust2 accesses cust2's order ORD-1002
    resp_cust2 = client_accept.post(
        "/tools/get_order",
        json={"order_id": "ORD-1002"},
        headers={"X-Customer-Context": valid_token_cust2},
    )
    assert resp_cust2.status_code == 200
    assert resp_cust2.json()["order_id"] == "ORD-1002"
    assert resp_cust2.json()["status"] == "DELIVERED"

    # Also list_orders tool with valid token
    resp_list = client_accept.post(
        "/tools/list_orders",
        json={"limit": 5},
        headers={"X-Customer-Context": valid_token_cust1},
    )
    assert resp_list.status_code == 200
    orders = resp_list.json()["orders"]
    assert len(orders) == 1
    assert orders[0]["order_id"] == "ORD-1001"

    # 2. accept + invalid token -> 403 invalid_customer_context
    resp_invalid = client_accept.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={"X-Customer-Context": "malformed.invalid.token"},
    )
    assert resp_invalid.status_code == 403
    assert resp_invalid.json()["detail"] == "invalid_customer_context"

    # 3. accept + mismatch header -> 403 customer_context_mismatch
    resp_mismatch = client_accept.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={
            "X-Customer-Context": valid_token_cust1,
            "X-Customer-ID": "cust_1002",
            "X-Customer-Verified": "true",
        },
    )
    assert resp_mismatch.status_code == 403
    assert resp_mismatch.json()["detail"] == "customer_context_mismatch"

    # 4. accept + no token -> legacy path still works
    resp_legacy = client_accept.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={
            "X-Customer-ID": "cust_1001",
            "X-Customer-Verified": "true",
        },
    )
    assert resp_legacy.status_code == 200
    assert resp_legacy.json()["order_id"] == "ORD-1001"

    # 5. require + no token -> 403 invalid_customer_context
    app_require, _ = _build_test_app(resolver, cct_mode="require")
    client_require = TestClient(app_require)

    resp_require_no_token = client_require.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={
            "X-Customer-ID": "cust_1001",
            "X-Customer-Verified": "true",
        },
    )
    assert resp_require_no_token.status_code == 403
    assert resp_require_no_token.json()["detail"] == "invalid_customer_context"

    # 6. require + valid token -> 200
    resp_require_valid = client_require.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={"X-Customer-Context": valid_token_cust1},
    )
    assert resp_require_valid.status_code == 200
    assert resp_require_valid.json()["order_id"] == "ORD-1001"

    # 7. off -> token ignored (legacy headers apply)
    app_off, _ = _build_test_app(resolver, cct_mode="off")
    client_off = TestClient(app_off)

    # Even with an invalid token, legacy headers allow request through
    resp_off = client_off.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={
            "X-Customer-Context": "malformed.garbage.token",
            "X-Customer-ID": "cust_1001",
            "X-Customer-Verified": "true",
        },
    )
    assert resp_off.status_code == 200
    assert resp_off.json()["order_id"] == "ORD-1001"

    # In off mode without legacy headers, it fails per legacy logic (400)
    resp_off_missing_headers = client_off.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={"X-Customer-Context": valid_token_cust1},
    )
    assert resp_off_missing_headers.status_code == 400

    # 8. body customer_id -> 400 BOLA violation
    resp_bola = client_accept.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001", "customer_id": "cust_1001"},
        headers={"X-Customer-Context": valid_token_cust1},
    )
    assert resp_bola.status_code == 400
    assert "BOLA/IDOR violation" in resp_bola.json()["detail"]

    # Body customer_id also rejected in list_orders
    resp_bola_list = client_accept.post(
        "/tools/list_orders",
        json={"limit": 5, "customer_id": "cust_1001"},
        headers={"X-Customer-Context": valid_token_cust1},
    )
    assert resp_bola_list.status_code == 400
    assert "BOLA/IDOR violation" in resp_bola_list.json()["detail"]


# REQ-AUTH-003
def test_unknown_cct_mode_validation(auth_fixture):
    """Verify that unknown cct_mode raises ValueError in create_app and 403 fail-closed at runtime."""
    _, resolver = auth_fixture
    repo = FakeOrderRepo()

    # 1. create_app with invalid cct_mode raises ValueError
    with pytest.raises(ValueError, match="Invalid cct_mode"):
        create_app(
            order_repo=repo,
            cct_mode="bogus_mode",
            cct_resolver=resolver,
        )

    # 2. Runtime unknown mode fails closed with 403 invalid_customer_context
    app, _ = _build_test_app(resolver, cct_mode="accept")
    app.state.cct_mode = "corrupted_mode"
    client = TestClient(app)

    resp = client.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={"X-Customer-ID": "cust_1001", "X-Customer-Verified": "true"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "invalid_customer_context"


# REQ-AUTH-003
def test_cct_verification_via_threadpool(auth_fixture):
    """Verify that synchronous CCT verification runs via threadpool without blocking event loop."""
    import threading
    signer, resolver = auth_fixture
    valid_token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id="TICK-THREADPOOL",
    )

    thread_ids: set[int] = set()

    class ThreadTrackingResolver:
        def __init__(self, inner):
            self.inner = inner

        def get(self, kid: str):
            thread_ids.add(threading.get_ident())
            return self.inner.get(kid)

    tracking_resolver = ThreadTrackingResolver(resolver)
    app, _ = _build_test_app(tracking_resolver, cct_mode="accept")
    client = TestClient(app)

    main_thread = threading.get_ident()
    resp = client.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={"X-Customer-Context": valid_token},
    )
    assert resp.status_code == 200
    assert len(thread_ids) >= 1
    # Verify execution happened in threadpool worker thread
    assert all(t != main_thread for t in thread_ids)

