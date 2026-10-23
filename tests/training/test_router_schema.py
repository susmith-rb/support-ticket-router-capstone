import pytest
from router_schema import QUESTIONS, SENTIMENTS, INTENTS, URGENCY_LEVELS


def test_schema_required_questions():
    expected_questions = {"escalate", "sentiment", "urgency", "intent"}
    assert set(QUESTIONS.keys()) == expected_questions


def test_schema_question_structure():
    for qid, q in QUESTIONS.items():
        assert "type" in q, f"{qid} missing 'type'"
        assert "instructions" in q, f"{qid} missing 'instructions'"
        assert "criteria" in q, f"{qid} missing 'criteria'"
        assert isinstance(q["instructions"], str) and len(q["instructions"]) > 0


def test_schema_types_and_limits():
    # escalate: noul
    assert QUESTIONS["escalate"]["type"] == "noul"
    assert set(QUESTIONS["escalate"]["criteria"].keys()) == {"true", "false"}

    # sentiment: choice, <= 5 options (Laya protocol constraint)
    assert QUESTIONS["sentiment"]["type"] == "choice"
    assert len(QUESTIONS["sentiment"]["criteria"]) <= 5
    assert list(QUESTIONS["sentiment"]["criteria"].keys()) == SENTIMENTS

    # urgency: score, length == URGENCY_LEVELS
    assert QUESTIONS["urgency"]["type"] == "score"
    assert isinstance(QUESTIONS["urgency"]["criteria"], list)
    assert len(QUESTIONS["urgency"]["criteria"]) == URGENCY_LEVELS

    # intent: choice, <= 5 options (Laya protocol constraint)
    assert QUESTIONS["intent"]["type"] == "choice"
    assert len(QUESTIONS["intent"]["criteria"]) <= 5
    assert list(QUESTIONS["intent"]["criteria"].keys()) == INTENTS
