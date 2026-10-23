"""test_redact.py — redaction must catch PII but keep order IDs as [ORDER] (not [CARD])."""
import pytest
from prepare_twcs import redact, luhn_ok

CASES = {
    "@AmazonHelp the return delivery against order no.112-1234567-1234567 has not been picked up.": ("[ORDER]", "[CARD]"),
    "my card 4111 1111 1111 1111 was charged twice": ("[CARD]", "4111"),
    "call me at +1 (415) 555-0134 or mail jane.doe@example.com": ("[PHONE]", "jane.doe"),
    "Order #88213 still not here": ("[ORDER]", "88213"),
    "@115712 thanks! https://amzn.to/abc": ("@user", "amzn.to"),
    "it was supposed to arrive 2 days ago ^AB": ("arrive 2 days ago", "^AB"),
}


# REQ-ROUTER-S1-002
def test_redact():
    for text, (must, must_not) in CASES.items():
        out = redact(text)
        assert must in out and must_not not in out, (text, out)


# REQ-ROUTER-S1-002
def test_luhn_ok():
    assert luhn_ok("4111111111111111") is True
    assert luhn_ok("4111111111111112") is False
    assert luhn_ok("123") is False  # too short (< 13)


# REQ-ROUTER-S1-002
def test_redact_additional_cases():
    # Email redaction
    assert redact("support: test.user+tag@domain.co.uk") == "support: [EMAIL]"
    # Url redaction
    assert redact("visit http://example.com/path?arg=1 now") == "visit [URL] now"
    # Agent signature strip
    assert redact("We are on it -JL") == "We are on it"
    assert redact("Looking into this ^XYZ") == "Looking into this"
    # Whitespace cleanup
    assert redact("  hello   world  ") == "hello world"


if __name__ == "__main__":
    for t in CASES:
        print(redact(t))
    test_redact()
    print("redaction tests passed")
