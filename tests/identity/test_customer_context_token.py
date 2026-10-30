"""Unit tests for Customer Context Token (CCT) minting, verification, and key resolvers.

# REQ-AUTH-002: Signed Customer Context Token (minting at TriageRouter)
# REQ-AUTH-003: CCT Transport and Verification at order-lookup-mcp (RLS from verified claims)
"""

from __future__ import annotations

import base64
import json
import pickle
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from ticket_router.identity.customer_token import (
    SECP256R1_ORDER,
    CustomerContextError,
    KmsPublicKeyResolver,
    KmsSigner,
    LocalEcSigner,
    StaticPublicKeyResolver,
    VerifiedCustomerContext,
    _b64url_decode,
    _b64url_encode,
    mint_customer_context_token,
    verify_customer_context_token,
)

KID_PREFIX = "projects/susmith-fde-ticket-router/locations/us-central1/keyRings/ticket-router-identity/cryptoKeys/customer-context-signer/"
KID_1 = f"{KID_PREFIX}cryptoKeyVersions/1"


@pytest.fixture
def ec_key_pair():
    """Generate ephemeral EC P-256 key pair for hermetic test execution."""
    priv_key = ec.generate_private_key(ec.SECP256R1())
    pub_key = priv_key.public_key()
    return priv_key, pub_key


# REQ-AUTH-002, REQ-AUTH-003
def test_token_format_and_claims(ec_key_pair):
    """Test token header, claims, ttl, jti, signature, KmsSigner DER->raw, and KmsPublicKeyResolver."""
    priv_key, pub_key = ec_key_pair
    local_signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})

    now = 1730000000
    token = mint_customer_context_token(
        signer=local_signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id="TICK-42",
        ttl_s=300,
        now=now,
        jti="custom-jti-1234",
    )

    parts = token.split(".")
    assert len(parts) == 3

    header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
    assert header["alg"] == "ES256"
    assert header["typ"] == "JWT"
    assert header["kid"] == KID_1

    claims = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    assert claims["iss"] == "triage-router"
    assert claims["sub"] == "cust_1001"
    assert claims["aud"] == "order-lookup-mcp"
    assert claims["iat"] == now
    assert claims["exp"] == now + 300
    assert claims["jti"] == "custom-jti-1234"
    assert claims["act"] == {"sub": "triage-router"}
    assert claims["tid"] == "TICK-42"

    raw_sig = _b64url_decode(parts[2])
    assert len(raw_sig) == 64

    # Verify signature through verify function
    verified = verify_customer_context_token(
        token,
        resolver=resolver,
        audience="order-lookup-mcp",
        allowed_issuers=["triage-router"],
        key_prefix=KID_PREFIX,
        now=now,
    )
    assert isinstance(verified, VerifiedCustomerContext)
    assert verified.customer_id == "cust_1001"
    assert verified.issuer == "triage-router"
    assert verified.jti == "custom-jti-1234"
    assert verified.ticket_id == "TICK-42"
    assert verified.expires_at == now + 300

    # Test KmsSigner DER -> raw conversion with fake session
    class FakeKmsPostSession:
        def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
            self.private_key = private_key
            self.calls: list[dict[str, Any]] = []

        def post(self, url: str, json: dict[str, Any], timeout: float) -> Any:
            self.calls.append({"url": url, "json": json, "timeout": timeout})
            digest_b64 = json["digest"]["sha256"]
            digest = base64.b64decode(digest_b64)
            der_sig = self.private_key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
            sig_b64 = base64.b64encode(der_sig).decode("ascii")

            class FakeResponse:
                status_code = 200

                def raise_for_status(self) -> None:
                    pass

                def json(self) -> dict[str, Any]:
                    return {"signature": sig_b64}

            return FakeResponse()

    fake_session = FakeKmsPostSession(priv_key)
    kms_signer = KmsSigner(key_version_name=KID_1, session=fake_session)
    assert kms_signer.kid == KID_1

    kms_token = mint_customer_context_token(
        signer=kms_signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1002",
        ticket_id=None,
        ttl_s=180,
        now=now,
    )
    assert len(fake_session.calls) == 1
    assert fake_session.calls[0]["timeout"] == 2.0

    kms_verified = verify_customer_context_token(
        kms_token,
        resolver=resolver,
        audience="order-lookup-mcp",
        allowed_issuers=["triage-router"],
        key_prefix=KID_PREFIX,
        now=now,
    )
    assert kms_verified.customer_id == "cust_1002"
    assert kms_verified.ticket_id is None

    # Test KmsPublicKeyResolver with fake session and caching
    pem_bytes = pub_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pem_str = pem_bytes.decode("ascii")

    class FakeKmsGetSession:
        def __init__(self) -> None:
            self.get_count = 0

        def get(self, url: str, timeout: float) -> Any:
            self.get_count += 1

            class FakeResponse:
                status_code = 200

                def json(self) -> dict[str, Any]:
                    return {"pem": pem_str}

            return FakeResponse()

    fake_get_session = FakeKmsGetSession()
    kms_resolver = KmsPublicKeyResolver(session=fake_get_session, cache_ttl_s=3600)

    # First fetch: calls session
    key1 = kms_resolver.get(KID_1)
    assert isinstance(key1, ec.EllipticCurvePublicKey)
    assert fake_get_session.get_count == 1

    # Second fetch: served from cache
    key2 = kms_resolver.get(KID_1)
    assert fake_get_session.get_count == 1
    assert key1 == key2


