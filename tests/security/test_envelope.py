"""Tests for KMS envelope encryption of PII redaction maps.

# REQ-STORE-009: Reversible PII Tokens and KMS Envelope-Encrypted Redaction Maps
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, Optional

import pytest
from cryptography.exceptions import InvalidTag

from ticket_router.security.envelope import (
    KEY_ID_ENVELOPE_V1,
    EnvelopeCipher,
    RedactionSealer,
)


class MockKmsResponse:
    """Mock HTTP response for Cloud KMS REST API calls."""

    def __init__(self, data: Dict[str, Any], status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def json(self) -> Dict[str, Any]:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"KMS HTTP error: {self.status_code}")


class FakeKmsSession:
    """Hermetic in-memory mock simulating Cloud KMS symmetric encrypt/decrypt endpoints."""

    def __init__(self) -> None:
        self.encrypt_calls: list[Dict[str, Any]] = []
        self.decrypt_calls: list[Dict[str, Any]] = []

    def post(
        self,
        url: str,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 10.0,
    ) -> MockKmsResponse:
        payload = json or {}
        if url.endswith(":encrypt"):
            self.encrypt_calls.append({"url": url, "payload": payload})
            pt_b64 = payload.get("plaintext", "")
            raw_dek = base64.b64decode(pt_b64)
            # Wrap DEK by tagging
            wrapped = b"KMS_MOCK_WRAPPED:" + raw_dek
            return MockKmsResponse({
                "ciphertext": base64.b64encode(wrapped).decode("ascii"),
                "name": f"{url[:-8]}/cryptoKeyVersions/1",
            })
        if url.endswith(":decrypt"):
            self.decrypt_calls.append({"url": url, "payload": payload})
            ct_b64 = payload.get("ciphertext", "")
            wrapped = base64.b64decode(ct_b64)
            if not wrapped.startswith(b"KMS_MOCK_WRAPPED:"):
                return MockKmsResponse({"error": "Invalid ciphertext"}, status_code=400)
            raw_dek = wrapped[len(b"KMS_MOCK_WRAPPED:"):]
            return MockKmsResponse({
                "plaintext": base64.b64encode(raw_dek).decode("ascii"),
            })
        return MockKmsResponse({"error": "Not found"}, status_code=404)


# REQ-STORE-009
def test_envelope_roundtrip_and_aad_binding():
    """Verify AES-256-GCM envelope roundtrip, AAD binding, and tamper resistance.

    Acceptance test for REQ-STORE-009:
    1. Generates 256-bit DEK, encrypts with AES-256-GCM and AAD = ticket_id.
    2. Wraps DEK with KMS :encrypt and recovers via KMS :decrypt.
    3. Rejects decryption with InvalidTag if AAD does not match ticket_id.
    4. Rejects tampered ciphertext.
    """
    key_name = "projects/susmith-fde-ticket-router/locations/us-central1/keyRings/ticket-router-identity/cryptoKeys/redaction-map-wrapper"
    fake_session = FakeKmsSession()
    cipher = EnvelopeCipher(kms_key_name=key_name, session=fake_session)

    ticket_id = "ticket-uuid-1010"
    aad = ticket_id.encode("utf-8")
    mapping = {
        "[EMAIL_ADDRESS_1]": "alice@customer.example",
        "[PHONE_NUMBER_1]": "+1-555-0123",
    }
    plaintext = json.dumps(mapping).encode("utf-8")

    # 1. Encrypt
    blob = cipher.encrypt(plaintext, aad=aad)
    assert isinstance(blob, bytes)
    assert len(fake_session.encrypt_calls) == 1

    # Verify envelope structure
    envelope_json = base64.b64decode(blob)
    envelope = json.loads(envelope_json.decode("utf-8"))
    assert envelope["v"] == 1
    assert envelope["kid"] == key_name
    assert "wdek" in envelope
    assert "nonce" in envelope
    assert "ct" in envelope

    # Plaintext mapping must NOT exist anywhere in the serialized envelope
    assert "alice@customer.example" not in envelope_json.decode("utf-8")

    # 2. Decrypt with correct AAD
    decrypted = cipher.decrypt(blob, aad=aad)
    assert decrypted == plaintext
    assert json.loads(decrypted.decode("utf-8")) == mapping
    assert len(fake_session.decrypt_calls) == 1

    # 3. AAD Binding: Decrypt with mismatched AAD MUST fail with InvalidTag
    wrong_aad = b"ticket-uuid-different"
    with pytest.raises(InvalidTag):
        cipher.decrypt(blob, aad=wrong_aad)

    # 4. Tampered ciphertext MUST fail with InvalidTag
    tampered_envelope = dict(envelope)
    ct_bytes = bytearray(base64.b64decode(envelope["ct"]))
    ct_bytes[0] ^= 0xFF  # Flip bits in ciphertext
    tampered_envelope["ct"] = base64.b64encode(ct_bytes).decode("ascii")
    tampered_blob = base64.b64encode(json.dumps(tampered_envelope).encode("utf-8"))

    with pytest.raises(InvalidTag):
        cipher.decrypt(tampered_blob, aad=aad)


# REQ-STORE-009
def test_redaction_sealer_helper():
    """Verify RedactionSealer helper correctly encodes mappings and key_id."""
    key_name = "projects/susmith-fde-ticket-router/locations/us-central1/keyRings/ticket-router-identity/cryptoKeys/redaction-map-wrapper"
    fake_session = FakeKmsSession()
    cipher = EnvelopeCipher(kms_key_name=key_name, session=fake_session)
    sealer = RedactionSealer(cipher=cipher)

    ticket_id = "ticket-sealer-202"
    mapping = {"[EMAIL_1]": "dr.vance@example.org"}

    envelope_b64, key_id = sealer.seal(ticket_id=ticket_id, mapping=mapping)
    assert key_id == KEY_ID_ENVELOPE_V1
    assert isinstance(envelope_b64, str)

    # Decrypt with cipher
    recovered_bytes = cipher.decrypt(envelope_b64.encode("ascii"), aad=ticket_id.encode("utf-8"))
    assert json.loads(recovered_bytes.decode("utf-8")) == mapping


# REQ-STORE-009
def test_envelope_zero_plaintext_logging(caplog):
    """Verify that plaintext secrets and raw DEKs are never emitted to logs."""
    caplog.set_level(logging.DEBUG)
    key_name = "projects/test/locations/us-central1/keyRings/test/cryptoKeys/redaction-map-wrapper"
    fake_session = FakeKmsSession()
    cipher = EnvelopeCipher(kms_key_name=key_name, session=fake_session)

    secret_email = "supersecret_topsecret_email@bank.corp"
    plaintext = json.dumps({"[EMAIL_1]": secret_email}).encode("utf-8")
    aad = b"ticket-audit-logging-test"

    blob = cipher.encrypt(plaintext, aad=aad)
    cipher.decrypt(blob, aad=aad)

    # Assert secret string is not in any captured log records
    for record in caplog.records:
        assert secret_email not in record.getMessage()
