"""Runtime authentication utilities using Application Default Credentials (ADC).

# REQ-PLAT-002: Runtime auth via google.auth ADC with token caching.
Avoids external subprocess invocation (such as gcloud CLI) in runtime services.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple
import google.auth
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

_CACHED_CREDENTIALS: Optional[Credentials] = None
_CACHED_PROJECT_ID: Optional[str] = None


def get_credentials(
    scopes: Optional[Sequence[str]] = None,
) -> Tuple[Credentials, Optional[str]]:
    """Retrieve Application Default Credentials, reusing cached credentials if valid."""
    global _CACHED_CREDENTIALS, _CACHED_PROJECT_ID
    req_scopes = list(scopes) if scopes else [CLOUD_PLATFORM_SCOPE]
    if _CACHED_CREDENTIALS is not None:
        return _CACHED_CREDENTIALS, _CACHED_PROJECT_ID

    creds, project_id = google.auth.default(scopes=req_scopes)
    _CACHED_CREDENTIALS = creds
    _CACHED_PROJECT_ID = project_id
    return creds, project_id


def get_access_token(scopes: Optional[Sequence[str]] = None) -> str:
    """Retrieve a valid bearer access token, refreshing credentials if needed.

    Cached token is reused until expiration.
    """
    creds, _ = get_credentials(scopes=scopes)
    if not creds.valid:
        request = Request()
        creds.refresh(request)
    return str(creds.token)


def reset_auth_cache() -> None:
    """Reset cached credentials (primarily for test isolation)."""
    global _CACHED_CREDENTIALS, _CACHED_PROJECT_ID
    _CACHED_CREDENTIALS = None
    _CACHED_PROJECT_ID = None
