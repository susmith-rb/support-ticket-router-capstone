"""Unit tests for immutable report artifacts and scorecard generator.

# REQ-REVAL-019: Immutable runs & report artifacts.
Validates generation of scorecard.json and report.md, byte-identical rerun
determinism, no PII leakage beyond scrubbed text, and report sections.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.routing.report import resolve_dataset_path, write_run_artifacts
from evals.routing.schema import Expected, RoutingCase, Style


def _make_case(
    ticket_id: str,
    intent: str,
    decision: str,
    queue: str,
    description: str = "Order status query",
    label_status: str = "verified",
    verified_fields: tuple[str, ...] = ("intent", "decision", "queue", "escalate"),
    source_dataset: str = "synthetic_jetski",
    notes: str | None = None,
) -> RoutingCase:
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
        source_dataset=source_dataset,
        source_ref="ref",
        ground_truth_source="human_adjudicated",
        label_status=label_status,
        verified_fields=verified_fields,
        annotations=(),
        adjudication=None,
        group_id=f"grp-{ticket_id}",
        parent_ticket_id=None,
        perturbation=None,
        failure_ref=None,
        notes=notes,
    )


def _make_trace(
    ticket_id: str,
    intent: str,
    decision: str,
    queue: str,
    tier: str = "tier1_router_s1",
    latency_ms: float = 12.0,
    cost_usd: float = 0.0001,
    degraded: bool = False,
    error: str | None = None,
    details: dict | None = None,
) -> dict:
    det = {"intent_prob": 0.92}
    if details:
        det.update(details)
    return {
        "ticket_id": ticket_id,
        "predicted": {
            "intent": intent,
            "decision": decision,
            "queue": queue,
            "escalate": (decision == "escalate"),
            "tier": tier,
            "degraded": degraded,
        },
        "decision_raw": {"reason": "test_reason", "details": det},
        "tier_calls": [{"tier": tier, "latency_ms": latency_ms, "cost_usd": cost_usd}],
        "latency_ms_total": latency_ms,
        "cost_usd_total": cost_usd,
        "error": error,
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
    assert "@example.com" not in rep_text


def test_report_empty_denominators_and_headline_n(tmp_path: Path) -> None:
    # Regression test for Bug 1: verified view with intent-only verified cases
    run_dir = tmp_path / "run_empty_denoms"
    cases = [
        _make_case(
            "t1",
            "order_status",
            "auto_rag",
            "none",
            label_status="verified",
            verified_fields=("intent",),  # NO decision, NO queue, NO escalate
        )
    ]
    traces = [_make_trace("t1", "order_status", "auto_rag", "none")]
    cfg = {"bootstrap_resamples": 10, "created_at": "2026-10-28T12:00:00Z"}

    sc = write_run_artifacts(run_dir, traces, cases, cfg=cfg)
    assert sc is not None
    rep_text = (run_dir / "report.md").read_text(encoding="utf-8")

    # Scorecard JSON has null for n=0 metrics in verified view
    assert sc["metrics"]["verified"]["decision_accuracy"] is None
    assert sc["metrics"]["verified"]["routing_correctness"] is None
    assert sc["metrics"]["verified"]["strict_safety_recall"] is None

    # Report markdown renders n/a (n=0)
    assert "| **Decision Accuracy** | n/a (n=0) | n/a |" in rep_text
    assert "| **Routing Correctness (Primary)** | n/a (n=0) | n/a |" in rep_text
    assert "| **Strict Safety Recall (Wilson LB)** | n/a (n=0) | n/a |" in rep_text

    # Metrics with n>0 render with count
    assert "1.0000 (n=1)" in rep_text


def test_report_gates_not_evaluated_and_incomplete(tmp_path: Path) -> None:
    # Regression test for Bug 1 & 2: gate evaluation on n=0 cases produces NOT_EVALUATED and INCOMPLETE
    run_dir = tmp_path / "run_incomplete"
    cases = [
        _make_case(
            "t1",
            "order_status",
            "auto_rag",
            "none",
            label_status="verified",
            verified_fields=("intent",),
        )
    ]
    traces = [_make_trace("t1", "order_status", "auto_rag", "none")]
    cfg = {
        "bootstrap_resamples": 10,
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

    sc = write_run_artifacts(run_dir, traces, cases, cfg=cfg)
    assert sc is not None
    rep_text = (run_dir / "report.md").read_text(encoding="utf-8")

    assert sc["gates"]["overall_status"] == "INCOMPLETE"
    assert sc["gates"]["overall_passed"] is False
    assert "- **Overall Hard Gates Passed**: ⚠️ INCOMPLETE" in rep_text

    assert "| `regression_pass_rate` | `1.0` | n/a | ⚠️ NOT_EVALUATED | run regression split |" in rep_text
    assert "| `strict_safety_recall` | `0.98` | n/a | ⚠️ NOT_EVALUATED |" in rep_text


def test_report_data_provenance_and_warning_banner(tmp_path: Path) -> None:
    # Regression test for Bug 7: data provenance summary & <50% verified warning banner
    run_dir = tmp_path / "run_prov"
    cases = [
        _make_case("t1", "order_status", "auto_rag", "none", label_status="provisional"),
        _make_case("t2", "order_status", "auto_rag", "none", label_status="disputed"),
        _make_case("t3", "order_status", "auto_rag", "none", label_status="verified"),
    ]
    traces = [
        _make_trace("t1", "order_status", "auto_rag", "none"),
        _make_trace("t2", "order_status", "auto_rag", "none"),
        _make_trace("t3", "order_status", "auto_rag", "none"),
    ]
    cfg = {"bootstrap_resamples": 10, "created_at": "2026-10-28T12:00:00Z"}

    sc = write_run_artifacts(run_dir, traces, cases, cfg=cfg)
    assert sc is not None
    rep_text = (run_dir / "report.md").read_text(encoding="utf-8")

    prov = sc["provenance"]
    assert prov["verified_cases"] == 1
    assert prov["total_cases"] == 3
    assert prov["has_low_verified_warning"] is True

    assert "### Data Provenance Summary" in rep_text
    assert "- **Label Status**: disputed: 1, provisional: 1, verified: 1" in rep_text
    assert "> ⚠️ **Warning**: Fewer than 50% of evaluated cases are verified (33.33%)." in rep_text


def test_report_failure_clusters_and_representatives(tmp_path: Path) -> None:
    # Regression test for Bug 5: clusters and representative misclassified tickets rendered with tier
    run_dir = tmp_path / "run_clusters"
    cases = [
        _make_case("t_fail1", "shipping_policy", "auto_rag", "none", description="Where is shipping policy?"),
        _make_case("t_fail2", "shipping_policy", "auto_rag", "none", description="Shipping options help"),
    ]
    traces = [
        _make_trace("t_fail1", "shipping", "auto_rag", "none", tier="tier2_flash"),
        _make_trace("t_fail2", "shipping", "auto_rag", "none", tier="tier2_flash"),
    ]
    cfg = {"bootstrap_resamples": 10, "created_at": "2026-10-28T12:00:00Z"}

    sc = write_run_artifacts(run_dir, traces, cases, cfg=cfg)
    assert sc is not None
    rep_text = (run_dir / "report.md").read_text(encoding="utf-8")

    assert len(sc["clusters"]) > 0
    assert "## 8. Top Failure Clusters" in rep_text
    assert "| Rank | Error Type | Transition | Deciding Tier | Size | Severity | Score | Signature Terms |" in rep_text
    assert "Tier: `tier2_flash`" in rep_text
    assert "### Representative Misclassified Tickets" in rep_text


def test_report_cost_latency_slices_and_degraded_table(tmp_path: Path) -> None:
    # Regression test for Bugs 3, 4, 7, 8: cost projections, tier latency, slices, and degraded cases
    run_dir = tmp_path / "run_details"
    cases = [
        _make_case("t1", "order_status", "auto_rag", "none", source_dataset="bitext_cs"),
        _make_case("t2", "account_payment", "escalate", "human-urgent", notes="laya_train_collision:hash1"),
    ]
    traces = [
        _make_trace("t1", "order_status", "auto_rag", "none", latency_ms=100.0, cost_usd=0.001),
        _make_trace(
            "t2",
            "account_payment",
            "escalate",
            "human-urgent",
            latency_ms=250.0,
            cost_usd=0.002,
            degraded=True,
            details={"s1_error": "timeout_error", "t2_error": None},
        ),
    ]
    cfg = {"bootstrap_resamples": 10, "created_at": "2026-10-28T12:00:00Z"}

    sc = write_run_artifacts(run_dir, traces, cases, cfg=cfg)
    assert sc is not None
    rep_text = (run_dir / "report.md").read_text(encoding="utf-8")

    # Cost projections in Section 9
    assert "- **Total Run Cost**: `$0.0030`" in rep_text
    assert "- **Projected Monthly Cost (1M tickets)**:" in rep_text
    assert "- **Projected Monthly Cost (10M tickets)**:" in rep_text
    assert "### Per-Tier Latency Breakdown" in rep_text
    assert "| `tier1_router_s1` |" in rep_text

    # Slices in Section 10
    assert "## 10. Per-Slice Performance" in rep_text
    assert "### By Source Dataset" in rep_text
    assert "### By Laya Contamination (in-distribution collision)" in rep_text
    assert "`contaminated`" in rep_text

    # Degraded table in Section 11
    assert "## 11. Degraded / Error Cases" in rep_text
    assert "| `t2` |" in rep_text
    assert "`timeout_error`" in rep_text


def test_resolve_dataset_path(tmp_path: Path) -> None:
    # Regression test for Bug 9: dataset path resolution
    # 1. Existing file
    f = tmp_path / "custom.jsonl"
    f.write_text("{}\n")
    assert resolve_dataset_path(f, split="dev") == f

    # 2. Existing directory
    d = tmp_path / "dataset_dir"
    d.mkdir()
    split_file = d / "dev.jsonl"
    split_file.write_text("{}\n")
    assert resolve_dataset_path(d, split="dev") == split_file
    # Non-existent split falls back to dir
    assert resolve_dataset_path(d, split="heldout") == d

    # 3. Default search finding highest routing_v1.*
    base = tmp_path / "data"
    base.mkdir()
    (base / "routing_v1.0.0").mkdir()
    (base / "routing_v1.0.0" / "dev.jsonl").write_text("{}\n")
    (base / "routing_v1.2.0").mkdir()
    v120_dev = base / "routing_v1.2.0" / "dev.jsonl"
    v120_dev.write_text("{}\n")

    from evals.routing.report import _find_highest_version_dataset_dir

    highest = _find_highest_version_dataset_dir(base)
    assert highest.name == "routing_v1.2.0"


def test_created_at_from_config_snapshot(tmp_path: Path) -> None:
    # Regression test for Bug 6: timestamp from config_snapshot.json
    run_dir = tmp_path / "run_snapshot_timestamp"
    run_dir.mkdir()
    snap_path = run_dir / "config_snapshot.json"
    expected_ts = "2026-10-28T19:23:30.222103+00:00"
    snap_path.write_text(json.dumps({"created_at": expected_ts}))

    cases = [_make_case("t1", "order_status", "auto_rag", "none")]
    traces = [_make_trace("t1", "order_status", "auto_rag", "none")]
    cfg = {"bootstrap_resamples": 10}

    sc = write_run_artifacts(run_dir, traces, cases, cfg=cfg)
    assert sc is not None
    rep_text = (run_dir / "report.md").read_text(encoding="utf-8")

    assert sc["created_at"] == expected_ts
    assert f"- **Generated At**: `{expected_ts}`" in rep_text


def test_report_sections_error_summary_and_cost_split(tmp_path: Path) -> None:
    # REQ-REVAL-017 & REQ-REVAL-018: Section 8 has Error Type Summary, Section 9 has Cost Split
    run_dir = tmp_path / "run_sec8_sec9"
    cases = [
        _make_case("t1", "order_status", "auto_rag", "none"),
        _make_case("t2", "account_payment", "escalate", "human-urgent"),
    ]
    traces = [
        # t1: misclassified as shipping (invalid label)
        _make_trace("t1", "shipping", "auto_rag", "none", tier="tier2_flash"),
        # t2: correct
        _make_trace("t2", "account_payment", "escalate", "human-urgent", tier="tier1_router_s1"),
    ]
    # Add tier calls with purpose and unmeasured latency to trace 1
    traces[0]["tier_calls"] = [
        {"tier": "tier1_router_s1", "pass_index": 0, "purpose": "decision", "cost_usd": 0.0005, "latency_ms": 10.0, "latency_measured": True},
        {"tier": "tier2_flash", "pass_index": 0, "purpose": "decision", "cost_usd": 0.0010, "latency_ms": 120.0, "latency_measured": False},
        {"tier": "tier2_flash", "pass_index": 1, "purpose": "reliability", "cost_usd": 0.0010, "latency_ms": 120.0, "latency_measured": False},
    ]
    traces[0]["cost_usd_total"] = 0.0025
    traces[1]["tier_calls"] = [
        {"tier": "tier1_router_s1", "pass_index": 0, "purpose": "decision", "cost_usd": 0.0005, "latency_ms": 15.0, "latency_measured": True},
    ]
    traces[1]["cost_usd_total"] = 0.0005

    cfg = {"bootstrap_resamples": 10, "created_at": "2026-10-28T12:00:00Z"}
    sc = write_run_artifacts(run_dir, traces, cases, cfg=cfg)
    assert sc is not None
    rep_text = (run_dir / "report.md").read_text(encoding="utf-8")

    # Section 8 assertions
    assert "### Error Type Summary" in rep_text
    assert "| `invalid_label` | `tier2_flash` | 1 | 100.00% |" in rep_text
    assert "### Top Failure Clusters" in rep_text
    assert "| 1 | `invalid_label` | `*->shipping` | `tier2_flash` | 1 | 3 | 3 |" in rep_text

    # Section 9 assertions
    assert "- **Total Run Cost**: `$0.0030`" in rep_text
    assert "- **Decision Cost**: `$0.0020`" in rep_text
    assert "- **Reliability Cost (k-pass)**: `$0.0010`" in rep_text
    assert "n/a*" in rep_text
    assert "*Note: Tiers marked with `n/a*` lack measured live wall-clock latency" in rep_text
