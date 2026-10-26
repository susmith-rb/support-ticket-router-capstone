import pytest
from training.router_s1.pipeline.strict_recall import RULESETS, run, strict


# REQ-ROUTER-S1-010
def test_strict_condition():
    assert strict({"expected": {"sentiment": "angry", "urgency": 0}}) is True
    assert strict({"expected": {"sentiment": "neutral", "urgency": "3"}}) is True
    assert strict({"expected": {"sentiment": "frustrated", "urgency": 2}}) is False
    assert strict({"expected": {"sentiment": "neutral", "urgency": 1}}) is False


# REQ-ROUTER-S1-010
def test_rulesets_structure():
    assert "none" in RULESETS
    assert "lean" in RULESETS
    assert "lean+deadline" in RULESETS
    assert "full" in RULESETS

    assert RULESETS["none"] == set()
    assert "shouting" in RULESETS["lean"]
    assert "deadline" in RULESETS["lean+deadline"]
    assert "non_english" in RULESETS["full"]


# REQ-ROUTER-S1-010
def test_strict_run():
    rows = [
        # Hard hit
        {"_t0": (True, []), "_pesc": 0.1, "expected": {"sentiment": "angry", "urgency": 0}, "text": "fraud legal"},
        # Score >= tau_esc
        {"_t0": (False, []), "_pesc": 0.6, "expected": {"sentiment": "angry", "urgency": 0}, "text": "high score"},
        # Soft hit matching soft_used
        {"_t0": (False, ["deadline"]), "_pesc": 0.1, "expected": {"sentiment": "neutral", "urgency": 3}, "text": "deadline"},
        # Miss (not hard, pesc < tau_low, soft not in soft_used)
        {"_t0": (False, ["other"]), "_pesc": 0.1, "expected": {"sentiment": "angry", "urgency": 0}, "text": "missed angry"},
        # Negative case (not strict)
        {"_t0": (False, []), "_pesc": 0.05, "expected": {"sentiment": "neutral", "urgency": 0}, "text": "ok"},
    ]

    res = run(rows, tau_low=0.2, tau_esc=0.5, soft_used={"deadline"})
    assert res["P"] == 4
    # 3 caught out of 4 -> recall 0.75
    assert res["recall_bound"] == pytest.approx(0.75)
    assert res["human"] == pytest.approx(2 / 5)
    assert res["tier2"] == pytest.approx(1 / 5)
    assert res["auto"] == pytest.approx(2 / 5)
    assert len(res["missed"]) == 1
