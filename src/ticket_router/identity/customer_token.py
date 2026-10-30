"""Customer Context Token (CCT) minting and verification.

# REQ-AUTH-002: Signed Customer Context Token (minting at TriageRouter)
# REQ-AUTH-003: CCT Transport and Verification at order-lookup-mcp (RLS from verified claims)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Protocol, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

logger = logging.getLogger(__name__)

SECP256R1_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_B64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class CustomerContextError(Exception):
    """Raised when customer context token verification fails."""

    def __init__(self, reason: str, message: Optional[str] = None) -> None:
        super().__init__(message or reason)
        self.reason: str = reason


@dataclass(frozen=True)
class VerifiedCustomerContext:
    """Cryptographically verified customer context claims from CCT."""

    customer_id: str
    issuer: str
    jti: str
    ticket_id: Optional[str]
    expires_at: int


class Signer(Protocol):
    """Signer protocol producing raw 64-byte r||s ES256 signatures."""

    kid: str

    def sign(self, message: bytes) -> bytes:
        ...


class KmsSigner:
    """Cloud KMS asymmetric signer producing raw r||s ES256 signatures."""

    def __init__(self, key_version_name: str, session: Any = None) -> None:
        self.kid: str = key_version_name
        self._session = session
        self._is_mtls: bool = bool(getattr(session, "is_mtls", False))
        self._lock = threading.Lock()

    def __getstate__(self) -> Dict[str, Any]:
        return {"kid": self.kid}

    def __setstate__(self, state: Dict[str, Any]) -> None:
        self.kid = state["kid"]
        self._session = None
        self._is_mtls = False
        self._lock = threading.Lock()

    def _get_session(self) -> Any:
        if self._session is None:
            with self._lock:
                if self._session is None:
                    from ticket_router.common.google_http import create_google_session

                    # Bounded auth refresh attempts (max 1) for predictable latency in signing
                    self._session, self._is_mtls = create_google_session(
                        max_refresh_attempts=1
                    )
        return self._session

    def sign(self, message: bytes) -> bytes:
        digest = hashlib.sha256(message).digest()
        digest_b64 = base64.b64encode(digest).decode("ascii")
        from ticket_router.common.google_http import to_mtls_url

        session = self._get_session()
        is_mtls = bool(getattr(session, "is_mtls", self._is_mtls))
        url = to_mtls_url(
            f"https://cloudkms.googleapis.com/v1/{self.kid}:asymmetricSign",
            is_mtls,
        )
        resp = session.post(
            url,
            json={"digest": {"sha256": digest_b64}},
            timeout=2.0,
        )
        resp.raise_for_status()
        data = resp.json()
        sig_b64 = data["signature"]
        der_sig = base64.b64decode(sig_b64)
        r, s = decode_dss_signature(der_sig)
        if s > SECP256R1_ORDER // 2:
            s = SECP256R1_ORDER - s
        return r.to_bytes(32, byteorder="big") + s.to_bytes(32, byteorder="big")


class LocalEcSigner:
    """Test signer using local cryptography EC P-256 private key returning raw r||s."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey, kid: str) -> None:
        self.private_key = private_key
        self.kid: str = kid

    def sign(self, message: bytes) -> bytes:
        der_sig = self.private_key.sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_sig)
        if s > SECP256R1_ORDER // 2:
            s = SECP256R1_ORDER - s
        return r.to_bytes(32, byteorder="big") + s.to_bytes(32, byteorder="big")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data_str: str) -> bytes:
    if not isinstance(data_str, str) or not data_str:
        raise ValueError("Base64url data must be a non-empty string")
    if not _B64URL_PATTERN.fullmatch(data_str):
        raise ValueError(f"Invalid base64url characters in: {data_str!r}")
    if len(data_str) % 4 == 1:
        raise ValueError(f"Invalid base64url length: {len(data_str)}")
    rem = len(data_str) % 4
    padded = data_str + ("=" * (4 - rem) if rem > 0 else "")
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise ValueError(f"Invalid base64url data: {exc}") from exc
    re_encoded = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if re_encoded != data_str:
        raise ValueError("Non-canonical base64url encoding")
    return decoded