# REQ-AUTH-002
def test_mint_only_for_verified_customers(ec_key_pair):
    """Test mint raises ValueError for empty string, anon:* prefixes, or invalid TTL."""
    priv_key, _ = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)

    # Empty customer_id rejected
    with pytest.raises(ValueError, match="Invalid customer_id"):
        mint_customer_context_token(
            signer=signer,
            issuer="triage-router",
            audience="order-lookup-mcp",
            customer_id="",
            ticket_id=None,
        )

    # Anonymous prefix rejected
    with pytest.raises(ValueError, match="Invalid customer_id"):
        mint_customer_context_token(
            signer=signer,
            issuer="triage-router",
            audience="order-lookup-mcp",
            customer_id="anon:sess-1234",
            ticket_id=None,
        )

    # Invalid TTL rejected
    with pytest.raises(ValueError, match="Invalid ttl_s"):
        mint_customer_context_token(
            signer=signer,
            issuer="triage-router",
            audience="order-lookup-mcp",
            customer_id="cust_1001",
            ticket_id=None,
            ttl_s=0,
        )

    with pytest.raises(ValueError, match="Invalid ttl_s"):
        mint_customer_context_token(
            signer=signer,
            issuer="triage-router",
            audience="order-lookup-mcp",
            customer_id="cust_1001",
            ticket_id=None,
            ttl_s=601,
        )

    # Legitimate verified customer succeeds
    token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id="TICK-1",
        ttl_s=300,
    )
    assert isinstance(token, str)
    assert len(token.split(".")) == 3


# REQ-AUTH-003
def test_verify_tamper_payload(ec_key_pair):
    """Verify that tampering with the payload fails signature verification."""
    priv_key, pub_key = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})

    token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )
    parts = token.split(".")

    # Tamper payload claims to cust_9999
    tampered_claims = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    tampered_claims["sub"] = "cust_9999"
    tampered_b64 = _b64url_encode(json.dumps(tampered_claims, separators=(",", ":")).encode("utf-8"))
    tampered_token = f"{parts[0]}.{tampered_b64}.{parts[2]}"

    with pytest.raises(CustomerContextError) as exc_info:
        verify_customer_context_token(
            tampered_token,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
        )
    assert exc_info.value.reason == "bad_signature"


