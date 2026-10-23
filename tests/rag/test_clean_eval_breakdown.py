import pytest
from clean_eval_breakdown import esc, compute_breakdown


def test_esc_logic():
    # escalate=True -> esc() is True regardless of grounded
    assert esc({"gen": {"escalate": True, "grounded": True}}) is True
    assert esc({"gen": {"escalate": True, "grounded": False}}) is True

    # grounded=False -> esc() is True even if escalate=False
    assert esc({"gen": {"escalate": False, "grounded": False}}) is True

    # escalate=False and grounded=True -> esc() is False
    assert esc({"gen": {"escalate": False, "grounded": True}}) is False

    # grounded defaults to True if missing and escalate is False -> False
    assert esc({"gen": {"escalate": False}}) is False

    # empty gen -> grounded defaults to True, escalate defaults to False -> False
    assert esc({}) is False


def test_compute_breakdown():
    answers = [
        # Hit (rank <= 5), answered, cites source
        {"rank": 1, "source_doc": "doc-a", "gen": {"escalate": False, "grounded": True, "citations": [{"doc_id": "doc-a", "quote": "q"}]}},
        # Hit (rank <= 5), abstained
        {"rank": 3, "source_doc": "doc-b", "gen": {"escalate": True, "grounded": True, "citations": []}},
        # Miss (rank > 5), answered, does not cite source
        {"rank": 7, "source_doc": "doc-c", "gen": {"escalate": False, "grounded": True, "citations": [{"doc_id": "doc-other", "quote": "q"}]}},
        # Miss (no rank), abstained
        {"rank": None, "source_doc": "doc-d", "gen": {"escalate": False, "grounded": False, "citations": []}},
    ]
    judge = [
        {"claims": [{"claim": "claim 1", "supported": True}, {"claim": "claim 2", "supported": False}]}
    ]

    bd = compute_breakdown(answers, judge)

    assert bd["total"] == 4
    assert bd["hit_count"] == 2
    assert bd["miss_count"] == 2
    assert bd["abstained_hit"] == 1
    assert bd["abstained_miss"] == 1
    assert bd["abstained_hit_rate"] == 0.5
    assert bd["abstained_miss_rate"] == 0.5
    assert bd["answered_count"] == 2
    assert bd["answered_cites_source"] == 1
    assert bd["answered_cites_source_rate"] == 0.5
    assert bd["unsupported_claims"] == ["claim 2"]
