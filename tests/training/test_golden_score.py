import pytest
from golden_score import norm, kappa


def test_norm_escalate():
    assert norm("escalate", "yes") == "true"
    assert norm("escalate", "YES") == "true"
    assert norm("escalate", "true") == "true"
    assert norm("escalate", "no") == "false"
    assert norm("escalate", "false") == "false"
    assert norm("escalate", "") is None
    assert norm("escalate", None) is None


def test_norm_urgency():
    assert norm("urgency", "3 - immediate") == "3"
    assert norm("urgency", "0 - can wait") == "0"
    assert norm("urgency", "2") == "2"
    assert norm("urgency", "") is None


def test_norm_other_questions():
    assert norm("sentiment", " ANGRY ") == "angry"
    assert norm("intent", "Order_Status") == "order_status"
    assert norm("sentiment", "") is None


def test_kappa_perfect_agreement():
    pairs = [("a", "a"), ("b", "b"), ("c", "c"), ("a", "a")]
    assert kappa(pairs) == 1.0


def test_kappa_chance_agreement():
    # Symmetric 50/50 distribution where observed agreement equals chance agreement
    pairs = [("a", "b"), ("b", "a")]
    # po = 0.0, pe = 0.5 -> kappa < 0
    k = kappa(pairs)
    assert k is not None
    assert k <= 0.0


def test_kappa_empty():
    assert kappa([]) is None
