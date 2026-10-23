import pytest
from simulate_system import HARD, NEG, REPEAT, DEADLINE, non_english, shouting, tier0, p_esc, run


def test_hard_regexes():
    assert HARD.search("I am calling my lawyer right now") is not None
    assert HARD.search("This is fraud and unauthorized charge") is not None
    assert HARD.search("I will contact the police") is not None
    assert HARD.search("There was a data breach and privacy issue") is not None
    assert HARD.search("Just checking if the color is blue") is None


def test_soft_regexes():
    assert NEG.search("This is the worst and most pathetic service") is not None
    assert REPEAT.search("I have asked 3rd time for my refund") is not None
    assert DEADLINE.search("I need this delivered immediately today") is not None
    assert NEG.search("Thank you so much") is None


def test_shouting():
    # 2 or more capitalized words >= 3 chars not in CAPS_OK
    assert shouting("WHERE IS MY PACKAGE") is True
    assert shouting("This is UNACCEPTABLE NOW") is True
    # Only 1 capitalized word
    assert shouting("Please HELP me") is False
    # Words in CAPS_OK (e.g. AMAZON, PRIME, USA, URL, ORDER) don't count
    assert shouting("My AMAZON PRIME order") is False


def test_non_english():
    # Pure non-ASCII / foreign script
    assert non_english("私の荷物はどこですか？") is True
    # Standard English
    assert non_english("Where is my order? It was supposed to arrive yesterday.") is False


def test_tier0():
    hard, soft = tier0("I will sue you for fraud, this is the worst experience ever")
    assert hard is True
    assert "negative" in soft

    hard, soft = tier0("I am asking for the 3rd time, need it today!")
    assert hard is False
    assert "repeat" in soft
    assert "deadline" in soft


def test_p_esc():
    p = {
        "escalate": {"true": 0.4},
        "sentiment": {"angry": 0.8},
        "urgency": {"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.9},
    }
    # Max of (0.4, 0.8, 0.9) is 0.9
    assert p_esc(p) == 0.9


def test_run_routing():
    rows = [
        # 1. Hard escalation -> human_hard
        {
            "text": "fraud and theft",
            "_t0": (True, []),
            "_pesc": 0.1,
            "expected": {"escalate": True},
            "p": {"intent": {"order_status": 1.0}},
            "gold_p_esc": 1.0,
        },
        # 2. Tier 1 high score >= tau_esc (0.5) -> human_tier1
        {
            "text": "angry complaint",
            "_t0": (False, []),
            "_pesc": 0.7,
            "expected": {"escalate": True},
            "p": {"intent": {"order_status": 1.0}},
            "gold_p_esc": 1.0,
        },
        # 3. In band (tau_low <= pesc < tau_esc) -> tier2
        {
            "text": "moderate issue",
            "_t0": (False, []),
            "_pesc": 0.3,
            "expected": {"escalate": True},
            "p": {"intent": {"order_status": 1.0}},
            "gold_p_esc": 0.5,
        },
        # 4. Soft rule hit -> tier2
        {
            "text": "deadline today",
            "_t0": (False, ["deadline"]),
            "_pesc": 0.05,
            "expected": {"escalate": True},
            "p": {"intent": {"order_status": 1.0}},
            "gold_p_esc": 0.5,
        },
        # 5. Low score, no rules -> auto
        {
            "text": "routine question",
            "_t0": (False, []),
            "_pesc": 0.05,
            "expected": {"escalate": False},
            "p": {"intent": {"order_status": 1.0}},
            "gold_p_esc": 0.0,
        },
    ]

    res = run(rows, tau_low=0.15, tau_esc=0.5, variant="band_only")
    assert res["n"] == 5
    assert res["positives"] == 4
    assert res["share"]["human_hard"] == 0.2
    assert res["share"]["human_tier1"] == 0.2
    assert res["share"]["tier2"] == 0.4
    assert res["share"]["auto"] == 0.2
    # All 4 positives were caught (1 hard, 1 tier1, 2 tier2)
    assert res["system_recall_bound"] == 1.0
    assert len(res["missed"]) == 0
