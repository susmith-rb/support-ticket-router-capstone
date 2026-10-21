"""test_redact.py — redaction must catch PII but keep order IDs as [ORDER] (not [CARD])."""
from prepare_twcs import redact

CASES = {
    "@AmazonHelp the return delivery against order no.112-1234567-1234567 has not been picked up.": ("[ORDER]", "[CARD]"),
    "my card 4111 1111 1111 1111 was charged twice": ("[CARD]", "4111"),
    "call me at +1 (415) 555-0134 or mail jane.doe@example.com": ("[PHONE]", "jane.doe"),
    "Order #88213 still not here": ("[ORDER]", "88213"),
    "@115712 thanks! https://amzn.to/abc": ("@user", "amzn.to"),
    "it was supposed to arrive 2 days ago ^AB": ("arrive 2 days ago", "^AB"),
}

def test_redact():
    for text, (must, must_not) in CASES.items():
        out = redact(text)
        assert must in out and must_not not in out, (text, out)

if __name__ == "__main__":
    for t in CASES: print(redact(t))
    test_redact(); print("redaction tests passed")
