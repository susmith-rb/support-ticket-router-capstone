import pytest
from label_teacher import to_gold


def test_to_gold_insufficient_votes():
    # Less than 2 successful votes returns None
    assert to_gold([]) is None
    assert to_gold([{"error": "rate limit"}]) is None
    assert to_gold([{"escalate": True, "sentiment": "angry", "urgency": 3, "intent": "order_status"}]) is None
    assert to_gold([
        {"escalate": True, "sentiment": "angry", "urgency": 3, "intent": "order_status"},
        {"error": "timeout"},
    ]) is None


def test_to_gold_unanimous_votes():
    votes = [
        {"escalate": True, "sentiment": "angry", "urgency": 3, "intent": "order_status"},
        {"escalate": True, "sentiment": "angry", "urgency": 3, "intent": "order_status"},
        {"escalate": True, "sentiment": "angry", "urgency": 3, "intent": "order_status"},
    ]
    gold, n = to_gold(votes)
    assert n == 3
    assert gold["escalate"]["probabilities"]["true"] == 1.0
    assert gold["escalate"]["probabilities"]["false"] == 0.0
    assert gold["sentiment"]["probabilities"]["angry"] == 1.0
    assert gold["sentiment"]["probabilities"]["neutral"] == 0.0
    assert gold["urgency"]["probabilities"]["3"] == 1.0
    assert gold["urgency"]["probabilities"]["0"] == 0.0
    assert gold["intent"]["probabilities"]["order_status"] == 1.0
    assert gold["intent"]["probabilities"]["other"] == 0.0


def test_to_gold_split_votes():
    votes = [
        {"escalate": True, "sentiment": "angry", "urgency": 2, "intent": "order_status"},
        {"escalate": False, "sentiment": "frustrated", "urgency": 1, "intent": "shipping_policy"},
        {"escalate": True, "sentiment": "angry", "urgency": 2, "intent": "order_status"},
    ]
    gold, n = to_gold(votes)
    assert n == 3
    assert gold["escalate"]["probabilities"]["true"] == pytest.approx(2 / 3)
    assert gold["escalate"]["probabilities"]["false"] == pytest.approx(1 / 3)
    assert gold["sentiment"]["probabilities"]["angry"] == pytest.approx(2 / 3)
    assert gold["sentiment"]["probabilities"]["frustrated"] == pytest.approx(1 / 3)
    assert gold["urgency"]["probabilities"]["2"] == pytest.approx(2 / 3)
    assert gold["urgency"]["probabilities"]["1"] == pytest.approx(1 / 3)
    assert gold["intent"]["probabilities"]["order_status"] == pytest.approx(2 / 3)
    assert gold["intent"]["probabilities"]["shipping_policy"] == pytest.approx(1 / 3)
