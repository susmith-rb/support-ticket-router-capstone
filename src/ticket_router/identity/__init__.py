"""Customer identity and authentication subsystem for ticket router.

# REQ-IDENT-001: Cloud Identity Platform IdP Configuration & Token Format
# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution via ticket-db-mcp
# REQ-IDENT-004: Fail-Closed Anonymous Fallback & Authorization Boundaries
# REQ-IDENT-007: Identity Threat Mitigations (Anti-Spoofing, Replay, IDOR, Suspension)
"""

from __future__ import annotations

from ticket_router.identity.resolver import (
    CustomerContext,
    ResolvedCustomerContext,
    resolve_identity,
)
from ticket_router.identity.verify import (
    GOOGLE_ISSUER,
    GOOGLE_TOKENINFO_URL,
    TokenVerificationError,
    VerifiedIdentity,
    clear_identity_cache,
    get_cached_identity,
    get_identity_cache_size,
    hash_token,
    set_cached_identity,
    verify_customer_token,
    verify_google_oauth_token,
)

__all__ = [
    "GOOGLE_ISSUER",
    "GOOGLE_TOKENINFO_URL",
    "CustomerContext",
    "ResolvedCustomerContext",
    "TokenVerificationError",
    "VerifiedIdentity",
    "clear_identity_cache",
    "get_cached_identity",
    "get_identity_cache_size",
    "hash_token",
    "resolve_identity",
    "set_cached_identity",
    "verify_customer_token",
    "verify_google_oauth_token",
]
