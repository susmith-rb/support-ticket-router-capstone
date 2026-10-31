"""Unit tests for synthetic load testing runner, summary math, and cost guards.

# REQ-LOAD-001: Locust Load Testing Profiles and Cost/Quota Safeguards
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loadtest.dataset import LoadTestDataset
from loadtest.guard import CostGuardError, assert_cost_guard, check_cost_guard
from loadtest.profiles import get_profile
from loadtest.runner import LoadTestExecutor
from loadtest.stats import compute_load_summary, percentile


def test_percentile_calculation():
    # REQ-LOAD-001: Percentile interpolation accuracy
    assert percentile([], 50) is None
    assert percentile([42.0], 50) == 42.0

    # 10 values: 10, 20, 30, 40, 50, 60, 70, 80, 90, 100
    values = [float(x) for x in range(10, 110, 10)]
    assert percentile(values, 0) == 10.0
    assert percentile(values, 50) == 55.0
    assert percentile(values, 90) == 91.0
    assert percentile(values, 100) == 100.0


def test_summary_math_comprehensive():
    # REQ-LOAD-001: Comprehensive summary aggregation and breakdown
    records = [
        {
            "timestamp_iso": "2026-10-31T00:00:00Z",
            "request_idx": 1,
            "case_id": "c1",
            "category": "faq",
            "latency_ms": 100.0,
            "success": True,
            "decision": "auto_rag",
            "tier": "tier1_router_s1",
            "queue": "none",
        },
        {
            "timestamp_iso": "2026-10-31T00:00:01Z",
            "request_idx": 2,
            "case_id": "c2",
            "category": "order_auth",
            "latency_ms": 200.0,
            "success": True,
            "decision": "auto_rag",
            "tier": "tier1_router_s1",
            "queue": "none",
        },
        {
            "timestamp_iso": "2026-10-31T00:00:02Z",
            "request_idx": 3,
            "case_id": "c3",
            "category": "escalate_angry",
            "latency_ms": 500.0,
            "success": True,
            "decision": "escalate",
            "tier": "tier2_flash",
            "queue": "human-urgent",
        },
        {
            "timestamp_iso": "2026-10-31T00:00:03Z",
            "request_idx": 4,
            "case_id": "c4",
            "category": "faq",
            "latency_ms": 1000.0,
            "success": False,
            "decision": "error",
            "tier": "none",
            "queue": "none",
            "error": "Timeout",
        },
    ]

    summary = compute_load_summary(records, duration_s=4.0, profile_name="smoke")

    assert summary["total_requests"] == 4
    assert summary["successful_requests"] == 3
    assert summary["failed_requests"] == 1
    assert summary["error_rate_pct"] == 25.0
    assert summary["actual_qps"] == 1.0

    lat = summary["latency_ms"]
    assert lat["min"] == 100.0
    assert lat["max"] == 1000.0
    assert lat["p50"] == 350.0  # linear interpolation of [100, 200, 500, 1000]

    decisions = summary["decisions"]
    assert decisions["auto_rag"]["count"] == 2
    assert decisions["auto_rag"]["percentage"] == 50.0
    assert decisions["escalate"]["count"] == 1
    assert decisions["escalate"]["percentage"] == 25.0
    assert decisions["error"]["count"] == 1

    tiers = summary["tiers"]
    assert tiers["tier1_router_s1"]["count"] == 2
    assert tiers["tier2_flash"]["count"] == 1

    cats = summary["categories"]
    assert "faq" in cats
    assert cats["faq"]["count"] == 2
    assert cats["faq"]["errors"] == 1


def test_load_profiles_and_cost_guard():
    # REQ-LOAD-001: Load testing profiles and cost/quota safeguards
    smoke = get_profile("smoke")
    assert smoke.target_qps == 1.0
    assert smoke.duration_s == 120
    assert not smoke.requires_cost_ack

    # Smoke test runs freely without confirmation
    allowed, msg = check_cost_guard(smoke, i_understand_cost=False)
    assert allowed
    assert_cost_guard(smoke, i_understand_cost=False)

    sustained = get_profile("sustained")
    assert sustained.target_qps == 33.0
    assert sustained.duration_s == 1200
    assert sustained.estimated_requests == 39600
    assert sustained.requires_cost_ack

    # Sustained refuses to run without confirmation
    allowed, msg = check_cost_guard(sustained, i_understand_cost=False, env={})
    assert not allowed
    assert "COST/QUOTA GUARD ACTIVATED" in msg
    assert "39,600" in msg

    with pytest.raises(CostGuardError) as exc_info:
        assert_cost_guard(sustained, i_understand_cost=False, env={})
    assert "39,600" in str(exc_info.value)

    # Sustained passes when CLI flag is provided
    allowed, msg = check_cost_guard(sustained, i_understand_cost=True, env={})
    assert allowed
    assert "COST CONFIRMED" in msg

    # Sustained passes when environment variable is set
    allowed, msg = check_cost_guard(sustained, i_understand_cost=False, env={"I_UNDERSTAND_COST": "1"})
    assert allowed

    spike = get_profile("spike")
    assert spike.duration_s == 300
    assert spike.requires_cost_ack
    with pytest.raises(CostGuardError):
        assert_cost_guard(spike, i_understand_cost=False, env={})

    # Spike dynamic target QPS shape verification
    assert spike.get_target_qps_at(0) == 33.0
    assert spike.get_target_qps_at(30) == 33.0
    # Ramp up at t=90s (halfway between 60 and 120) -> 33 + 0.5 * 67 = 66.5
    assert pytest.approx(spike.get_target_qps_at(90), 0.1) == 66.5
    # Peak at t=180s (between 120 and 240) -> 100
    assert spike.get_target_qps_at(180) == 100.0
    # Ramp down at t=270s (halfway between 240 and 300) -> 100 - 0.5 * 67 = 66.5
    assert pytest.approx(spike.get_target_qps_at(270), 0.1) == 66.5


def test_dataset_weighted_sampling():
    # REQ-LOAD-001: Dataset sampling mix without new PII
    ds = LoadTestDataset()
    assert ds.total_cases() > 0

    samples = [ds.sample() for _ in range(100)]
    categories = {s.category for s in samples}
    # Ensure diverse categories are sampled
    assert "faq" in categories
    assert "order_auth" in categories or "order_anon" in categories
    assert "escalate_angry" in categories

    for s in samples:
        assert s.message
        assert s.category
        if s.oauth:
            assert s.demo_user in ("1001", "1002")


def test_dry_run_executor(tmp_path: Path):
    # REQ-LOAD-001: Dry-run execution generates CSV and JSON artifacts
    profile = get_profile("smoke")
    out_dir = tmp_path / "load_test_run"
    executor = LoadTestExecutor(
        profile=profile,
        dry_run=True,
        max_requests=5,
        duration_s=5,
        concurrency=2,
        out_dir=out_dir,
    )

    summary = executor.run()
    assert summary["total_requests"] == 5
    assert summary["successful_requests"] == 5
    assert summary["error_rate_pct"] == 0.0

    csv_file = out_dir / "requests.csv"
    assert csv_file.is_file()
    lines = csv_file.read_text().splitlines()
    assert len(lines) == 6  # header + 5 rows
    assert "timestamp_iso,request_idx" in lines[0]

    json_file = out_dir / "summary.json"
    assert json_file.is_file()
    saved = json.loads(json_file.read_text())
    assert saved["total_requests"] == 5
    assert saved["profile"] == "smoke"
    assert "p50" in saved["latency_ms"]