# REQ-AUTH-003
def test_verify_tamper_signature(ec_key_pair):
    """Verify that tampering with signature bytes fails verification."""
    priv_key, pub_key = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})

    token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )
    parts = token.split(".")
    raw_sig = bytearray(_b64url_decode(parts[2]))
    raw_sig[0] ^= 0xFF
    tampered_token = f"{parts[0]}.{parts[1]}.{_b64url_encode(bytes(raw_sig))}"

    with pytest.raises(CustomerContextError) as exc_info:
        verify_customer_context_token(
            tampered_token,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
        )
    assert exc_info.value.reason == "bad_signature"


# REQ-AUTH-003
def test_verify_alg_none(ec_key_pair):
    """Verify that algorithm 'none' is rejected with malformed."""
    priv_key, pub_key = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})

    token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )
    parts = token.split(".")
    header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
    header["alg"] = "none"
    tampered_header = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    tampered_token = f"{tampered_header}.{parts[1]}.{parts[2]}"

    with pytest.raises(CustomerContextError) as exc_info:
        verify_customer_context_token(
            tampered_token,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
        )
    assert exc_info.value.reason == "malformed"


# REQ-AUTH-003
def test_verify_wrong_audience(ec_key_pair):
    """Verify that audience mismatch raises bad_audience."""
    priv_key, pub_key = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})

    token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="wrong-audience-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )

    with pytest.raises(CustomerContextError) as exc_info:
        verify_customer_context_token(
            token,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
        )
    assert exc_info.value.reason == "bad_audience"


# REQ-AUTH-003
def test_verify_wrong_issuer(ec_key_pair):
    """Verify that untrusted issuer raises bad_issuer."""
    priv_key, pub_key = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})

    token = mint_customer_context_token(
        signer=signer,
        issuer="rogue-issuer",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )

    with pytest.raises(CustomerContextError) as exc_info:
        verify_customer_context_token(
            token,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
        )
    assert exc_info.value.reason == "bad_issuer"


# REQ-AUTH-003
def test_verify_expired(ec_key_pair):
    """Verify that expired token raises expired."""
    priv_key, pub_key = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})

    now = 1730000000
    token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
        ttl_s=100,
        now=now,
    )

    # Verification at now + 200 (leeway is 60s, exp was now + 100, so 100 <= 200 - 60 = 140)
    with pytest.raises(CustomerContextError) as exc_info:
        verify_customer_context_token(
            token,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
            now=now + 200,
            leeway_s=60,
        )
    assert exc_info.value.reason == "expired"


# REQ-AUTH-003
def test_verify_future_iat(ec_key_pair):
    """Verify that future iat beyond leeway raises not_yet_valid."""
    priv_key, pub_key = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})

    now = 1730000000
    token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
        ttl_s=300,
        now=now + 120,  # iat is in the future
    )

    with pytest.raises(CustomerContextError) as exc_info:
        verify_customer_context_token(
            token,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
            now=now,
            leeway_s=60,
        )
    assert exc_info.value.reason == "not_yet_valid"


# REQ-AUTH-003
def test_verify_ttl_too_long(ec_key_pair):
    """Verify that token with exp - iat > max_ttl_s raises ttl_too_long."""
    priv_key, pub_key = ec_key_pair
    local_signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})

    now = 1730000000
    # Manually craft claims with exp - iat = 700
    header = {"alg": "ES256", "typ": "JWT", "kid": KID_1}
    claims = {
        "iss": "triage-router",
        "sub": "cust_1001",
        "aud": "order-lookup-mcp",
        "iat": now,
        "exp": now + 700,
        "jti": "custom-jti",
        "act": {"sub": "triage-router"},
        "tid": None,
    }
    b64_h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    b64_c = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{b64_h}.{b64_c}".encode("ascii")
    raw_sig = local_signer.sign(signing_input)
    token = f"{b64_h}.{b64_c}.{_b64url_encode(raw_sig)}"

    with pytest.raises(CustomerContextError) as exc_info:
        verify_customer_context_token(
            token,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
            now=now,
            max_ttl_s=600,
        )
    assert exc_info.value.reason == "ttl_too_long"