def mint_customer_context_token(
    *,
    signer: Signer,
    issuer: str,
    audience: str,
    customer_id: str,
    ticket_id: Optional[str],
    ttl_s: int = 300,
    now: Optional[int] = None,
    jti: Optional[str] = None,
) -> str:
    """Mint a compact JWS (ES256) Customer Context Token.

    # REQ-AUTH-002
    """
    if not customer_id or customer_id.startswith("anon:"):
        raise ValueError(f"Invalid customer_id for CCT minting: '{customer_id}'")
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int):
        raise ValueError(f"Invalid ttl_s: {ttl_s} (must be int and not bool)")
    if ttl_s <= 0 or ttl_s > 600:
        raise ValueError(f"Invalid ttl_s: {ttl_s} (must be 1..600)")

    iat = int(time.time() if now is None else now)
    exp = iat + ttl_s
    token_jti = uuid.uuid4().hex if jti is None else jti

    header = {
        "alg": "ES256",
        "typ": "JWT",
        "kid": signer.kid,
    }
    claims: Dict[str, Any] = {
        "iss": issuer,
        "sub": customer_id,
        "aud": audience,
        "iat": iat,
        "exp": exp,
        "jti": token_jti,
        "act": {"sub": "triage-router"},
        "tid": ticket_id,
    }

    header_json = json.dumps(header, separators=(",", ":"))
    claims_json = json.dumps(claims, separators=(",", ":"))

    b64_header = _b64url_encode(header_json.encode("utf-8"))
    b64_claims = _b64url_encode(claims_json.encode("utf-8"))

    signing_input = f"{b64_header}.{b64_claims}".encode("ascii")
    raw_signature = signer.sign(signing_input)
    b64_sig = _b64url_encode(raw_signature)

    return f"{b64_header}.{b64_claims}.{b64_sig}"


class PublicKeyResolver(Protocol):
    """Protocol for resolving public keys by kid."""

    def get(self, kid: str) -> ec.EllipticCurvePublicKey:
        ...


def _reject_json_constant(val: str) -> Any:
    raise ValueError(f"Disallowed JSON constant: '{val}'")


class KmsPublicKeyResolver:
    """Cloud KMS public key resolver with bounded TTL caching and negative caching."""

    def __init__(self, session: Any = None, cache_ttl_s: int = 3600) -> None:
        self._session = session
        self._is_mtls: bool = bool(getattr(session, "is_mtls", False))
        self.cache_ttl_s = cache_ttl_s
        self._cache: OrderedDict[str, Tuple[ec.EllipticCurvePublicKey, float]] = OrderedDict()
        self._negative_cache: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def __getstate__(self) -> Dict[str, Any]:
        return {"cache_ttl_s": self.cache_ttl_s}

    def __setstate__(self, state: Dict[str, Any]) -> None:
        self.cache_ttl_s = state.get("cache_ttl_s", 3600)
        self._session = None
        self._is_mtls = False
        self._cache = OrderedDict()
        self._negative_cache = OrderedDict()
        self._lock = threading.Lock()

    def _get_session(self) -> Any:
        if self._session is None:
            with self._lock:
                if self._session is None:
                    from ticket_router.common.google_http import create_google_session

                    # Bounded auth refresh attempts (max 1) for predictable latency in signing
                    self._session, self._is_mtls = create_google_session(
                        max_refresh_attempts=1
                    )
        return self._session

    def _record_negative_cache(self, kid: str) -> None:
        with self._lock:
            if kid in self._negative_cache:
                del self._negative_cache[kid]
            elif len(self._negative_cache) >= 16:
                self._negative_cache.popitem(last=False)
            self._negative_cache[kid] = time.time() + 30.0

    def get(self, kid: str) -> ec.EllipticCurvePublicKey:
        now = time.time()
        with self._lock:
            neg_exp = self._negative_cache.get(kid)
            if neg_exp is not None:
                if now < neg_exp:
                    raise CustomerContextError("key_fetch_failed", f"Negative cache hit for kid '{kid}'")
                del self._negative_cache[kid]

            cached = self._cache.get(kid)
            if cached is not None:
                key, expires_at = cached
                if now < expires_at:
                    return key
                del self._cache[kid]

        session = self._get_session()
        is_mtls = bool(getattr(session, "is_mtls", self._is_mtls))
        from ticket_router.common.google_http import to_mtls_url

        url = to_mtls_url(f"https://cloudkms.googleapis.com/v1/{kid}/publicKey", is_mtls)
        try:
            resp = session.get(url, timeout=2.0)
            if resp.status_code != 200:
                self._record_negative_cache(kid)
                raise CustomerContextError("key_fetch_failed", f"KMS returned HTTP {resp.status_code}")
            data = resp.json()
            pem = data.get("pem")
            if not pem:
                self._record_negative_cache(kid)
                raise CustomerContextError("key_fetch_failed", "Missing pem in KMS response")
            key = serialization.load_pem_public_key(pem.encode("utf-8"))
            if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
                self._record_negative_cache(kid)
                raise CustomerContextError("unknown_kid", "Public key is not EC SECP256R1")
        except CustomerContextError:
            self._record_negative_cache(kid)
            raise
        except Exception as exc:
            self._record_negative_cache(kid)
            raise CustomerContextError("key_fetch_failed", str(exc)) from exc

        with self._lock:
            if kid in self._cache:
                del self._cache[kid]
            elif len(self._cache) >= 16:
                self._cache.popitem(last=False)
            self._cache[kid] = (key, now + self.cache_ttl_s)
        return key


