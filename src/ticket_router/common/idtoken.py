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
    """Clear all in-memory cached ID tokens."""
    with _CACHE_LOCK:
        _ID_TOKEN_CACHE.clear()


_METADATA_SA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts"


def _generate_id_token_via_iamcredentials(
    sa_email: str,
    audience: str,
    session: Optional[Any] = None,
) -> str:
    """Mint a Google-signed OIDC ID token for sa_email via IAM Credentials API.

    # REQ-AUTH-004: Mints ID tokens through iamcredentials.googleapis.com using
    # the caller's ADC access token without long-lived keys or metadata ID tokens.
    """
    import requests

    from ticket_router.common import auth

    url = (
        f"https://iamcredentials.googleapis.com/v1/projects/-/"
        f"serviceAccounts/{sa_email}:generateIdToken"
    )
    post_fn = session.post if session is not None else requests.post
    resp = post_fn(
        url,
        json={"audience": audience, "includeEmail": True},
        headers={"Authorization": f"Bearer {auth.get_access_token()}"},
        timeout=10,
    )
    resp.raise_for_status()
    return str(resp.json()["token"])


def _fetch_id_token_with_fallback(req: Any, audience: str, fetch: Callable[[Any, str], str]) -> str:
    """fetch_id_token, falling back to IAM Credentials generateIdToken.

    Cloud Build's metadata server refuses ID tokens for user-specified build SAs
    (404 "please provide a user-specified service account" for `default`, and 404
    for the explicit email too). The fallback mints the token through
    iamcredentials.googleapis.com using the SA's own access token; the SA needs
    roles/iam.serviceAccountTokenCreator on itself (granted in Terraform for sa-cd-deployer).
    """
    try:
        return fetch(req, audience)
    except Exception as exc:  # google.auth.exceptions.DefaultCredentialsError / TransportError
        if "user-specified service account" not in str(exc):
            raise
    import requests

    email = requests.get(
        f"{_METADATA_SA}/default/email", headers={"Metadata-Flavor": "Google"}, timeout=5
    ).text.strip()
    return _generate_id_token_via_iamcredentials(email, audience)


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
    """
    now = clock() if clock is not None else time.time()
    impersonate_sa = os.environ.get("ID_TOKEN_IMPERSONATE_SA", "").strip()
    cache_key = (impersonate_sa, audience)

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
    else:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import id_token as google_id_token

        req = request if request is not None else GoogleAuthRequest()
        token = _fetch_id_token_with_fallback(req, audience, google_id_token.fetch_id_token)

    exp = _extract_jwt_exp(token)
    if exp is not None:
        expires_at = exp - 60.0
    else:
        expires_at = now + 3000.0  # Safe 50-minute default fallback

    with _CACHE_LOCK:
        _ID_TOKEN_CACHE[cache_key] = (token, expires_at)

    return token
