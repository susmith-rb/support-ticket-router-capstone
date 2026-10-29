"""Identity resolution mapping IdP tokens to database customer entities via ticket-db-mcp.

# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution via ticket-db-mcp
# REQ-IDENT-004: Fail-Closed Anonymous Fallback & Authorization Boundaries
# REQ-IDENT-007: Identity Threat Mitigations (Anti-Spoofing, Replay, IDOR, Suspension)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ticket_router.identity.verify import (
    TokenVerificationError,
    VerifiedIdentity,
    get_cached_identity,
    set_cached_identity,
    verify_customer_token,
    verify_google_oauth_token,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CustomerContext:
    """Resolved customer identity context bound to verified session state."""

    customer_id: Optional[str]
    verified: bool
    issuer: Optional[str]
    reason: str  # 'ok' | 'missing_token' | 'invalid_token' | 'expired' | 'unlinked' | 'suspended'
    subject: Optional[str] = None
    email_verified: bool = False

    @property
    def customer_verified(self) -> bool:
        return self.verified

    @property
    def identity_issuer(self) -> Optional[str]:
        return self.issuer


ResolvedCustomerContext = CustomerContext


def resolve_identity(
    token: Optional[str] = None,
    ticket_db_client: Any = None,
    project_id: Optional[str] = None,
    request: Optional[Any] = None,
    clock: Optional[Callable[[], float]] = None,
    oauth_access_token: Optional[str] = None,
    expected_oauth_client_id: Optional[str] = None,
    oauth_verifier: Optional[Callable[..., VerifiedIdentity]] = None,
) -> CustomerContext:
    """Resolve raw customer token into verified CustomerContext via ticket-db-mcp.

    Guarantees fail-closed semantics:
    - Missing token -> reason='missing_token', verified=False, customer_id=None
    - Invalid signature/claims -> reason='invalid_token', verified=False, customer_id=None
    - Expired token -> reason='expired', verified=False, customer_id=None
    - Unlinked subject -> reason='unlinked', verified=False, customer_id=None
    - Suspended customer -> reason='suspended', verified=False, customer_id=None
    - Active customer -> reason='ok', verified=True, customer_id=resolved_id
    """
    # 1. Precedence: Firebase JWT token (REQ-IDENT-003)
    if token and isinstance(token, str) and token.strip():
        raw_token = token.strip()

        # Check in-process server-side cache
        cached = get_cached_identity(raw_token, clock=clock)
        if cached is not None:
            _, cached_ctx = cached
            if isinstance(cached_ctx, CustomerContext):
                return cached_ctx

        # Cryptographic token verification
        try:
            verified: VerifiedIdentity = verify_customer_token(
                token=raw_token,
                project_id=project_id,
                request=request,
                clock=clock,
            )
        except ValueError as exc:
            err_msg = str(exc).lower()
            if "expired" in err_msg:
                return CustomerContext(
                    customer_id=None,
                    verified=False,
                    issuer=None,
                    reason="expired",
                )
            return CustomerContext(
                customer_id=None,
                verified=False,
                issuer=None,
                reason="invalid_token",
            )
        except Exception as exc:
            logger.warning("Unexpected error during customer token verification: %s", exc)
            return CustomerContext(
                customer_id=None,
                verified=False,
                issuer=None,
                reason="invalid_token",
            )

        # Query ticket-db-mcp resolve_customer(issuer, subject)
        try:
            resp = ticket_db_client.resolve_customer(
                issuer=verified.issuer,
                subject=verified.subject,
            )
        except Exception as exc:
            logger.warning(
                "Failed to resolve customer identity via ticket-db-mcp: %s", exc
            )
            return CustomerContext(
                customer_id=None,
                verified=False,
                issuer=verified.issuer,
                reason="unlinked",
                subject=verified.subject,
                email_verified=verified.email_verified,
            )

        if not isinstance(resp, dict):
            logger.warning("Invalid resolve_customer response type: %s", type(resp))
            return CustomerContext(
                customer_id=None,
                verified=False,
                issuer=verified.issuer,
                reason="unlinked",
                subject=verified.subject,
                email_verified=verified.email_verified,
            )

        status = str(resp.get("status", "")).lower()
        linked = bool(resp.get("linked", False))
        resolved_id = resp.get("customer_id")

        if status == "suspended":
            ctx = CustomerContext(
                customer_id=None,
                verified=False,
                issuer=verified.issuer,
                reason="suspended",
                subject=verified.subject,
                email_verified=verified.email_verified,
            )
            set_cached_identity(raw_token, verified, ctx, clock=clock)
            return ctx

        if not linked or status == "unlinked" or not resolved_id or status != "active":
            ctx = CustomerContext(
                customer_id=None,
                verified=False,
                issuer=verified.issuer,
                reason="unlinked",
                subject=verified.subject,
                email_verified=verified.email_verified,
            )
            set_cached_identity(raw_token, verified, ctx, clock=clock)
            return ctx

        ctx = CustomerContext(
            customer_id=str(resolved_id),
            verified=True,
            issuer=verified.issuer,
            reason="ok",
            subject=verified.subject,
            email_verified=verified.email_verified,
        )
        set_cached_identity(raw_token, verified, ctx, clock=clock)
        return ctx

    # 2. Google OAuth 2.0 Access Token verification (REQ-IDENT-009)
    if oauth_access_token and isinstance(oauth_access_token, str) and oauth_access_token.strip():
        raw_oauth = oauth_access_token.strip()
        cache_key = f"oauth:{raw_oauth}"
        cached = get_cached_identity(cache_key, clock=clock)
        if cached is not None:
            _, cached_ctx = cached
            if isinstance(cached_ctx, CustomerContext):
                return cached_ctx

        verifier = oauth_verifier or verify_google_oauth_token
        try:
            verified_oauth = verifier(
                raw_oauth,
                expected_client_id=expected_oauth_client_id,
                clock=clock,
            )
        except TokenVerificationError as exc:
            err_msg = str(exc).lower()
            if "expired" in err_msg:
                return CustomerContext(
                    customer_id=None,
                    verified=False,
                    issuer=None,
                    reason="expired",
                )
            return CustomerContext(
                customer_id=None,
                verified=False,
                issuer=None,
                reason="invalid_token",
            )
        except ValueError as exc:
            err_msg = str(exc).lower()
            if "expired" in err_msg:
                return CustomerContext(
                    customer_id=None,
                    verified=False,
                    issuer=None,
                    reason="expired",
                )
            return CustomerContext(
                customer_id=None,
                verified=False,
                issuer=None,
                reason="invalid_token",
            )
        except Exception as exc:
            logger.warning("Unexpected error during Google OAuth token verification: %s", exc)
            return CustomerContext(
                customer_id=None,
                verified=False,
                issuer=None,
                reason="invalid_token",
            )

        # Resolve via ticket-db-mcp resolve_customer(issuer, subject)
        try:
            resp = ticket_db_client.resolve_customer(
                issuer=verified_oauth.issuer,
                subject=verified_oauth.subject,
            )
        except Exception as exc:
            logger.warning(
                "Failed to resolve customer identity via ticket-db-mcp: %s", exc
            )
            return CustomerContext(
                customer_id=None,
                verified=False,
                issuer=verified_oauth.issuer,
                reason="unlinked",
                subject=verified_oauth.subject,
                email_verified=verified_oauth.email_verified,
            )

        if not isinstance(resp, dict):
            logger.warning("Invalid resolve_customer response type: %s", type(resp))
            return CustomerContext(
                customer_id=None,
                verified=False,
                issuer=verified_oauth.issuer,
                reason="unlinked",
                subject=verified_oauth.subject,
                email_verified=verified_oauth.email_verified,
            )

        status = str(resp.get("status", "")).lower()
        linked = bool(resp.get("linked", False))
        resolved_id = resp.get("customer_id")

        if status == "suspended":
            ctx = CustomerContext(
                customer_id=None,
                verified=False,
                issuer=verified_oauth.issuer,
                reason="suspended",
                subject=verified_oauth.subject,
                email_verified=verified_oauth.email_verified,
            )
            set_cached_identity(cache_key, verified_oauth, ctx, clock=clock)
            return ctx

        if not linked or status == "unlinked" or not resolved_id or status != "active":
            ctx = CustomerContext(
                customer_id=None,
                verified=False,
                issuer=verified_oauth.issuer,
                reason="unlinked",
                subject=verified_oauth.subject,
                email_verified=verified_oauth.email_verified,
            )
            set_cached_identity(cache_key, verified_oauth, ctx, clock=clock)
            return ctx

        ctx = CustomerContext(
            customer_id=str(resolved_id),
            verified=True,
            issuer=verified_oauth.issuer,
            reason="ok",
            subject=verified_oauth.subject,
            email_verified=verified_oauth.email_verified,
        )
        set_cached_identity(cache_key, verified_oauth, ctx, clock=clock)
        return ctx

    # 3. Anonymous / missing token fallback (REQ-IDENT-004)
    return CustomerContext(
        customer_id=None,
        verified=False,
        issuer=None,
        reason="missing_token",
    )

