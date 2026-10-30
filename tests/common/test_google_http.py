"""Tests for mTLS-aware Google HTTP session creation and URL rewriting.

# REQ-AUTH-006: mTLS-Aware Google API Transports (certificate-bound agent identity tokens)
"""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import MagicMock

import google.auth
import pytest
from google.auth.transport.requests import AuthorizedSession

from ticket_router.common.google_http import create_google_session, to_mtls_url


def test_mtls_session_and_url_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify create_google_session and to_mtls_url under mTLS and non-mTLS conditions.

    # REQ-AUTH-006
    """
    fake_credentials = MagicMock(spec=google.auth.credentials.Credentials)
    mock_auth_default = MagicMock(return_value=(fake_credentials, "test-project"))
    monkeypatch.setattr(google.auth, "default", mock_auth_default)

    # 1. Non-mTLS session creation
    def mock_configure_no_mtls(self: AuthorizedSession, client_cert_callback=None) -> None:
        self._is_mtls = False

    monkeypatch.setattr(AuthorizedSession, "configure_mtls_channel", mock_configure_no_mtls)

    session_no_mtls, is_mtls = create_google_session()
    assert isinstance(session_no_mtls, AuthorizedSession)
    assert is_mtls is False
    assert session_no_mtls.is_mtls is False
    mock_auth_default.assert_called_with(scopes=["https://www.googleapis.com/auth/cloud-platform"])

    # Verify connection pooling adapter is mounted
    https_adapter = session_no_mtls.adapters.get("https://")
    http_adapter = session_no_mtls.adapters.get("http://")
    assert https_adapter is not None
    assert http_adapter is not None
    assert https_adapter._pool_connections == 10
    assert https_adapter._pool_maxsize == 20

    # 2. mTLS session creation with custom scopes and pool sizes
    def mock_configure_with_mtls(self: AuthorizedSession, client_cert_callback=None) -> None:
        self._is_mtls = True

    monkeypatch.setattr(AuthorizedSession, "configure_mtls_channel", mock_configure_with_mtls)

    custom_scopes = ["https://www.googleapis.com/auth/cloudkms"]
    session_mtls, is_mtls = create_google_session(
        scopes=custom_scopes,
        pool_connections=15,
        pool_maxsize=30,
        max_retries=2,
    )
    assert isinstance(session_mtls, AuthorizedSession)
    assert is_mtls is True
    assert session_mtls.is_mtls is True
    mock_auth_default.assert_called_with(scopes=custom_scopes)
    https_adapter_mtls = session_mtls.adapters.get("https://")
    assert https_adapter_mtls is not None
    assert https_adapter_mtls._pool_connections == 15
    assert https_adapter_mtls._pool_maxsize == 30

    # 3. Table-driven URL rewrite cases (REQ-AUTH-006)
    table_cases = [
        # (input_url, expected_when_mtls_true)
        (
            "https://modelarmor.us-central1.rep.googleapis.com/v1/templates/t:sanitize",
            "https://modelarmor.us-central1.rep.mtls.googleapis.com/v1/templates/t:sanitize",
        ),
        (
            "https://cloudkms.googleapis.com/v1/projects/p/locations/l/keyRings/r/cryptoKeys/k",
            "https://cloudkms.mtls.googleapis.com/v1/projects/p/locations/l/keyRings/r/cryptoKeys/k",
        ),
        (
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/sa@p.iam.gserviceaccount.com:generateIdToken",
            "https://iamcredentials.mtls.googleapis.com/v1/projects/-/serviceAccounts/sa@p.iam.gserviceaccount.com:generateIdToken",
        ),
        (
            "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/endpoints/123:rawPredict",
            "https://us-central1-aiplatform.mtls.googleapis.com/v1/projects/p/locations/us-central1/endpoints/123:rawPredict",
        ),
        (
            "https://x.googleapis.com/v1/resource?param=value#section",
            "https://x.mtls.googleapis.com/v1/resource?param=value#section",
        ),
        (
            "https://cloudkms.googleapis.com:8443/v1/test",
            "https://cloudkms.mtls.googleapis.com:8443/v1/test",
        ),
        (
            "https://user:secret@cloudkms.googleapis.com:443/v1/test",
            "https://user:secret@cloudkms.mtls.googleapis.com:443/v1/test",
        ),
        # Negative cases: already mTLS
        (
            "https://cloudkms.mtls.googleapis.com/v1/projects/p",
            "https://cloudkms.mtls.googleapis.com/v1/projects/p",
        ),
        (
            "https://modelarmor.us-central1.rep.mtls.googleapis.com/v1/templates/t",
            "https://modelarmor.us-central1.rep.mtls.googleapis.com/v1/templates/t",
        ),
        # PSC endpoints (*.p.googleapis.com or p.googleapis.com) are left unchanged
        (
            "https://foo.p.googleapis.com/a",
            "https://foo.p.googleapis.com/a",
        ),
        (
            "https://p.googleapis.com/a",
            "https://p.googleapis.com/a",
        ),
        # Sandbox endpoints: x.sandbox.googleapis.com -> x.mtls.sandbox.googleapis.com
        (
            "https://x.sandbox.googleapis.com/v1/test",
            "https://x.mtls.sandbox.googleapis.com/v1/test",
        ),
        (
            "https://x.mtls.sandbox.googleapis.com/v1/test",
            "https://x.mtls.sandbox.googleapis.com/v1/test",
        ),
        # Negative cases: other domains
        (
            "https://order-lookup-mcp-620485621180.us-central1.run.app/order",
            "https://order-lookup-mcp-620485621180.us-central1.run.app/order",
        ),
        (
            "https://12345.prediction.vertexai.goog/v1/endpoints/123:rawPredict",
            "https://12345.prediction.vertexai.goog/v1/endpoints/123:rawPredict",
        ),
        (
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts",
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts",
        ),
        (
            "https://googleapis.com/browse",
            "https://googleapis.com/browse",
        ),
        (
            "https://example.com/api",
            "https://example.com/api",
        ),
        (
            "",
            "",
        ),
    ]

    for input_url, expected_mtls in table_cases:
        # With mTLS True
        assert to_mtls_url(input_url, is_mtls=True) == expected_mtls, f"Failed mTLS rewrite for: {input_url}"
        # With mTLS False -> MUST be unchanged
        assert to_mtls_url(input_url, is_mtls=False) == input_url, f"Failed non-mTLS identity for: {input_url}"


def test_create_google_session_refresh_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_google_session passes max_refresh_attempts and refresh_status_codes to AuthorizedSession.

    # REQ-AUTH-006: F2 fix
    """
    fake_credentials = MagicMock(spec=google.auth.credentials.Credentials)
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (fake_credentials, "p"))
    monkeypatch.setattr(
        AuthorizedSession,
        "configure_mtls_channel",
        lambda self, client_cert_callback=None: None,
    )

    # Defaults: max_refresh_attempts=2, refresh_status_codes=(401,)
    s_default, _ = create_google_session()
    assert getattr(s_default, "_max_refresh_attempts", None) == 2
    assert getattr(s_default, "_refresh_status_codes", None) == (401,)

    # Custom: max_refresh_attempts=0
    s_custom, _ = create_google_session(max_refresh_attempts=0, refresh_status_codes=(401, 403))
    assert getattr(s_custom, "_max_refresh_attempts", None) == 0
    assert getattr(s_custom, "_refresh_status_codes", None) == (401, 403)


