"""Regression: live Model Armor advanced-SDP response must drive reversible tokenization.

# REQ-STORE-009
# REQ-MA-001

The live response (captured 2026-10-30 from template support-router-lean with an advanced
DLP inspect template) once produced zero tokens because SdpFinding.location was stringified.
"""

import json
from pathlib import Path

from ticket_router.agents.triage_router.policy import _extract_sdp_findings
from ticket_router.security.model_armor import parse_model_armor_response
from ticket_router.security.pii_tokens import tokenize_pii

FIXTURE = Path(__file__).parent / "fixtures" / "ma_advanced_sdp_response.json"
TEXT = "Reach me at jane.verify@example.com, phone 415-555-0100, SSN 123-45-6789, card 4111 1111 1111 1111"


# REQ-STORE-009
def test_live_advanced_sdp_response_tokenizes_email_phone_and_card() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    verdict = parse_model_armor_response(data, latency_ms=1.0, original_text=TEXT)
    assert all(isinstance(f.location, dict) for f in verdict.sdp_findings)

    tokenized, mapping = tokenize_pii(TEXT, _extract_sdp_findings(verdict))

    assert "jane.verify@example.com" not in tokenized
    assert "[EMAIL_ADDRESS_1]" in tokenized
    assert "[PHONE_NUMBER_1]" in tokenized
    assert "[CREDIT_CARD_NUMBER_1]" in tokenized
    assert mapping["[EMAIL_ADDRESS_1]"] == "jane.verify@example.com"
    assert mapping["[CREDIT_CARD_NUMBER_1]"] == "<withheld>"