# REQ-AUTH-003
def test_verify_unknown_kid_prefix(ec_key_pair):
    """Verify that kid with unexpected prefix raises unknown_kid."""
    priv_key, pub_key = ec_key_pair
    bad_kid = "projects/untrusted-project/locations/us-central1/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1"
    signer = LocalEcSigner(private_key=priv_key, kid=bad_kid)
    resolver = StaticPublicKeyResolver({bad_kid: pub_key})

    token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )

    with pytest.raises(CustomerContextError) as exc_info:
        verify_customer_context_token(
            token,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
        )
    assert exc_info.value.reason == "unknown_kid"


# REQ-AUTH-003
def test_verify_anonymous_subject(ec_key_pair):
    """Verify that token containing anonymous sub is rejected with anonymous_subject."""
    priv_key, pub_key = ec_key_pair
    local_signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})

    now = 1730000000
    header = {"alg": "ES256", "typ": "JWT", "kid": KID_1}
    claims = {
        "iss": "triage-router",
        "sub": "anon:rogue-subject",
        "aud": "order-lookup-mcp",
        "iat": now,
        "exp": now + 300,
        "jti": "custom-jti",
        "act": {"sub": "triage-router"},
        "tid": None,
    }
    b64_h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    b64_c = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{b64_h}.{b64_c}".encode("ascii")
    raw_sig = local_signer.sign(signing_input)
    token = f"{b64_h}.{b64_c}.{_b64url_encode(raw_sig)}"

    with pytest.raises(CustomerContextError) as exc_info:
        verify_customer_context_token(
            token,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
            now=now,
        )
    assert exc_info.value.reason == "anonymous_subject"


# REQ-AUTH-003
def test_verify_key_fetch_failed(ec_key_pair):
    """Verify that network or KMS error in key resolver raises key_fetch_failed."""
    priv_key, _ = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)

    class FailingSession:
        def get(self, url: str, timeout: float) -> Any:
            class FakeResponse:
                status_code = 503

                def json(self) -> dict[str, Any]:
                    return {"error": "unavailable"}

            return FakeResponse()

    resolver = KmsPublicKeyResolver(session=FailingSession())

    token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )

    with pytest.raises(CustomerContextError) as exc_info:
        verify_customer_context_token(
            token,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
        )
    assert exc_info.value.reason == "key_fetch_failed"