class StaticPublicKeyResolver:
    """Static dictionary public key resolver for tests."""

    def __init__(self, mapping: Dict[str, Any]) -> None:
        self.mapping = mapping

    def get(self, kid: str) -> ec.EllipticCurvePublicKey:
        key = self.mapping.get(kid)
        if key is None:
            raise CustomerContextError("unknown_kid", f"Unknown kid '{kid}'")
        return key


def verify_customer_context_token(
    token: str,
    *,
    resolver: Any,
    audience: str,
    allowed_issuers: Iterable[str],
    key_prefix: str,
    now: Optional[int] = None,
    leeway_s: int = 60,
    max_ttl_s: int = 600,
) -> VerifiedCustomerContext:
    """Verify an incoming Customer Context Token strictly per REQ-AUTH-003.

    # REQ-AUTH-003
    """
    try:
        return _verify_customer_context_token_impl(
            token,
            resolver=resolver,
            audience=audience,
            allowed_issuers=allowed_issuers,
            key_prefix=key_prefix,
            now=now,
            leeway_s=leeway_s,
            max_ttl_s=max_ttl_s,
        )
    except CustomerContextError:
        raise
    except Exception as exc:
        raise CustomerContextError("malformed", f"Unexpected verification failure: {exc}") from exc


def _verify_customer_context_token_impl(
    token: str,
    *,
    resolver: Any,
    audience: str,
    allowed_issuers: Iterable[str],
    key_prefix: str,
    now: Optional[int] = None,
    leeway_s: int = 60,
    max_ttl_s: int = 600,
) -> VerifiedCustomerContext:
    if not isinstance(token, str):
        raise CustomerContextError("malformed", "Token must be a string")

    parts = token.split(".")
    if len(parts) != 3:
        raise CustomerContextError("malformed", "Token must contain exactly 3 dot-separated parts")

    if not key_prefix:
        raise CustomerContextError("unknown_kid", "Empty key_prefix rejected")
    normalized_prefix = key_prefix if key_prefix.endswith("/") else f"{key_prefix}/"

    # 1. Base64url decode all three segments
    try:
        header_bytes = _b64url_decode(parts[0])
    except Exception as exc:
        raise CustomerContextError("malformed", f"Failed to decode header: {exc}") from exc

    try:
        claims_bytes = _b64url_decode(parts[1])
    except Exception as exc:
        raise CustomerContextError("malformed", f"Failed to decode claims: {exc}") from exc

    try:
        raw_sig = _b64url_decode(parts[2])
    except Exception as exc:
        raise CustomerContextError("malformed", f"Failed to decode signature: {exc}") from exc

    # 2. Header parsing & validation
    try:
        header = json.loads(header_bytes.decode("utf-8"), parse_constant=_reject_json_constant)
        if not isinstance(header, dict):
            raise CustomerContextError("malformed", "Header must be a JSON object")
    except CustomerContextError:
        raise
    except Exception as exc:
        raise CustomerContextError("malformed", f"Failed to parse header: {exc}") from exc

    alg = header.get("alg")
    if alg != "ES256":
        raise CustomerContextError("malformed", f"Unsupported alg '{alg}'; ES256 required")

    kid = header.get("kid")
    if not kid or not isinstance(kid, str):
        raise CustomerContextError("unknown_kid", f"Invalid or unexpected kid '{kid}'")

    kid_pattern = re.escape(normalized_prefix) + r"cryptoKeyVersions/[1-9][0-9]{0,5}"
    if not re.fullmatch(kid_pattern, kid):
        raise CustomerContextError("unknown_kid", f"kid '{kid}' does not match expected pattern")

    # 3. Public key resolution
    try:
        pub_key = resolver.get(kid)
    except CustomerContextError:
        raise
    except Exception as exc:
        raise CustomerContextError("unknown_kid", f"Failed to resolve key for kid '{kid}': {exc}") from exc

    if not isinstance(pub_key, ec.EllipticCurvePublicKey) or not isinstance(pub_key.curve, ec.SECP256R1):
        raise CustomerContextError("unknown_kid", f"Resolved key for '{kid}' is not an EC SECP256R1 public key")

    # 4. Signature verification
    if len(raw_sig) != 64:
        raise CustomerContextError("bad_signature", f"Expected 64-byte raw signature, got {len(raw_sig)}")

    r = int.from_bytes(raw_sig[:32], byteorder="big")
    s = int.from_bytes(raw_sig[32:], byteorder="big")
    if r <= 0 or r >= SECP256R1_ORDER or s <= 0 or s > SECP256R1_ORDER // 2:
        raise CustomerContextError("bad_signature", "Non-canonical or high-S signature rejected")

    try:
        der_sig = encode_dss_signature(r, s)
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        pub_key.verify(der_sig, signing_input, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise CustomerContextError("bad_signature", "Invalid token signature") from exc
    except Exception as exc:
        raise CustomerContextError("bad_signature", f"Signature check failed: {exc}") from exc

    # 5. Claims parsing & validation
    try:
        claims = json.loads(claims_bytes.decode("utf-8"), parse_constant=_reject_json_constant)
        if not isinstance(claims, dict):
            raise CustomerContextError("malformed", "Claims must be a JSON object")
    except CustomerContextError:
        raise
    except Exception as exc:
        raise CustomerContextError("malformed", f"Failed to parse claims: {exc}") from exc

    aud = claims.get("aud")
    if not isinstance(aud, str):
        raise CustomerContextError("bad_audience", f"Claim 'aud' must be string, got {type(aud).__name__}")
    if aud != audience:
        raise CustomerContextError("bad_audience", f"Audience mismatch: expected '{audience}', got '{aud}'")

    iss = claims.get("iss")
    if not isinstance(iss, str):
        raise CustomerContextError("bad_issuer", f"Claim 'iss' must be string, got {type(iss).__name__}")
    allowed_set = set(allowed_issuers)
    if iss not in allowed_set:
        raise CustomerContextError("bad_issuer", f"Issuer '{iss}' not in allowed set")

    exp = claims.get("exp")
    iat = claims.get("iat")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
        raise CustomerContextError("malformed", "Claim 'exp' must be finite numeric and not bool")
    if isinstance(iat, bool) or not isinstance(iat, (int, float)) or not math.isfinite(iat):
        raise CustomerContextError("malformed", "Claim 'iat' must be finite numeric and not bool")

    ttl = exp - iat
    if ttl <= 0:
        raise CustomerContextError("malformed", f"Token TTL {ttl} must be positive")
    if ttl > max_ttl_s:
        raise CustomerContextError("ttl_too_long", f"Token TTL {ttl} exceeds maximum {max_ttl_s}")

    now_ts = int(time.time() if now is None else now)
    if exp <= now_ts - leeway_s:
        raise CustomerContextError("expired", f"Token expired (exp={exp}, now={now_ts}, leeway={leeway_s})")
    if iat > now_ts + leeway_s:
        raise CustomerContextError("not_yet_valid", f"Token not yet valid (iat={iat}, now={now_ts}, leeway={leeway_s})")

    sub = claims.get("sub")
    if not sub or not isinstance(sub, str) or sub.startswith("anon:"):
        raise CustomerContextError("anonymous_subject", "Token subject is anonymous or empty")

    jti = str(claims.get("jti") or "")
    tid = claims.get("tid")
    if tid is not None:
        tid = str(tid)

    return VerifiedCustomerContext(
        customer_id=sub,
        issuer=iss,
        jti=jti,
        ticket_id=tid,
        expires_at=int(exp),
    )
