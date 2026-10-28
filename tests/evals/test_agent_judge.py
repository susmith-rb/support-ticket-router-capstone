"""Tests for anchored 5/3/1 LLM judge rubrics, packet export, ingestion, and calibration.

# REQ-EVAL-007: Anchored 5/3/1 LLM Judge Rubric & Offline Subagent Evaluation
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.agent_judge_ingest import (
    CALIBRATION_ANCHORS_FILE,
    RUBRICS,
    compute_cohens_kappa,
    export_judge_packets,
    ingest_judge_scores,
    run_calibration_check,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_judge_packet_export_and_ingest(tmp_path: Path):
    # REQ-EVAL-007
    # 1. Test rubrics structure
    for dim in ("groundedness", "helpfulness", "calming_ack"):
        assert dim in RUBRICS
        assert set(RUBRICS[dim].keys()) == {1, 3, 5}
        for score, text in RUBRICS[dim].items():
            assert len(text) > 10, f"Rubric description too short for {dim}:{score}"

    # 2. Test trace packet export
    traces = [
        {
            "case_id": "EX-01",
            "input_message": "What is the return window?",
            "answer": "You can return unopened items within 30 days.",
            "decision": "auto_rag",
            "escalate": False,
            "citations": [{"doc_id": "faq-returns", "quote": "within 30 days"}],
        },
        {
            "case_id": "EX-02",
            "input_message": "You broke my package!",
            "answer": "Ticket TICK-99 has been escalated to a specialist with 15-min ETA.",
            "decision": "escalate",
            "escalate": True,
            "queue": "human-standard",
        },
    ]

    traces_file = tmp_path / "traces.jsonl"
    with open(traces_file, "w", encoding="utf-8") as f:
        for t in traces:
            f.write(json.dumps(t) + "\n")

    packets_file = tmp_path / "packets.jsonl"
    count = export_judge_packets(traces_file, packets_file)
    assert count == 3  # EX-01 produces groundedness + helpfulness, EX-02 produces calming_ack

    with open(packets_file, "r", encoding="utf-8") as f:
        packets = [json.loads(line) for line in f]
    dims = {p["dimension"] for p in packets}
    assert dims == {"groundedness", "helpfulness", "calming_ack"}

    # 3. Test Cohen's Kappa calculation
    # Perfect agreement
    rater_a = [5, 3, 1, 5, 3, 1, 5, 5, 3, 1]
    rater_b = [5, 3, 1, 5, 3, 1, 5, 5, 3, 1]
    assert compute_cohens_kappa(rater_a, rater_b) == 1.0

    # High agreement
    rater_c = [5, 3, 1, 5, 3, 3, 5, 5, 3, 1]
    kappa = compute_cohens_kappa(rater_a, rater_c)
    assert kappa > 0.75

    # 4. Calibration check across 20 anchor cases:
    # REQ-EVAL-007: Reports 'pending' until independent judge subagent scores are provided (no premature kappa claim)
    assert CALIBRATION_ANCHORS_FILE.is_file(), f"Missing {CALIBRATION_ANCHORS_FILE}"
    cal_pending = run_calibration_check(CALIBRATION_ANCHORS_FILE)
    assert cal_pending["status"] == "pending"
    assert cal_pending["total_anchors"] == 20
    assert cal_pending["cohens_kappa"] is None
    assert cal_pending["passes_calibration"] is False

    # When independent scores are supplied (simulated via judge_fn or scores file), computes Cohen's kappa
    cal_eval = run_calibration_check(CALIBRATION_ANCHORS_FILE, judge_fn=lambda p: int(p["gold_score"]))
    assert cal_eval["status"] == "completed"
    assert cal_eval["passes_calibration"] is True
    assert cal_eval["cohens_kappa"] == 1.0

    # 5. Ingestion of scored packets
    scored_file = tmp_path / "scored_packets.jsonl"
    scored_packets = [
        {"dimension": "groundedness", "judge_score": 5},
        {"dimension": "groundedness", "judge_score": 5},
        {"dimension": "helpfulness", "judge_score": 5},
        {"dimension": "helpfulness", "judge_score": 3},
        {"dimension": "calming_ack", "judge_score": 5},
    ]
    with open(scored_file, "w", encoding="utf-8") as f:
        for sp in scored_packets:
            f.write(json.dumps(sp) + "\n")

    summary = ingest_judge_scores(scored_file)
    assert summary["groundedness"]["count"] == 2
    assert summary["groundedness"]["mean"] == 5.0
    assert summary["helpfulness"]["mean"] == 4.0
    assert summary["calming_ack"]["mean"] == 5.0