# REQ-AUTH-003
def test_verify_kid_traversal_and_special_chars(ec_key_pair):
    """Verify that path traversal, query, fragment, and sibling key prefixes in kid are rejected."""
    priv_key, pub_key = ec_key_pair
    resolver = StaticPublicKeyResolver({KID_1: pub_key})

    # 1. Path traversal ..
    bad_traversal_kid = f"{KID_PREFIX}../../projects/evil/locations/us-central1/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1"
    signer_traversal = LocalEcSigner(private_key=priv_key, kid=bad_traversal_kid)
    token_traversal = mint_customer_context_token(
        signer=signer_traversal,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(
            token_traversal,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
        )
    assert exc.value.reason == "unknown_kid"

    # 2. Query param in kid
    bad_query_kid = f"{KID_1}?param=val"
    signer_query = LocalEcSigner(private_key=priv_key, kid=bad_query_kid)
    token_query = mint_customer_context_token(
        signer=signer_query,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(
            token_query,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
        )
    assert exc.value.reason == "unknown_kid"

    # 3. Fragment in kid
    bad_frag_kid = f"{KID_1}#fragment"
    signer_frag = LocalEcSigner(private_key=priv_key, kid=bad_frag_kid)
    token_frag = mint_customer_context_token(
        signer=signer_frag,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(
            token_frag,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
        )
    assert exc.value.reason == "unknown_kid"

    # 4. Sibling key prefix
    sibling_prefix = KID_PREFIX.replace("customer-context-signer/", "sibling-signer/")
    bad_sibling_kid = f"{sibling_prefix}cryptoKeyVersions/1"
    signer_sibling = LocalEcSigner(private_key=priv_key, kid=bad_sibling_kid)
    token_sibling = mint_customer_context_token(
        signer=signer_sibling,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(
            token_sibling,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
        )
    assert exc.value.reason == "unknown_kid"

    # 5. Empty key_prefix rejected
    valid_token = mint_customer_context_token(
        signer=LocalEcSigner(private_key=priv_key, kid=KID_1),
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(
            valid_token,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix="",
        )
    assert exc.value.reason == "unknown_kid"


# REQ-AUTH-003
def test_verify_secp256k1_curve_rejected(ec_key_pair):
    """Verify that non-SECP256R1 curves (e.g. SECP256K1) are rejected in resolver and verify."""
    k1_priv = ec.generate_private_key(ec.SECP256K1())
    k1_pub = k1_priv.public_key()
    k1_pem = k1_pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")

    class FakeK1KmsSession:
        def get(self, url: str, timeout: float) -> Any:
            class FakeResp:
                status_code = 200

                def json(self) -> dict[str, Any]:
                    return {"pem": k1_pem}

            return FakeResp()

    # In KmsPublicKeyResolver.get: curve validation rejects with unknown_kid
    resolver = KmsPublicKeyResolver(session=FakeK1KmsSession())
    with pytest.raises(CustomerContextError) as exc:
        resolver.get(KID_1)
    assert exc.value.reason == "unknown_kid"

    # In verify_customer_context_token: curve validation rejects with unknown_kid
    signer = LocalEcSigner(private_key=k1_priv, kid=KID_1)
    token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )
    static_resolver = StaticPublicKeyResolver({KID_1: k1_pub})
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(
            token,
            resolver=static_resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
        )
    assert exc.value.reason == "unknown_kid"


# REQ-AUTH-003
def test_verify_malformed_numeric_and_json_constants(ec_key_pair):
    """Verify that NaN, Infinity, bool exp/iat, and parse_constant reject with malformed."""
    priv_key, pub_key = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})
    now = 1730000000

    def _craft_token_with_claims(claims_dict: dict[str, Any]) -> str:
        h = {"alg": "ES256", "typ": "JWT", "kid": KID_1}
        b64_h = _b64url_encode(json.dumps(h, separators=(",", ":")).encode("utf-8"))
        b64_c = _b64url_encode(json.dumps(claims_dict, separators=(",", ":")).encode("utf-8"))
        sig = signer.sign(f"{b64_h}.{b64_c}".encode("ascii"))
        return f"{b64_h}.{b64_c}.{_b64url_encode(sig)}"

    # 1. bool exp / iat
    token_bool_exp = _craft_token_with_claims({
        "iss": "triage-router", "sub": "cust_1001", "aud": "order-lookup-mcp",
        "iat": now, "exp": True, "jti": "jti1",
    })
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(token_bool_exp, resolver=resolver, audience="order-lookup-mcp", allowed_issuers=["triage-router"], key_prefix=KID_PREFIX, now=now)
    assert exc.value.reason == "malformed"

    token_bool_iat = _craft_token_with_claims({
        "iss": "triage-router", "sub": "cust_1001", "aud": "order-lookup-mcp",
        "iat": False, "exp": now + 300, "jti": "jti1",
    })
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(token_bool_iat, resolver=resolver, audience="order-lookup-mcp", allowed_issuers=["triage-router"], key_prefix=KID_PREFIX, now=now)
    assert exc.value.reason == "malformed"

    # 2. Raw JSON NaN/Infinity via manual string crafting
    h = {"alg": "ES256", "typ": "JWT", "kid": KID_1}
    b64_h = _b64url_encode(json.dumps(h, separators=(",", ":")).encode("utf-8"))
    raw_claims_nan = f'{{"iss":"triage-router","sub":"cust_1001","aud":"order-lookup-mcp","iat":{now},"exp":NaN,"jti":"jti1"}}'
    b64_c_nan = _b64url_encode(raw_claims_nan.encode("utf-8"))
    sig_nan = signer.sign(f"{b64_h}.{b64_c_nan}".encode("ascii"))
    token_nan = f"{b64_h}.{b64_c_nan}.{_b64url_encode(sig_nan)}"

    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(token_nan, resolver=resolver, audience="order-lookup-mcp", allowed_issuers=["triage-router"], key_prefix=KID_PREFIX, now=now)
    assert exc.value.reason == "malformed"

    raw_claims_inf = f'{{"iss":"triage-router","sub":"cust_1001","aud":"order-lookup-mcp","iat":{now},"exp":Infinity,"jti":"jti1"}}'
    b64_c_inf = _b64url_encode(raw_claims_inf.encode("utf-8"))
    sig_inf = signer.sign(f"{b64_h}.{b64_c_inf}".encode("ascii"))
    token_inf = f"{b64_h}.{b64_c_inf}.{_b64url_encode(sig_inf)}"

    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(token_inf, resolver=resolver, audience="order-lookup-mcp", allowed_issuers=["triage-router"], key_prefix=KID_PREFIX, now=now)
    assert exc.value.reason == "malformed"


# REQ-AUTH-003
def test_verify_non_string_iss_and_aud(ec_key_pair):
    """Verify that non-string iss and aud raise bad_issuer/bad_audience without TypeError."""
    priv_key, pub_key = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})
    now = 1730000000

    def _craft_token(claims_dict: dict[str, Any]) -> str:
        h = {"alg": "ES256", "typ": "JWT", "kid": KID_1}
        b64_h = _b64url_encode(json.dumps(h, separators=(",", ":")).encode("utf-8"))
        b64_c = _b64url_encode(json.dumps(claims_dict, separators=(",", ":")).encode("utf-8"))
        sig = signer.sign(f"{b64_h}.{b64_c}".encode("ascii"))
        return f"{b64_h}.{b64_c}.{_b64url_encode(sig)}"

    # List aud
    token_list_aud = _craft_token({
        "iss": "triage-router", "sub": "cust_1001", "aud": ["order-lookup-mcp"],
        "iat": now, "exp": now + 300, "jti": "j1",
    })
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(token_list_aud, resolver=resolver, audience="order-lookup-mcp", allowed_issuers=["triage-router"], key_prefix=KID_PREFIX, now=now)
    assert exc.value.reason == "bad_audience"

    # List iss
    token_list_iss = _craft_token({
        "iss": ["triage-router"], "sub": "cust_1001", "aud": "order-lookup-mcp",
        "iat": now, "exp": now + 300, "jti": "j1",
    })
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(token_list_iss, resolver=resolver, audience="order-lookup-mcp", allowed_issuers=["triage-router"], key_prefix=KID_PREFIX, now=now)
    assert exc.value.reason == "bad_issuer"


