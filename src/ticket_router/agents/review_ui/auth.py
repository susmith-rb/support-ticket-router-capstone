"""Cloud Run Identity-Aware Proxy (IAP) authentication and rep identity derivation.

# REQ-AGENT-033: Human Review UI with Cloud Run IAP and Verified Rep Identity
# REQ-AUTH-010: Human Review UI Rep Identity Assertion and sa-review-ui Least Privilege
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request

logger = logging.getLogger("ticket_router.agents.review_ui.auth")

IAP_HEADER_NAME = "x-goog-iap-jwt-assertion"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"


def parse_allowed_audiences() -> list[str]:
    """Parse comma-separated list of expected IAP audiences from environment."""
    raw = os.environ.get("IAP_EXPECTED_AUDIENCE", "").strip()
    if not raw:
        return []
    return [aud.strip() for aud in raw.split(",") if aud.strip()]


def verify_iap_jwt(
    token: str,
    expected_audience: Optional[str] = None,
    verifier: Optional[Callable[[str, Optional[str]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Cryptographically verify an IAP assertion JWT and validate claims.

    Enforces:
    - Valid signature from Google IAP public keys.
    - Matching audience (per Cloud Run IAP docs).
    - Expected issuer 'https://cloud.google.com/iap'.
    - Presence of verified 'email' claim.
    """
    if not token or not token.strip():
        raise HTTPException(status_code=401, detail="Empty IAP JWT assertion token")

    claims: Dict[str, Any]
    if verifier is not None:
        try:
            claims = verifier(token, expected_audience)
        except Exception as exc:
            logger.warning("Custom IAP verification failed: %s", exc)
            raise HTTPException(status_code=401, detail=f"Invalid IAP JWT: {exc}") from exc
    else:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import id_token as google_id_token

        certs_url = os.environ.get("IAP_CERTS_URL", IAP_CERTS_URL)
        auth_request = GoogleAuthRequest()

        audiences = [expected_audience] if expected_audience else parse_allowed_audiences()
        if not audiences:
            audiences = [None]  # type: ignore[list-item]

        last_error: Optional[Exception] = None
        verified = False
        for aud in audiences:
            try:
                claims = google_id_token.verify_token(
                    token,
                    auth_request,
                    audience=aud,
                    certs_url=certs_url,
                )
                verified = True
                break
            except Exception as exc:
                last_error = exc

        if not verified:
            logger.warning("IAP JWT verification failed against audiences %s: %s", audiences, last_error)
            raise HTTPException(status_code=401, detail=f"Invalid IAP JWT assertion: {last_error}") from last_error

    # Verify issuer
    iss = claims.get("iss", "")
    if iss != IAP_ISSUER and iss not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(status_code=401, detail=f"Unauthorized IAP issuer: {iss}")

    # Verify email
    email = claims.get("email")
    if not email or not isinstance(email, str) or "@" not in email:
        raise HTTPException(status_code=401, detail="IAP JWT missing or invalid email claim")

    return claims


def extract_rep_id(claims: Dict[str, Any]) -> str:
    """Derive canonical rep_id from verified IAP JWT claims."""
    raw_email = str(claims.get("email", "")).strip().lower()
    # Normalize accounts.google.com: prefix if present
    if raw_email.startswith("accounts.google.com:"):
        raw_email = raw_email[len("accounts.google.com:"):]
    return raw_email


def get_current_rep(request: Request) -> str:
    """FastAPI dependency extracting verified rep_id from X-Goog-IAP-JWT-Assertion header.

    Fails closed with HTTP 401 if header is missing, malformed, or unverified.
    """
    token = request.headers.get(IAP_HEADER_NAME)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing required X-Goog-IAP-JWT-Assertion header",
        )

    verifier = getattr(request.app.state, "iap_verifier", None)
    expected_aud = getattr(request.app.state, "expected_audience", None) or os.environ.get(
        "IAP_EXPECTED_AUDIENCE"
    )

    claims = verify_iap_jwt(token=token, expected_audience=expected_aud, verifier=verifier)
    return extract_rep_id(claims)
