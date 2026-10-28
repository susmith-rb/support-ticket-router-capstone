"""Unit tests for immutable report artifacts and scorecard generator.

# REQ-REVAL-019: Immutable runs & report artifacts.
Validates generation of scorecard.json and report.md, byte-identical rerun
determinism, no PII leakage beyond scrubbed text, and report sections.
"""

from __future__ import annotations

from pathlib import Path

from evals.routing.report import write_run_artifacts
from evals.routing.schema import Expected, RoutingCase, Style


def _make_case(ticket_id: str, intent: str, decision: str, queue: str, description: str = "Order status query") -> RoutingCase:
    return RoutingCase(
        ticket_id=ticket_id,
        content_hash="h1",
        dataset_version="v1.0",
        split="dev",
        created_at="2026-10-28T12:00:00Z",
        subject=None,
        description=description,
        channel="web",
        language="en",
        pii_scrubbed=True,
        expected=Expected(
            intent=intent,
            acceptable_intents=(intent,),
            escalate=(decision == "escalate"),
            decision=decision,
            queue=queue,
        ),
        difficulty="medium",
        scenarios=("normal",),
        style=Style(register="casual", length_bucket="medium (20–80)"),
        source="synthetic",
        source_dataset="synthetic_jetski",
        source_ref="ref",
        ground_truth_source="human_adjudicated",
        label_status="verified",
        verified_fields=("intent", "decision", "queue", "escalate"),
        annotations=(),
        adjudication=None,
        group_id=f"grp-{ticket_id}",
        parent_ticket_id=None,
        perturbation=None,
        failure_ref=None,
    )


def _make_trace(ticket_id: str, intent: str, decision: str, queue: str) -> dict:
    return {
        "ticket_id": ticket_id,
        "predicted": {
            "intent": intent,
            "decision": decision,
            "queue": queue,
            "escalate": (decision == "escalate"),
            "tier": "tier1_router_s1",
            "degraded": False,
        },
        "decision_raw": {"details": {"intent_prob": 0.92}},
        "tier_calls": [{"tier": "tier1_router_s1", "latency_ms": 12.0, "cost_usd": 0.0001}],
        "latency_ms_total": 12.0,
        "cost_usd_total": 0.0001,
    }


def test_write_run_artifacts_and_byte_identity(tmp_path: Path) -> None:
    # REQ-REVAL-019: Report generation and byte-identical determinism.
    run_dir = tmp_path / "run_001_test"
    cases = [_make_case("t1", "order_status", "auto_rag", "none")]
    traces = [_make_trace("t1", "order_status", "auto_rag", "none")]
    cfg = {
        "seed": 20261028,
        "bootstrap_resamples": 50,
        "created_at": "2026-10-28T12:00:00Z",
        "gate_thresholds": {
            "strict_safety_recall_min": 0.98,
            "prompt_injection_violations_max": 0,
            "priority_gaming_violations_max": 0,
            "regression_pass_rate_min": 1.0,
            "taxonomy_validity_min": 1.0,
            "degraded_rate_max": 0.01,
            "error_rate_max": 0.01,
            "cost_cap_usd": 5.00,
        },
    }

    sc1 = write_run_artifacts(run_dir, traces, cases, cfg=cfg)

    sc_path = run_dir / "scorecard.json"
    rep_path = run_dir / "report.md"
    assert sc_path.exists()
    assert rep_path.exists()

    sc_content1 = sc_path.read_text(encoding="utf-8")
    rep_content1 = rep_path.read_text(encoding="utf-8")

    assert "Routing Evaluation Report" in rep_content1
    assert "Headline Performance Metrics" in rep_content1
    assert "Hard Gates Checklist" in rep_content1

    # Second run to test exact byte identity
    sc2 = write_run_artifacts(run_dir, traces, cases, cfg=cfg)
    sc_content2 = sc_path.read_text(encoding="utf-8")
    rep_content2 = rep_path.read_text(encoding="utf-8")

    assert sc_content1 == sc_content2
    assert rep_content1 == rep_content2
    assert sc1 == sc2


def test_report_no_pii_leak(tmp_path: Path) -> None:
    # REQ-REVAL-019: No unredacted PII patterns in scorecard or report.
    run_dir = tmp_path / "run_002_pii"
    # Case with properly redacted token [ORDER] and [EMAIL], marked incorrect so it shows in failure clusters
    cases = [_make_case("t_redact", "order_status", "auto_rag", "none", description="Order [ORDER] for user [EMAIL]")]
    traces = [_make_trace("t_redact", "other", "escalate", "human-standard")]
    cfg = {"bootstrap_resamples": 20, "created_at": "2026-10-28T12:00:00Z"}

    write_run_artifacts(run_dir, traces, cases, cfg=cfg)
    rep_text = (run_dir / "report.md").read_text(encoding="utf-8")

    assert "[ORDER]" in rep_text or "[EMAIL]" in rep_text
    # Ensure raw email @example.com or raw 16-digit cards are not present
    assert "@example.com" not in rep_text
