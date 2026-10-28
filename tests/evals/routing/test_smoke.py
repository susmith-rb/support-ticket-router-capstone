"""Unit tests for hermetic evaluation harness smoke test.

# REQ-REVAL-021: Hermetic tests & CI smoke.
Validates that run_smoke executes end-to-end on the fixture dataset and pre-recorded
traces in < 15 seconds, producing valid scorecard and report artifacts with zero
network dependencies.
"""

from __future__ import annotations

from pathlib import Path

from evals.routing.smoke import run_smoke


def test_smoke_end_to_end(tmp_path: Path) -> None:
    # REQ-REVAL-021: End-to-end smoke test on <= 40-case fixture in < 15s.
    res = run_smoke(out_dir=tmp_path, quiet=True)

    assert res["elapsed_seconds"] < 15.0
    assert res["cases_count"] <= 40

    cand_dir = Path(res["candidate_dir"])
    assert (cand_dir / "scorecard.json").exists()
    assert (cand_dir / "report.md").exists()
    assert (cand_dir / "config_snapshot.json").exists()

    scorecard = res["candidate_scorecard"]
    assert scorecard["gates"]["overall_passed"] is True
    assert "comparison" in scorecard
    assert scorecard["comparison"]["verdict"] in ("ACCEPT", "ACCEPT_NEUTRAL")

    report_text = (cand_dir / "report.md").read_text(encoding="utf-8")
    assert "Routing Evaluation Report" in report_text
    assert "Headline Performance Metrics" in report_text
    assert "Hard Gates Checklist" in report_text
    assert "Baseline vs Candidate Comparison" in report_text
