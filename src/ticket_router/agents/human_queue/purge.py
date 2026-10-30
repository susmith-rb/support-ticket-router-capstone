"""FastAPI router for redaction map TTL purge operations.

# REQ-STORE-010: Redaction Map 30-Day TTL Purge
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ticket_router.store.interfaces import TicketRepository

logger = logging.getLogger("ticket_router.agents.human_queue.purge")

router = APIRouter()

TokenVerifier = Callable[[str, Optional[str], Optional[str]], Dict[str, Any]]
_custom_token_verifier: Optional[TokenVerifier] = None


def set_token_verifier(verifier: Optional[TokenVerifier]) -> None:
    """Inject a custom token verifier for testing without live Google auth."""
    global _custom_token_verifier
    _custom_token_verifier = verifier


def default_verify_oidc_token(
    token: str,
    expected_audience: Optional[str],
    allowed_email: Optional[str],
) -> Dict[str, Any]:
    """Verify Google OIDC token signature, audience, email_verified, and caller email."""
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import id_token

    auth_request = GoogleAuthRequest()
    try:
        claims = id_token.verify_oauth2_token(
            token,
            auth_request,
            audience=expected_audience,
        )
    except Exception as err:
        raise ValueError(f"Invalid OIDC token: {err}") from err

    if not claims.get("email_verified"):
        raise ValueError("OIDC token email_verified is not true")

    caller_email = claims.get("email")
    if allowed_email and caller_email != allowed_email:
        raise ValueError(
            f"Caller email '{caller_email}' does not match allowed invoker '{allowed_email}'"
        )

    return claims


def verify_purge_auth(
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """FastAPI dependency enforcing OIDC authentication from Cloud Scheduler.

    # REQ-STORE-010: Requires Google OIDC bearer token matching PURGE_AUDIENCE
    # and PURGE_INVOKER_EMAIL with email_verified == True. Any other caller -> 401.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header (Bearer token required)",
        )

    token = authorization[7:].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Empty Bearer token in Authorization header",
        )

    expected_aud = os.environ.get("PURGE_AUDIENCE")
    allowed_email = os.environ.get("PURGE_INVOKER_EMAIL")

    verifier = _custom_token_verifier or default_verify_oidc_token
    try:
        claims = verifier(token, expected_aud, allowed_email)
    except Exception as err:
        logger.warning("Purge token verification failed: %s", err)
        raise HTTPException(status_code=401, detail=f"Unauthorized: {err}") from err

    if not isinstance(claims, dict) or not claims.get("email_verified"):
        raise HTTPException(status_code=401, detail="Unauthorized: email_verified is required")
    caller_email = claims.get("email")
    if allowed_email and caller_email != allowed_email:
        raise HTTPException(status_code=401, detail="Unauthorized: caller email mismatch")

    return claims


def get_ticket_repo(request: Request) -> TicketRepository:
    """Dependency retrieving configured TicketRepository from app state."""
    if hasattr(request.app.state, "ticket_repo"):
        return request.app.state.ticket_repo
    if hasattr(request.app.state, "agent") and hasattr(request.app.state.agent, "repo"):
        return request.app.state.agent.repo

    allow_fake = os.getenv("ALLOW_FAKE_REPOS", "").lower() in ("1", "true")
    if allow_fake:
        from ticket_router.store.fake import FakeTicketRepo

        return FakeTicketRepo()

    raise HTTPException(status_code=500, detail="TicketRepository not configured on app state")


class PurgeResponse(BaseModel):
    deleted: int = Field(..., description="Number of expired redaction maps purged")


@router.post(
    "/internal/purge-expired-redaction-maps",
    response_model=PurgeResponse,
)
def purge_expired_redaction_maps(
    request: Request,
    claims: Dict[str, Any] = Depends(verify_purge_auth),
    repo: TicketRepository = Depends(get_ticket_repo),
) -> PurgeResponse:
    """Purge redaction maps past their 30-day TTL (REQ-STORE-010).

    Invoked daily by Cloud Scheduler. Emits deleted row count (never PII) to logs.
    """
    deleted_count = repo.purge_expired_redaction_maps()
    logger.info(
        "Purged %d expired redaction maps (caller=%s)",
        deleted_count,
        claims.get("email", "unknown"),
    )
    return PurgeResponse(deleted=deleted_count)