# REQ-AUTH-003
def test_verify_negative_and_zero_ttl(ec_key_pair):
    """Verify that exp - iat <= 0 raises malformed."""
    priv_key, pub_key = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})
    now = 1730000000

    def _craft_token(exp_val: int, iat_val: int) -> str:
        h = {"alg": "ES256", "typ": "JWT", "kid": KID_1}
        c = {"iss": "triage-router", "sub": "cust_1001", "aud": "order-lookup-mcp", "iat": iat_val, "exp": exp_val, "jti": "j1"}
        b64_h = _b64url_encode(json.dumps(h, separators=(",", ":")).encode("utf-8"))
        b64_c = _b64url_encode(json.dumps(c, separators=(",", ":")).encode("utf-8"))
        sig = signer.sign(f"{b64_h}.{b64_c}".encode("ascii"))
        return f"{b64_h}.{b64_c}.{_b64url_encode(sig)}"

    # exp == iat
    token_zero = _craft_token(now, now)
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(token_zero, resolver=resolver, audience="order-lookup-mcp", allowed_issuers=["triage-router"], key_prefix=KID_PREFIX, now=now)
    assert exc.value.reason == "malformed"

    # exp < iat (negative TTL)
    token_neg = _craft_token(now - 10, now)
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(token_neg, resolver=resolver, audience="order-lookup-mcp", allowed_issuers=["triage-router"], key_prefix=KID_PREFIX, now=now)
    assert exc.value.reason == "malformed"