def test_create_google_session_realistic_cert_and_nocert(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Realistic test generating EC cert+key and certificate_config.json without mocking configure_mtls_channel.

    # REQ-AUTH-006: F6 fix
    """
    import datetime
    import json

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from google.auth.credentials import AnonymousCredentials
    from requests.adapters import HTTPAdapter

    # Generate self-signed EC key and cert
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    subject = issuer = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "workload")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cfg_file = tmp_path / "certificate_config.json"

    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(key_pem)
    cfg_file.write_text(
        json.dumps(
            {
                "cert_configs": {
                    "workload": {
                        "cert_path": str(cert_file),
                        "key_path": str(key_file),
                    }
                }
            }
        )
    )

    monkeypatch.setenv("GOOGLE_API_CERTIFICATE_CONFIG", str(cfg_file))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GOOGLE_API_USE_CLIENT_CERTIFICATE", raising=False)
    monkeypatch.setattr(google.auth, "default", lambda scopes=None, **kw: (AnonymousCredentials(), "p"))

    # 1. mTLS enabled via realistic cert configuration
    session, is_mtls = create_google_session()
    assert is_mtls is True
    adapter = session.adapters["https://"]
    assert type(adapter).__name__ == "_MutualTlsAdapter"
    assert getattr(adapter, "_pool_maxsize", None) == 20

    # 2. No-cert variant: assert is_mtls False and plain HTTPAdapter
    monkeypatch.delenv("GOOGLE_API_CERTIFICATE_CONFIG", raising=False)
    session_nocert, is_mtls_nocert = create_google_session()
    assert is_mtls_nocert is False
    adapter_nocert = session_nocert.adapters["https://"]
    assert type(adapter_nocert) is HTTPAdapter
    assert type(adapter_nocert).__name__ == "HTTPAdapter"
    assert getattr(adapter_nocert, "_pool_maxsize", None) == 20


def test_authorized_session_max_refresh_attempts_zero_single_post() -> None:
    """Real AuthorizedSession test proving max_refresh_attempts=0 yields exactly one POST on 401.

    # REQ-AUTH-006: F6 fix
    """
    import requests
    from google.auth.credentials import Credentials
    from requests.adapters import HTTPAdapter

    class TrackingCredentials(Credentials):
        def __init__(self) -> None:
            super().__init__()
            self.token = "initial-token"
            self.refresh_calls = 0

        def refresh(self, request: Any) -> None:
            self.refresh_calls += 1
            self.token = f"refreshed-{self.refresh_calls}"

    TrackingCredentials.__abstractmethods__ = frozenset()

    class Stub401Adapter(HTTPAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.post_count = 0

        def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
            self.post_count += 1
            resp = requests.Response()
            resp.status_code = 401
            resp.request = request
            resp._content = b'{"error": "unauthorized"}'
            return resp

    # max_refresh_attempts=0 -> exactly 1 POST, 0 refresh calls
    creds_zero = TrackingCredentials()
    session_zero = AuthorizedSession(creds_zero, max_refresh_attempts=0)
    stub_zero = Stub401Adapter()
    session_zero.mount("https://", stub_zero)
    session_zero.mount("http://", stub_zero)

    resp_zero = session_zero.post("https://example.com/api", json={"key": "val"})
    assert resp_zero.status_code == 401
    assert stub_zero.post_count == 1
    assert creds_zero.refresh_calls == 0

    # Comparative check: max_refresh_attempts=2 -> initial POST + 2 refreshes = 3 POSTs
    creds_two = TrackingCredentials()
    session_two = AuthorizedSession(creds_two, max_refresh_attempts=2)
    stub_two = Stub401Adapter()
    session_two.mount("https://", stub_two)
    session_two.mount("http://", stub_two)

    resp_two = session_two.post("https://example.com/api", json={"key": "val"})
    assert resp_two.status_code == 401
    assert stub_two.post_count == 3
    assert creds_two.refresh_calls == 2

