"""Tests for PII tokenization and reversible typed token mapping.

# REQ-STORE-009: Reversible PII Tokens and KMS Envelope-Encrypted Redaction Maps
"""

from __future__ import annotations

from ticket_router.security.pii_tokens import (
    WITHHELD_VALUE,
    tokenize_pii,
)


# REQ-STORE-009
def test_tokenize_is_reversible_and_withholds_cards():
    """Verify that tokenize_pii creates reversible tokens and strictly withholds card numbers.

    Acceptance test for REQ-STORE-009:
    1. Replaces SDP finding spans with [<INFO_TYPE>_<n>]
    2. Numbers per info_type in order of appearance
    3. Reuses token for repeated identical values
    4. CREDIT_CARD_NUMBER maps to '<withheld>' (never stores card digits)
    5. Non-card tokens are 100% reversible back to original text
    """
    card_number = "4111 2222 3333 4444"
    alice_email = "alice@example.com"
    bob_email = "bob@example.com"
    phone = "+1-555-0199"

    text = (
        f"Hello from {alice_email}, please call me at {phone}. "
        f"If I am not reachable, contact {bob_email} or {alice_email} again. "
        f"My payment card is {card_number}."
    )

    # Calculate exact codepoint spans
    findings = [
        # Alice 1st
        {
            "infoType": "EMAIL_ADDRESS",
            "location": {"codepointRange": {"start": text.index(alice_email), "end": text.index(alice_email) + len(alice_email)}},
        },
        # Phone
        {
            "infoType": "PHONE_NUMBER",
            "location": {"codepointRange": {"start": text.index(phone), "end": text.index(phone) + len(phone)}},
        },
        # Bob
        {
            "infoType": "EMAIL_ADDRESS",
            "location": {"codepointRange": {"start": text.index(bob_email), "end": text.index(bob_email) + len(bob_email)}},
        },
        # Alice 2nd (repeated)
        {
            "infoType": "EMAIL_ADDRESS",
            "location": {"codepointRange": {"start": text.rindex(alice_email), "end": text.rindex(alice_email) + len(alice_email)}},
        },
        # Card number
        {
            "infoType": "CREDIT_CARD_NUMBER",
            "location": {"codepointRange": {"start": text.index(card_number), "end": text.index(card_number) + len(card_number)}},
        },
    ]

    tokenized_text, mapping = tokenize_pii(text, findings)

    # 1. Check tokenized string structure
    assert "[EMAIL_ADDRESS_1]" in tokenized_text
    assert "[EMAIL_ADDRESS_2]" in tokenized_text
    assert "[PHONE_NUMBER_1]" in tokenized_text
    assert "[CREDIT_CARD_NUMBER_1]" in tokenized_text

    # Repeated Alice should use the same token [EMAIL_ADDRESS_1]
    assert tokenized_text.count("[EMAIL_ADDRESS_1]") == 2
    assert tokenized_text.count("[EMAIL_ADDRESS_2]") == 1

    # 2. Check mapping contracts
    assert mapping["[EMAIL_ADDRESS_1]"] == alice_email
    assert mapping["[EMAIL_ADDRESS_2]"] == bob_email
    assert mapping["[PHONE_NUMBER_1]"] == phone

    # PCI Invariant: Card digits must NEVER appear in the mapping
    assert mapping["[CREDIT_CARD_NUMBER_1]"] == WITHHELD_VALUE
    assert card_number not in mapping.values()
    assert card_number not in str(mapping)

    # 3. Check reversibility for non-withheld tokens
    reconstructed = tokenized_text
    for token, val in mapping.items():
        if val != WITHHELD_VALUE:
            reconstructed = reconstructed.replace(token, val)

    # Everything except the card number should match the original text
    expected_non_card = text.replace(card_number, "[CREDIT_CARD_NUMBER_1]")
    assert reconstructed == expected_non_card


# REQ-STORE-009
def test_tokenize_edge_cases():
    """Verify empty text, empty findings, and malformed codepoint ranges."""
    assert tokenize_pii("", []) == ("", {})
    assert tokenize_pii("Normal text", []) == ("Normal text", {})

    # Malformed / out of bounds spans ignored safely
    malformed = [
        {"infoType": "EMAIL_ADDRESS", "location": {"codepointRange": {"start": -5, "end": 10}}},
        {"infoType": "EMAIL_ADDRESS", "location": {"codepointRange": {"start": 10, "end": 5}}},
        {"infoType": "EMAIL_ADDRESS", "location": {"codepointRange": {"start": 5, "end": 9999}}},
        {"infoType": "EMAIL_ADDRESS", "location": "not-a-dict"},
    ]
    txt = "Short text"
    res_txt, res_map = tokenize_pii(txt, malformed)
    assert res_txt == txt
    assert res_map == {}