# REQ-AUTH-002
def test_mint_ttl_s_must_be_int_not_bool(ec_key_pair):
    """Verify that mint rejects bool or non-int ttl_s."""
    priv_key, _ = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)

    with pytest.raises(ValueError):
        mint_customer_context_token(
            signer=signer,
            issuer="triage-router",
            audience="order-lookup-mcp",
            customer_id="cust_1001",
            ticket_id=None,
            ttl_s=True,  # bool
        )

    with pytest.raises(ValueError):
        mint_customer_context_token(
            signer=signer,
            issuer="triage-router",
            audience="order-lookup-mcp",
            customer_id="cust_1001",
            ticket_id=None,
            ttl_s=False,  # bool
        )


# REQ-AUTH-003
def test_verify_high_s_signature_rejected(ec_key_pair):
    """Verify that high-S signatures are rejected with bad_signature."""
    priv_key, pub_key = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})
    now = 1730000000

    token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
        now=now,
    )
    parts = token.split(".")
    raw_sig = _b64url_decode(parts[2])
    r = int.from_bytes(raw_sig[:32], byteorder="big")
    s = int.from_bytes(raw_sig[32:], byteorder="big")

    # Flip low-S to high-S
    s_high = SECP256R1_ORDER - s
    assert s_high > SECP256R1_ORDER // 2

    raw_high_s = r.to_bytes(32, byteorder="big") + s_high.to_bytes(32, byteorder="big")
    token_high_s = f"{parts[0]}.{parts[1]}.{_b64url_encode(raw_high_s)}"

    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(
            token_high_s,
            resolver=resolver,
            audience="order-lookup-mcp",
            allowed_issuers=["triage-router"],
            key_prefix=KID_PREFIX,
            now=now,
        )
    assert exc.value.reason == "bad_signature"


# REQ-AUTH-003
def test_verify_strict_base64url_decode(ec_key_pair):
    """Verify that non-canonical base64url inputs raise malformed."""
    priv_key, pub_key = ec_key_pair
    signer = LocalEcSigner(private_key=priv_key, kid=KID_1)
    resolver = StaticPublicKeyResolver({KID_1: pub_key})

    token = mint_customer_context_token(
        signer=signer,
        issuer="triage-router",
        audience="order-lookup-mcp",
        customer_id="cust_1001",
        ticket_id=None,
    )
    parts = token.split(".")

    # 1. Invalid character in header
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(
            f"{parts[0]}!@#.{parts[1]}.{parts[2]}",
            resolver=resolver, audience="order-lookup-mcp", allowed_issuers=["triage-router"], key_prefix=KID_PREFIX,
        )
    assert exc.value.reason == "malformed"

    # 2. Length % 4 == 1 in claims
    # Adding 1 valid base64 char makes len % 4 == 1 if len % 4 was 0
    bad_len_claims = parts[1] + "A" * ((5 - len(parts[1]) % 4) % 4)
    if len(bad_len_claims) % 4 != 1:
        bad_len_claims = parts[1] + "A"
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(
            f"{parts[0]}.{bad_len_claims}.{parts[2]}",
            resolver=resolver, audience="order-lookup-mcp", allowed_issuers=["triage-router"], key_prefix=KID_PREFIX,
        )
    assert exc.value.reason == "malformed"

    # 3. Non-canonical padding in signature (e.g. '=' present in token parts)
    padded_sig = parts[2] + "=="
    with pytest.raises(CustomerContextError) as exc:
        verify_customer_context_token(
            f"{parts[0]}.{parts[1]}.{padded_sig}",
            resolver=resolver, audience="order-lookup-mcp", allowed_issuers=["triage-router"], key_prefix=KID_PREFIX,
        )
    assert exc.value.reason == "malformed"


