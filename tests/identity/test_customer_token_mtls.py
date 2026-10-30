"""Hermetic tests for KMS signer and public key resolver mTLS endpoint rewriting.

# REQ-AUTH-006: mTLS-Aware Google API Transports (certificate-bound agent identity tokens)
"""

from __future__ import annotations

import base64
import pickle
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from ticket_router.identity.customer_token import KmsPublicKeyResolver, KmsSigner

KID = "projects/test-proj/locations/global/keyRings/kr/cryptoKeys/k/cryptoKeyVersions/1"


def _make_dummy_ec_key() -> tuple[ec.EllipticCurvePrivateKey, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_key, pem


def _make_kms_sign_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    # DER signature of 2 32-byte integers
    der = encode_dss_signature(123456789, 987654321)
    resp.json.return_value = {"signature": base64.b64encode(der).decode("ascii")}
    return resp


def _make_kms_pubkey_response(pem: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"pem": pem}
    return resp


def test_kms_signer_mtls_on_rewrites_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """When mTLS is on and no session injected, KmsSigner calls cloudkms.mtls.googleapis.com.

    # REQ-AUTH-006
    """
    mock_session = MagicMock()
    mock_session.is_mtls = True
    mock_session.post.return_value = _make_kms_sign_response()

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        lambda *args, **kwargs: (mock_session, True),
    )

    signer = KmsSigner(KID)
    sig = signer.sign(b"message-to-sign")
    assert len(sig) == 64

    assert mock_session.post.call_count == 1
    call_args, _ = mock_session.post.call_args
    url = call_args[0]
    assert url == f"https://cloudkms.mtls.googleapis.com/v1/{KID}:asymmetricSign"


def test_kms_signer_mtls_off_keeps_plain_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """When mTLS is off and no session injected, KmsSigner calls cloudkms.googleapis.com.

    # REQ-AUTH-006
    """
    mock_session = MagicMock()
    mock_session.is_mtls = False
    mock_session.post.return_value = _make_kms_sign_response()

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        lambda *args, **kwargs: (mock_session, False),
    )

    signer = KmsSigner(KID)
    sig = signer.sign(b"message-to-sign")
    assert len(sig) == 64

    call_args, _ = mock_session.post.call_args
    url = call_args[0]
    assert url == f"https://cloudkms.googleapis.com/v1/{KID}:asymmetricSign"


def test_kms_signer_injected_session() -> None:
    """Injected session is respected and rewrites URL only when session.is_mtls is True.

    # REQ-AUTH-006
    """
    # 1. is_mtls = False
    fake_sess_plain = MagicMock()
    fake_sess_plain.is_mtls = False
    fake_sess_plain.post.return_value = _make_kms_sign_response()

    signer_plain = KmsSigner(KID, session=fake_sess_plain)
    signer_plain.sign(b"test")
    assert fake_sess_plain.post.call_args[0][0] == f"https://cloudkms.googleapis.com/v1/{KID}:asymmetricSign"

    # 2. is_mtls = True
    fake_sess_mtls = MagicMock()
    fake_sess_mtls.is_mtls = True
    fake_sess_mtls.post.return_value = _make_kms_sign_response()

    signer_mtls = KmsSigner(KID, session=fake_sess_mtls)
    signer_mtls.sign(b"test")
    assert fake_sess_mtls.post.call_args[0][0] == f"https://cloudkms.mtls.googleapis.com/v1/{KID}:asymmetricSign"


def test_kms_signer_pickle_preserves_lazy_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pickling KmsSigner drops session and recreates lazily upon unpickling.

    # REQ-AUTH-006
    """
    mock_session = MagicMock()
    mock_session.is_mtls = True
    mock_session.post.return_value = _make_kms_sign_response()

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        lambda *args, **kwargs: (mock_session, True),
    )

    signer = KmsSigner(KID)
    signer.sign(b"msg1")
    assert signer._session is not None

    dumped = pickle.dumps(signer)
    unpickled: KmsSigner = pickle.loads(dumped)
    assert unpickled.kid == KID
    assert unpickled._session is None
    assert unpickled._is_mtls is False

    sig = unpickled.sign(b"msg2")
    assert len(sig) == 64
    assert unpickled._session is not None
    assert unpickled._is_mtls is True


def test_kms_resolver_mtls_on_and_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """KmsPublicKeyResolver rewrites to .mtls when is_mtls is True, plain when False.

    # REQ-AUTH-006
    """
    _, pem = _make_dummy_ec_key()

    # mTLS on
    mock_session_mtls = MagicMock()
    mock_session_mtls.is_mtls = True
    mock_session_mtls.get.return_value = _make_kms_pubkey_response(pem)

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        lambda *args, **kwargs: (mock_session_mtls, True),
    )

    resolver_mtls = KmsPublicKeyResolver()
    key_mtls = resolver_mtls.get(KID)
    assert isinstance(key_mtls, ec.EllipticCurvePublicKey)
    assert mock_session_mtls.get.call_args[0][0] == f"https://cloudkms.mtls.googleapis.com/v1/{KID}/publicKey"

    # mTLS off
    mock_session_plain = MagicMock()
    mock_session_plain.is_mtls = False
    mock_session_plain.get.return_value = _make_kms_pubkey_response(pem)

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        lambda *args, **kwargs: (mock_session_plain, False),
    )

    resolver_plain = KmsPublicKeyResolver()
    key_plain = resolver_plain.get(KID)
    assert isinstance(key_plain, ec.EllipticCurvePublicKey)
    assert mock_session_plain.get.call_args[0][0] == f"https://cloudkms.googleapis.com/v1/{KID}/publicKey"


def test_kms_resolver_pickle_preserves_lazy_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pickling KmsPublicKeyResolver drops session and recreates lazily upon unpickling.

    # REQ-AUTH-006
    """
    _, pem = _make_dummy_ec_key()
    mock_session = MagicMock()
    mock_session.is_mtls = True
    mock_session.get.return_value = _make_kms_pubkey_response(pem)

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        lambda *args, **kwargs: (mock_session, True),
    )

    resolver = KmsPublicKeyResolver(cache_ttl_s=1800)
    resolver.get(KID)
    assert resolver._session is not None

    dumped = pickle.dumps(resolver)
    unpickled: KmsPublicKeyResolver = pickle.loads(dumped)
    assert unpickled.cache_ttl_s == 1800
    assert unpickled._session is None
    assert unpickled._is_mtls is False

    key = unpickled.get(KID)
    assert isinstance(key, ec.EllipticCurvePublicKey)
    assert unpickled._session is not None
    assert unpickled._is_mtls is True


def test_kms_signer_and_resolver_use_max_refresh_attempts_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KmsSigner and KmsPublicKeyResolver call create_google_session with max_refresh_attempts=1.

    # REQ-AUTH-006: F2 fix
    """
    captured_kwargs = []

    def mock_create(*args, **kwargs):
        captured_kwargs.append(kwargs)
        mock_sess = MagicMock()
        mock_sess.is_mtls = False
        mock_sess.post.return_value = _make_kms_sign_response()
        _, pem = _make_dummy_ec_key()
        mock_sess.get.return_value = _make_kms_pubkey_response(pem)
        return mock_sess, False

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        mock_create,
    )

    signer = KmsSigner(KID)
    signer.sign(b"test")
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0].get("max_refresh_attempts") == 1

    resolver = KmsPublicKeyResolver()
    resolver.get(KID)
    assert len(captured_kwargs) == 2
    assert captured_kwargs[1].get("max_refresh_attempts") == 1

