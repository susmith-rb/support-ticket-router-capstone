import pytest
from training.router_s1.pipeline.evaluate import ece, macro_f1, probs, sweep


# REQ-ROUTER-S1-005
def test_ece_perfect_calibration():
    # If confidences equal accuracies in all bins, ECE is 0.0
    confs = [0.1, 0.1, 0.5, 0.5, 0.9, 0.9]
    # For bin ~0.1, 0/2 correct -> 0.0 accuracy, abs(0 - 0.1) = 0.1
    # For a perfectly calibrated set:
    # 10 cases with confidence 0.8 where exactly 8 are correct
    confs = [0.8] * 10
    correct = [1] * 8 + [0] * 2
    assert ece(confs, correct, bins=10) == pytest.approx(0.0)


# REQ-ROUTER-S1-005
def test_ece_poor_calibration():
    # Model is 100% confident on every prediction, but 0% are correct
    confs = [1.0] * 10
    correct = [0] * 10
    assert ece(confs, correct, bins=10) == pytest.approx(1.0)


# REQ-ROUTER-S1-005
def test_ece_empty():
    assert ece([], []) == 0.0


# REQ-ROUTER-S1-005
def test_macro_f1_perfect():
    y = ["a", "b", "c", "a"]
    p = ["a", "b", "c", "a"]
    labels = ["a", "b", "c"]
    assert macro_f1(y, p, labels) == pytest.approx(1.0)


# REQ-ROUTER-S1-005
def test_macro_f1_all_wrong():
    y = ["a", "a", "b", "b"]
    p = ["b", "b", "a", "a"]
    labels = ["a", "b"]
    assert macro_f1(y, p, labels) == pytest.approx(0.0)


# REQ-ROUTER-S1-005
def test_macro_f1_imbalanced():
    y = ["a", "a", "b"]
    p = ["a", "b", "b"]
    labels = ["a", "b"]
    # For 'a': TP=1, FP=0, FN=1 -> Prec=1.0, Rec=0.5 -> F1 = 2/3
    # For 'b': TP=1, FP=1, FN=0 -> Prec=0.5, Rec=1.0 -> F1 = 2/3
    # Macro F1 = 2/3
    assert macro_f1(y, p, labels) == pytest.approx(2 / 3)


# REQ-ROUTER-S1-006
def test_probs_probabilities_dict():
    ans = {"probabilities": {"optA": "0.7", "optB": 0.3}}
    res = probs(ans)
    assert res == {"optA": 0.7, "optB": 0.3}


# REQ-ROUTER-S1-006
def test_probs_noul():
    ans = {"noul": "0.85"}
    res = probs(ans)
    assert res["true"] == pytest.approx(0.85)
    assert res["false"] == pytest.approx(0.15)


# REQ-ROUTER-S1-006
def test_probs_empty():
    assert probs({}) == {}


# REQ-ROUTER-S1-006
def test_sweep():
    scores = [0.1, 0.4, 0.6, 0.9]
    pos = [False, False, True, True]
    res = sweep(scores, pos)

    # Should have 99 thresholds (0.01 to 0.99)
    assert len(res) == 99
    # At tau=0.5: scores >= 0.5 are 0.6, 0.9 (both pos). TP=2, FP=0.
    t50 = next(r for r in res if r["tau"] == 0.5)
    assert t50["recall"] == 1.0
    assert t50["false_escalation_rate"] == 0.0
    assert t50["escalated_share"] == 0.5

    # At tau=0.2: scores >= 0.2 are 0.4 (neg), 0.6 (pos), 0.9 (pos). TP=2, FP=1.
    t20 = next(r for r in res if r["tau"] == 0.2)
    assert t20["recall"] == 1.0
    assert t20["false_escalation_rate"] == 0.5  # 1 out of 2 negs
    assert t20["escalated_share"] == 0.75
