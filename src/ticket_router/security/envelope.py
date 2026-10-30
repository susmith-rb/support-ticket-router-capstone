"""KMS envelope encryption for PII redaction maps.

# REQ-STORE-009: Reversible PII Tokens and KMS Envelope-Encrypted Redaction Maps
# REQ-AUTH-006: mTLS-Aware Google API Transports (certificate-bound agent identity tokens)
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ticket_router.common.google_http import create_google_session, to_mtls_url

logger = logging.getLogger(__name__)

DEFAULT_KMS_ENDPOINT = "https://cloudkms.googleapis.com/v1"
KEY_ID_ENVELOPE_V1 = "kms-envelope-v1"


class EnvelopeCipher:
    """Envelope cipher using Cloud KMS to wrap local AES-256-GCM data encryption keys (DEK).

    # REQ-STORE-009:
    # 1. Generates random 256-bit DEK per map.
    # 2. Encrypts plaintext mapping with AES-256-GCM (random 96-bit nonce, AAD = ticket_id).
    # 3. Wraps DEK via Cloud KMS :encrypt symmetric REST endpoint using mTLS session (REQ-AUTH-006).
    # 4. Formats envelope JSON: {v:1, kid:<cryptoKey name>, wdek:<b64>, nonce:<b64>, ct:<b64>}.
    # 5. Returns base64-encoded envelope blob. Plaintext DEKs and mappings are never logged.
    """

    def __init__(
        self,
        kms_key_name: str,
        session: Optional[Any] = None,
        kms_endpoint: Optional[str] = None,
    ) -> None:
        self.kms_key_name = kms_key_name
        self.kms_endpoint = (kms_endpoint or DEFAULT_KMS_ENDPOINT).rstrip("/")
        self._injected_session = session

    def _get_session_and_url(self, action: str, key_name: Optional[str] = None) -> Tuple[Any, str]:
        target_key = key_name or self.kms_key_name
        raw_url = f"{self.kms_endpoint}/{target_key}:{action}"
        if self._injected_session is not None:
            return self._injected_session, raw_url

        session, is_mtls = create_google_session()
        url = to_mtls_url(raw_url, is_mtls)
        return session, url

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        """Encrypt plaintext with AES-256-GCM and wrap the DEK with Cloud KMS.

        Returns base64-encoded JSON envelope bytes.
        """
        # Generate 256-bit DEK and 96-bit nonce
        dek = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)

        try:
            # Encrypt locally with AES-256-GCM
            aesgcm = AESGCM(dek)
            ct = aesgcm.encrypt(nonce, plaintext, aad)

            # Wrap DEK via Cloud KMS REST API
            session, url = self._get_session_and_url("encrypt")
            dek_b64 = base64.b64encode(dek).decode("ascii")

            resp = session.post(
                url,
                json={"plaintext": dek_b64},
                headers={"Content-Type": "application/json"},
                timeout=10.0,
            )
            resp.raise_for_status()
            res_data = resp.json()
            wrapped_dek_b64 = res_data.get("ciphertext")
            if not wrapped_dek_b64:
                raise ValueError("Cloud KMS :encrypt response missing ciphertext field")

            envelope: Dict[str, Any] = {
                "v": 1,
                "kid": self.kms_key_name,
                "wdek": wrapped_dek_b64,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ct": base64.b64encode(ct).decode("ascii"),
            }

            envelope_json = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
            return base64.b64encode(envelope_json)
        finally:
            # Ensure DEK does not linger in local scope
            del dek

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        """Unwrap DEK with Cloud KMS and decrypt AES-256-GCM ciphertext.

        Raises InvalidTag if ciphertext or AAD has been tampered with.
        """
        if not blob:
            raise ValueError("Cannot decrypt empty envelope blob")

        # Decode base64 if needed, then parse JSON
        try:
            # First attempt base64 decode
            raw_json = base64.b64decode(blob)
            envelope = json.loads(raw_json.decode("utf-8"))
        except Exception:
            try:
                envelope = json.loads(blob.decode("utf-8"))
            except Exception as err:
                raise ValueError("Invalid envelope format: not valid base64 or JSON") from err

        if not isinstance(envelope, dict):
            raise ValueError("Invalid envelope payload structure")

        v = envelope.get("v")
        if v != 1:
            raise ValueError(f"Unsupported envelope version: {v}")

        kid = envelope.get("kid") or self.kms_key_name
        wdek = envelope.get("wdek")
        nonce_b64 = envelope.get("nonce")
        ct_b64 = envelope.get("ct")

        if not wdek or not nonce_b64 or not ct_b64:
            raise ValueError("Envelope missing required crypto fields (wdek, nonce, ct)")

        nonce = base64.b64decode(nonce_b64)
        ct = base64.b64decode(ct_b64)

        # Unwrap DEK via Cloud KMS REST API
        session, url = self._get_session_and_url("decrypt", key_name=kid)
        resp = session.post(
            url,
            json={"ciphertext": wdek},
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
        resp.raise_for_status()
        res_data = resp.json()
        dek_b64 = res_data.get("plaintext")
        if not dek_b64:
            raise ValueError("Cloud KMS :decrypt response missing plaintext field")

        dek = base64.b64decode(dek_b64)
        try:
            aesgcm = AESGCM(dek)
            plaintext = aesgcm.decrypt(nonce, ct, aad)
            return plaintext
        finally:
            del dek


class RedactionSealer:
    """Helper to seal token mappings into base64 KMS envelopes for ticket persistence."""

    def __init__(
        self,
        cipher: Optional[EnvelopeCipher] = None,
        kms_key_name: Optional[str] = None,
        session: Optional[Any] = None,
        kms_endpoint: Optional[str] = None,
    ) -> None:
        if cipher is not None:
            self.cipher = cipher
        else:
            key_name = kms_key_name or os.environ.get("REDACTION_KMS_KEY", "")
            self.cipher = EnvelopeCipher(
                kms_key_name=key_name,
                session=session,
                kms_endpoint=kms_endpoint,
            )

    def seal(self, ticket_id: str, mapping: Dict[str, str]) -> Tuple[str, str]:
        """Seal mapping for ticket_id into (envelope_b64, key_id).

        Returns (envelope_b64, 'kms-envelope-v1').
        """
        plaintext = json.dumps(mapping, separators=(",", ":")).encode("utf-8")
        aad = ticket_id.encode("utf-8")
        blob = self.cipher.encrypt(plaintext, aad=aad)
        envelope_b64 = blob.decode("ascii") if isinstance(blob, bytes) else str(blob)
        return envelope_b64, KEY_ID_ENVELOPE_V1
