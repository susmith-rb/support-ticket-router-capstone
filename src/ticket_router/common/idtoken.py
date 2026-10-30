"""Google Cloud OIDC ID token provider with per-audience caching.

# REQ-AGENT-006: Inter-Agent A2A Handoff Protocol
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
# REQ-AUTH-004: SPIFFE Agent Identity for TriageRouter (IAM Credentials generateIdToken)
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

_ID_TOKEN_CACHE: Dict[Tuple[str, str], Tuple[str, float]] = {}
_CACHE_LOCK = threading.Lock()

_IAM_SESSION_LOCK = threading.Lock()
_IAM_CACHED_SESSION: Optional[Any] = None
_IAM_CACHED_IS_MTLS: bool = False


def _get_iam_session() -> tuple[Any, bool]:
    """Get or create cached session for IAM Credentials generateIdToken.

    # REQ-AUTH-006: Reuses a module-level pooled session guarded by a lock,
    # capturing (sess, is_mtls) inside the lock so a concurrent _reset_iam_session()
    # cannot yield (None, False).
    """
    global _IAM_CACHED_SESSION, _IAM_CACHED_IS_MTLS
    with _IAM_SESSION_LOCK:
        if _IAM_CACHED_SESSION is None:
            from ticket_router.common.google_http import create_google_session

            sess, is_mtls = create_google_session()
            _IAM_CACHED_IS_MTLS = bool(is_mtls)
            _IAM_CACHED_SESSION = sess
        result = (_IAM_CACHED_SESSION, _IAM_CACHED_IS_MTLS)
    return result


def _reset_iam_session() -> None:
    """Reset cached IAM session (used on exceptions and in tests)."""
    global _IAM_CACHED_SESSION, _IAM_CACHED_IS_MTLS
    with _IAM_SESSION_LOCK:
        _IAM_CACHED_SESSION = None
        _IAM_CACHED_IS_MTLS = False


def _extract_jwt_exp(token: str) -> Optional[float]:
    """Extract exp claim from JWT token payload without cryptographic verification."""
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1]
            rem = len(payload_b64) % 4
            if rem > 0:
                payload_b64 += "=" * (4 - rem)
            payload_json = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
            data = json.loads(payload_json)
            exp_val = data.get("exp")
            if exp_val is not None:
                return float(exp_val)
    except Exception:
        pass
    return None


def clear_id_token_cache() -> None:
    """Clear all in-memory cached ID tokens and reset cached IAM session."""
    with _CACHE_LOCK:
        _ID_TOKEN_CACHE.clear()
    _reset_iam_session()


_METADATA_SA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts"


def _generate_id_token_via_iamcredentials(
    sa_email: str,
    audience: str,
    session: Optional[Any] = None,
) -> str:
    """Mint a Google-signed OIDC ID token for sa_email via IAM Credentials API.

    # REQ-AUTH-004: Mints ID tokens through iamcredentials.googleapis.com using
    # the caller's ADC access token without long-lived keys or metadata ID tokens.
    # REQ-AUTH-006: Uses create_google_session() and to_mtls_url for mTLS transport.
    # Reuses a module-level pooled session, reset on exception.
    """
    from google.auth.transport.requests import AuthorizedSession

    from ticket_router.common import auth
    from ticket_router.common.google_http import to_mtls_url

    used_cached = False
    if session is not None:
        sess = session
        is_mtls = bool(getattr(session, "is_mtls", False))
        headers = {"Authorization": f"Bearer {auth.get_access_token()}"}
    else:
        try:
            sess, is_mtls = _get_iam_session()
            used_cached = True
        except Exception:
            _reset_iam_session()
            raise

        if isinstance(sess, AuthorizedSession):
            headers = None
        else:
            headers = {"Authorization": f"Bearer {auth.get_access_token()}"}

    url = to_mtls_url(
        f"https://iamcredentials.googleapis.com/v1/projects/-/"
        f"serviceAccounts/{sa_email}:generateIdToken",
        is_mtls,
    )
    post_kwargs: dict[str, Any] = {
        "json": {"audience": audience, "includeEmail": True},
        "timeout": 10,
    }
    if headers is not None:
        post_kwargs["headers"] = headers

    try:
        resp = sess.post(url, **post_kwargs)
        resp.raise_for_status()
    except Exception:
        if used_cached:
            _reset_iam_session()
        raise
    return str(resp.json()["token"])


def _fetch_id_token_with_fallback(
    req: Any,
    audience: str,
    fetch: Callable[..., str],
    session: Optional[Any] = None,
) -> str:
    """fetch_id_token requesting UNBOUND tokens, falling back to IAM Credentials.

    Cloud Build's metadata server refuses ID tokens for user-specified build SAs
    (404 "please provide a user-specified service account" for `default`, and 404
    for the explicit email too). The fallback mints the token through
    iamcredentials.googleapis.com using the SA's own access token; the SA needs
    roles/iam.serviceAccountTokenCreator on itself (granted in Terraform for sa-cd-deployer).

    # REQ-AUTH-006: Under Agent Engine agent identity, requests unbound ID tokens
    # (bind_id_token=False) because target services (Cloud Run, FaqResolver) run on
    # plain TLS. Falls back to fetch without bind_id_token on older google-auth.
    """
    try:
        try:
            return fetch(req, audience, bind_id_token=False)
        except TypeError as exc:
            if "bind_id_token" in str(exc):
                return fetch(req, audience)
            raise
    except Exception as exc:  # google.auth.exceptions.DefaultCredentialsError / TransportError
        if "user-specified service account" not in str(exc):
            raise
    import requests

    email = requests.get(
        f"{_METADATA_SA}/default/email", headers={"Metadata-Flavor": "Google"}, timeout=5
    ).text.strip()
    return _generate_id_token_via_iamcredentials(email, audience, session=session)


def is_faq_agent_identity() -> bool:
    """Return True if running under FaqResolver GKE Agent Identity (REQ-AUTH-008)."""
    return (
        os.environ.get("FAQ_AGENT_IDENTITY") == "1"
        or os.path.exists("/var/run/secrets/workload-spiffe-credentials")
    )


def fetch_unbound_agent_identity_token(
    audience: str,
    request: Optional[Any] = None,
) -> str:
    """Fetch an unbound Google OIDC ID token for agent identity (REQ-AUTH-008, REQ-AUTH-009).

    Calls google.oauth2.id_token.fetch_id_token(req, audience, bind_id_token=False).
    Degrades gracefully if bind_id_token kwarg is not supported.
    """
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import id_token as google_id_token

    req = request if request is not None else GoogleAuthRequest()
    try:
        return google_id_token.fetch_id_token(req, audience, bind_id_token=False)
    except TypeError as exc:
        if "bind_id_token" in str(exc):
            return google_id_token.fetch_id_token(req, audience)
        raise


def get_id_token(
    audience: str,
    request: Optional[Any] = None,
    clock: Optional[Callable[[], float]] = None,
    force_refresh: bool = False,
    session: Optional[Any] = None,
) -> str:
    """Fetch and return a Google OIDC ID token for target audience with caching.

    Caches per-audience until (exp - 60) seconds per runtime wiring contract.
    Works natively on GKE Workload Identity and Vertex AI Agent Engine metadata.
    When ID_TOKEN_IMPERSONATE_SA is set (non-empty), mints via IAM Credentials
    generateIdToken using ADC access token and skips metadata fetch (# REQ-AUTH-004).
    When running under GKE Agent Identity, fetches an unbound ID token (# REQ-AUTH-008/009).
    """
    now = clock() if clock is not None else time.time()
    impersonate_sa = os.environ.get("ID_TOKEN_IMPERSONATE_SA", "").strip()
    agent_id_mode = is_faq_agent_identity()
    cache_key = (impersonate_sa, "agent_identity" if agent_id_mode else "metadata", audience)

    with _CACHE_LOCK:
        if not force_refresh and cache_key in _ID_TOKEN_CACHE:
            cached_token, expires_at = _ID_TOKEN_CACHE[cache_key]
            if now < expires_at:
                return cached_token

    if impersonate_sa:
        token = _generate_id_token_via_iamcredentials(
            impersonate_sa,
            audience,
            session=session,
        )
    elif agent_id_mode:
        token = fetch_unbound_agent_identity_token(audience, request=request)
    else:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import id_token as google_id_token

        req = request if request is not None else GoogleAuthRequest()
        token = _fetch_id_token_with_fallback(req, audience, google_id_token.fetch_id_token, session=session)

    exp = _extract_jwt_exp(token)
    if exp is not None:
        expires_at = exp - 60.0
    else:
        expires_at = now + 3000.0  # Safe 50-minute default fallback

    with _CACHE_LOCK:
        _ID_TOKEN_CACHE[cache_key] = (token, expires_at)

    return token
