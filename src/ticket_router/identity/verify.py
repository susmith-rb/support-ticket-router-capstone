"""Identity Platform customer token verification and server-side caching.

# REQ-IDENT-001: Cloud Identity Platform IdP Configuration & Token Format
# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution
# REQ-IDENT-007: Identity Threat Mitigations (Anti-Spoofing, Replay, IDOR, Suspension)
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from ticket_router.common.settings import Settings

logger = logging.getLogger(__name__)

# Server-side identity cache: sha256(token) -> (VerifiedIdentity, Any (CustomerContext), expires_at: float)
_IDENTITY_CACHE: Dict[str, Tuple[VerifiedIdentity, Any, float]] = {}
_CACHE_LOCK = threading.Lock()
MAX_CACHE_SIZE = 1000


@dataclass(frozen=True)
class VerifiedIdentity:
    """Cryptographically verified customer Identity Platform claims."""

    issuer: str
    subject: str
    email_verified: bool
    exp: float


def hash_token(token: str) -> str:
    """Compute deterministic SHA-256 hash of raw token for safe cache indexing."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clear_identity_cache() -> None:
    """Clear all in-memory cached identity entries."""
    with _CACHE_LOCK:
        _IDENTITY_CACHE.clear()


def get_identity_cache_size() -> int:
    """Return number of entries in the in-memory identity cache."""
    with _CACHE_LOCK:
        return len(_IDENTITY_CACHE)


def get_cached_identity(
    token: str,
    clock: Optional[Callable[[], float]] = None,
) -> Optional[Tuple[VerifiedIdentity, Any]]:
    """Look up verified identity and resolved context from thread-safe cache."""
    now = clock() if clock is not None else time.time()
    key = hash_token(token)
    with _CACHE_LOCK:
        entry = _IDENTITY_CACHE.get(key)
        if entry is None:
            return None
        identity, resolved_ctx, expires_at = entry
        if now < expires_at:
            return identity, resolved_ctx
        # Expired: evict
        del _IDENTITY_CACHE[key]
        return None


def set_cached_identity(
    token: str,
    identity: VerifiedIdentity,
    resolved_ctx: Any,
    clock: Optional[Callable[[], float]] = None,
) -> None:
    """Store verified identity in server-side cache with bounded TTL."""
    now = clock() if clock is not None else time.time()
    # TTL: min(exp, now + 300s)
    expires_at = min(identity.exp, now + 300.0)
    key = hash_token(token)

    with _CACHE_LOCK:
        # Bounded size management
        if len(_IDENTITY_CACHE) >= MAX_CACHE_SIZE and key not in _IDENTITY_CACHE:
            # Evict expired entries first
            expired_keys = [k for k, v in _IDENTITY_CACHE.items() if now >= v[2]]
            for k in expired_keys:
                del _IDENTITY_CACHE[k]
            # If still full, pop first item (FIFO)
            if len(_IDENTITY_CACHE) >= MAX_CACHE_SIZE:
                oldest_key = next(iter(_IDENTITY_CACHE))
                del _IDENTITY_CACHE[oldest_key]

        _IDENTITY_CACHE[key] = (identity, resolved_ctx, expires_at)


def verify_customer_token(
    token: str,
    project_id: Optional[str] = None,
    request: Optional[Any] = None,
    clock: Optional[Callable[[], float]] = None,
) -> VerifiedIdentity:
    """Cryptographically verify Firebase/Identity Platform customer JWT.

    Validates signature, iss, aud, exp, iat, and sub claim non-emptiness.
    Enforces strict binding to securetoken.google.com/<project> and project audience.
    email_verified is recorded for audit but NOT required.

    Raises:
        ValueError: If token is malformed, invalid, expired, or claims mismatch.
    """
    if not token or not isinstance(token, str) or not token.strip():
        raise ValueError("Missing or invalid customer ID token: token must be a non-empty string.")

    raw_token = token.strip()
    target_project = project_id or Settings.from_env().project
    expected_issuer = f"https://securetoken.google.com/{target_project}"
    expected_audience = target_project
    now = clock() if clock is not None else time.time()

    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import id_token as google_id_token

    req = request if request is not None else GoogleAuthRequest()

    try:
        claims = google_id_token.verify_firebase_token(
            raw_token,
            req,
            audience=expected_audience,
        )
    except Exception as exc:
        raise ValueError(f"Customer token verification failed: {exc}") from exc

    if not isinstance(claims, dict):
        raise ValueError("Invalid token payload: expected claims dictionary.")

    # Defense in depth: explicit issuer check
    iss = claims.get("iss")
    if iss != expected_issuer:
        raise ValueError(
            f"Issuer mismatch: expected '{expected_issuer}', got '{iss}'."
        )

    # Defense in depth: explicit audience check
    aud = claims.get("aud")
    if aud != expected_audience:
        raise ValueError(
            f"Audience mismatch: expected '{expected_audience}', got '{aud}'."
        )

    # Reject missing or empty subject
    sub = claims.get("sub")
    if not sub or not isinstance(sub, str) or not sub.strip():
        raise ValueError("Token missing required 'sub' (subject UID) claim.")

    # Expiry verification
    exp_val = claims.get("exp")
    if exp_val is None:
        raise ValueError("Token missing required 'exp' claim.")
    exp = float(exp_val)
    if now >= exp:
        raise ValueError(f"Token expired at timestamp {exp} (current time {now}).")

    # email_verified recorded, NOT required
    email_verified = bool(claims.get("email_verified", False))

    return VerifiedIdentity(
        issuer=str(iss),
        subject=str(sub).strip(),
        email_verified=email_verified,
        exp=exp,
    )
