"""Google Cloud OIDC ID token provider with per-audience caching.

# REQ-AGENT-006: Inter-Agent A2A Handoff Protocol
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
"""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

_ID_TOKEN_CACHE: Dict[str, Tuple[str, float]] = {}
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


def _fetch_id_token_with_fallback(req: Any, audience: str, fetch: Callable[[Any, str], str]) -> str:
    """fetch_id_token, retrying with the explicit SA email when `default` is refused.

    Cloud Build's metadata server returns 404 "please provide a user-specified
    service account" for .../service-accounts/default/identity when the build
    runs as a user-specified SA; the same call by email succeeds.
    """
    try:
        return fetch(req, audience)
    except Exception as exc:  # google.auth.exceptions.DefaultCredentialsError / TransportError
        if "user-specified service account" not in str(exc):
            raise
    import requests

    headers = {"Metadata-Flavor": "Google"}
    email = requests.get(f"{_METADATA_SA}/default/email", headers=headers, timeout=5).text.strip()
    resp = requests.get(
        f"{_METADATA_SA}/{email}/identity",
        params={"audience": audience, "format": "full"},
        headers=headers,
        timeout=5,
    )
    resp.raise_for_status()
    return resp.text.strip()


def get_id_token(
    audience: str,
    request: Optional[Any] = None,
    clock: Optional[Callable[[], float]] = None,
    force_refresh: bool = False,
) -> str:
    """Fetch and return a Google OIDC ID token for target audience with caching.

    Caches per-audience until (exp - 60) seconds per runtime wiring contract.
    Works natively on GKE Workload Identity and Vertex AI Agent Engine metadata.
    """
    now = clock() if clock is not None else time.time()

    with _CACHE_LOCK:
        if not force_refresh and audience in _ID_TOKEN_CACHE:
            cached_token, expires_at = _ID_TOKEN_CACHE[audience]
            if now < expires_at:
                return cached_token

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
        _ID_TOKEN_CACHE[audience] = (token, expires_at)

    return token