# REQ-AUTH-002, REQ-AUTH-003
def test_kms_signer_and_resolver_pickling(ec_key_pair):
    """Verify that KmsSigner and KmsPublicKeyResolver can be pickled and unpickled."""
    priv_key, pub_key = ec_key_pair

    class FakeSession:
        def post(self, url: str, json: dict[str, Any], timeout: float) -> Any:
            class FakeResp:
                def raise_for_status(self) -> None:
                    pass

                def json(self) -> dict[str, Any]:
                    digest_b64 = json["digest"]["sha256"]
                    digest = base64.b64decode(digest_b64)
                    der_sig = priv_key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
                    return {"signature": base64.b64encode(der_sig).decode("ascii")}

            return FakeResp()

        def get(self, url: str, timeout: float) -> Any:
            pem = pub_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("ascii")

            class FakeResp:
                status_code = 200

                def json(self) -> dict[str, Any]:
                    return {"pem": pem}

            return FakeResp()

    fake_session = FakeSession()

    # 1. KmsSigner
    signer = KmsSigner(key_version_name=KID_1, session=fake_session)
    sig = signer.sign(b"test message")
    assert len(sig) == 64

    pickled_signer = pickle.dumps(signer)
    unpickled_signer: KmsSigner = pickle.loads(pickled_signer)
    assert unpickled_signer.kid == KID_1
    assert unpickled_signer._session is None
    assert unpickled_signer._lock is not None

    # 2. KmsPublicKeyResolver
    resolver = KmsPublicKeyResolver(session=fake_session, cache_ttl_s=1800)
    resolved_key = resolver.get(KID_1)
    assert isinstance(resolved_key, ec.EllipticCurvePublicKey)

    pickled_resolver = pickle.dumps(resolver)
    unpickled_resolver: KmsPublicKeyResolver = pickle.loads(pickled_resolver)
    assert unpickled_resolver.cache_ttl_s == 1800
    assert unpickled_resolver._session is None
    assert len(unpickled_resolver._cache) == 0
    assert len(unpickled_resolver._negative_cache) == 0
    assert unpickled_resolver._lock is not None


# REQ-AUTH-003
def test_kms_resolver_cache_bounds_and_negative_caching(ec_key_pair):
    """Verify cache bounding (max 16 entries, evict oldest) and negative caching for 30s."""
    _, pub_key = ec_key_pair
    pem = pub_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")

    call_counts: dict[str, int] = {}

    class TrackingSession:
        def get(self, url: str, timeout: float) -> Any:
            kid = url.split("v1/", 1)[1].rsplit("/publicKey", 1)[0]
            call_counts[kid] = call_counts.get(kid, 0) + 1
            if "failing" in kid:
                class FailResp:
                    status_code = 500

                    def json(self) -> dict[str, Any]:
                        return {"error": "server_error"}

                return FailResp()

            class SuccessResp:
                status_code = 200

                def json(self) -> dict[str, Any]:
                    return {"pem": pem}

            return SuccessResp()

    session = TrackingSession()
    resolver = KmsPublicKeyResolver(session=session, cache_ttl_s=3600)

    # 1. Negative caching for failing kid
    failing_kid = f"{KID_PREFIX}cryptoKeyVersions/999_failing"
    with pytest.raises(CustomerContextError) as exc:
        resolver.get(failing_kid)
    assert exc.value.reason == "key_fetch_failed"
    assert call_counts[failing_kid] == 1

    # Second call should hit negative cache and NOT invoke session again
    with pytest.raises(CustomerContextError) as exc2:
        resolver.get(failing_kid)
    assert exc2.value.reason == "key_fetch_failed"
    assert "Negative cache hit" in str(exc2.value)
    assert call_counts[failing_kid] == 1  # Session was not called a second time

    # 2. Bounded cache: insert 17 keys, verify max 16 entries and oldest evicted
    for i in range(1, 18):
        kid = f"{KID_PREFIX}cryptoKeyVersions/{i}"
        resolver.get(kid)

    assert len(resolver._cache) == 16
    # Oldest key (version 1) was evicted
    assert f"{KID_PREFIX}cryptoKeyVersions/1" not in resolver._cache
    assert f"{KID_PREFIX}cryptoKeyVersions/17" in resolver._cache

