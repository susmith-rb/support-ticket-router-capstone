"""mTLS-aware Google HTTP session creation and endpoint URL rewriting.

# REQ-AUTH-006: mTLS-Aware Google API Transports (certificate-bound agent identity tokens)
"""

from __future__ import annotations

import urllib.parse
from typing import Optional, Sequence

import google.auth
from google.auth.transport.requests import AuthorizedSession
from requests.adapters import HTTPAdapter

DEFAULT_SCOPES: Sequence[str] = ("https://www.googleapis.com/auth/cloud-platform",)


def create_google_session(
    scopes: Optional[Sequence[str]] = None,
    pool_connections: int = 10,
    pool_maxsize: int = 20,
    max_retries: int = 0,
    max_refresh_attempts: int = 2,
    refresh_status_codes: Sequence[int] = (401,),
) -> tuple[AuthorizedSession, bool]:
    """Create a pooled AuthorizedSession configured for mTLS if a client cert is available.

    # REQ-AUTH-006: Under Agent Engine agent identity, google-auth requests
    # certificate-bound access tokens from the metadata server. Plain TLS
    # connections to Google APIs are rejected with 401. This function creates
    # an AuthorizedSession, mounts HTTPAdapters with custom connection pooling,
    # and calls configure_mtls_channel() to mount a mutual-TLS adapter when
    # client credentials exist (auto-detected via SPIFFE workload cert).
    #
    # Returns (session, is_mtls).
    """
    effective_scopes = list(scopes) if scopes is not None else list(DEFAULT_SCOPES)
    credentials, _ = google.auth.default(scopes=effective_scopes)
    session = AuthorizedSession(
        credentials,
        refresh_status_codes=refresh_status_codes,
        max_refresh_attempts=max_refresh_attempts,
    )

    adapter = HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        max_retries=max_retries,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.configure_mtls_channel()
    is_mtls = bool(getattr(session, "is_mtls", False))
    return session, is_mtls


def to_mtls_url(url: str, is_mtls: bool) -> str:
    """Rewrite a *.googleapis.com URL to its *.mtls.googleapis.com equivalent when is_mtls is True.

    # REQ-AUTH-006: Only *.googleapis.com hosts not already containing '.mtls.'
    # are rewritten. Handles 'x.googleapis.com', '{region}-aiplatform.googleapis.com',
    # '{svc}.{region}.rep.googleapis.com', and 'x.sandbox.googleapis.com' ->
    # 'x.mtls.sandbox.googleapis.com'.
    # Leaves Private Service Connect (*.p.googleapis.com) unchanged.
    # Preserves scheme, path, query, fragment, and port. Leaves everything else
    # (e.g. *.run.app, *.prediction.vertexai.goog, metadata.google.internal) untouched.
    """
    if not is_mtls or not isinstance(url, str) or not url:
        return url

    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname
    if not hostname or ".mtls." in hostname:
        return url

    # PSC endpoints (*.p.googleapis.com or p.googleapis.com) are already private/VPC-routed
    if hostname == "p.googleapis.com" or hostname.endswith(".p.googleapis.com"):
        return url

    sandbox_suffix = ".sandbox.googleapis.com"
    gapis_suffix = ".googleapis.com"

    if hostname.endswith(sandbox_suffix) and len(hostname) > len(sandbox_suffix):
        prefix = hostname[: -len(sandbox_suffix)]
        new_hostname = f"{prefix}.mtls.sandbox.googleapis.com"
    elif hostname.endswith(gapis_suffix) and len(hostname) > len(gapis_suffix):
        prefix = hostname[: -len(gapis_suffix)]
        new_hostname = f"{prefix}.mtls.googleapis.com"
    else:
        return url

    netloc = new_hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{netloc}"

    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )
