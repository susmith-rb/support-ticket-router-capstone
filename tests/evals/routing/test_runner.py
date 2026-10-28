"""Unit and integration tests for the routing evaluation runner.

# REQ-REVAL-011: Runner targets, tier cache, dry-run & cost cap.
# REQ-REVAL-015: Stochastic-tier reliability (k-pass).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.routing.cache import CacheMiss
from evals.routing.cli_run import main as cli_main
from evals.routing.runner import (
    CostCapExceeded,
    next_run_dir,
    run,
)
from tests.evals.routing.fakes import (
    FakeRoutingCase,
    FakeS1Client,
    FakeTier2Invoker,
    make_prediction,
)


@pytest.fixture
def tmp_eval_env(tmp_path: Path):
    """Fixture providing isolated cache and run directories."""
    cache_dir = tmp_path / "cache"
    runs_dir = tmp_path / "runs"
    cache_dir.mkdir(parents=True)
    runs_dir.mkdir(parents=True)
    return {
        "cache_dir": cache_dir,
        "runs_dir": runs_dir,
        "tmp_path": tmp_path,
    }


def test_next_run_dir_never_overwrites(tmp_eval_env):
    """REQ-REVAL-011: Run directory generation increments index monotonically and never overwrites."""
    runs_dir = tmp_eval_env["runs_dir"]
    d1 = next_run_dir(root=runs_dir, label="baseline")
    d2 = next_run_dir(root=runs_dir, label="baseline")
    d3 = next_run_dir(root=runs_dir, label="candidate")

    assert d1.name == "run_001_baseline"
    assert d2.name == "run_002_baseline"
    assert d3.name == "run_003_candidate"
    assert d1.exists() and d2.exists() and d3.exists()


def test_live_to_replay_round_trip_equality(tmp_eval_env):
    """REQ-REVAL-011: Replay traces match live traces exactly except cache_hit flags."""
    cache_dir = tmp_eval_env["cache_dir"]
    runs_dir = tmp_eval_env["runs_dir"]

    cases = [
        FakeRoutingCase(ticket_id="TICK-101", subject="Where is my order?", description="ORD-998811"),
        FakeRoutingCase(ticket_id="TICK-102", subject="I want a refund", description="Product arrived damaged"),
    ]

    s1_fake = FakeS1Client(
        responses={
            "ORD-998811": make_prediction(
                intent="order_status",
                decision="auto_rag",
                escalate_prob=0.04,
                intent_prob=0.95,
                latency_ms=22.0,
            ),
            "damaged": make_prediction(
                intent="returns_refunds",
                decision="escalate",
                escalate=True,
                escalate_prob=0.45,
                intent_prob=0.88,
                latency_ms=24.0,
            ),
        }
    )

    t2_fake = FakeTier2Invoker()

    # 1. Live run -> populates cache
    live_traces = run(
        cases=cases,
        target="policy",
        candidate_overlay=None,
        run_dir=runs_dir / "run_live",
        mode="live",
        cache_root=cache_dir,
        s1_client_override=s1_fake,
        tier2_invoker_override=t2_fake,
    )

    assert len(live_traces) == 2
    for t in live_traces:
        for c in t.tier_calls:
            assert c["cache_hit"] is False

    # 2. Replay run -> zero live calls, inner clients are None
    replay_traces = run(
        cases=cases,
        target="replay",
        candidate_overlay=None,
        run_dir=runs_dir / "run_replay",
        mode="replay",
        cache_root=cache_dir,
        s1_client_override=None,
        tier2_invoker_override=None,
    )

    assert len(replay_traces) == 2
    for t in replay_traces:
        for c in t.tier_calls:
            assert c["cache_hit"] is True

    # Compare traces: byte-identical except cache_hit flags
    for t_live, t_replay in zip(live_traces, replay_traces):
        assert t_live.ticket_id == t_replay.ticket_id
        assert t_live.predicted == t_replay.predicted
        assert t_live.decision_raw == t_replay.decision_raw
        assert len(t_live.tier_calls) == len(t_replay.tier_calls)
        for c_live, c_replay in zip(t_live.tier_calls, t_replay.tier_calls):
            assert c_live["tier"] == c_replay["tier"]
            assert c_live["prediction"] == c_replay["prediction"]
            assert c_live["latency_ms"] == c_replay["latency_ms"]


def test_cache_miss_in_replay_raises_error(tmp_eval_env):
    """REQ-REVAL-011: Cache miss in replay mode raises CacheMiss."""
    cache_dir = tmp_eval_env["cache_dir"]
    runs_dir = tmp_eval_env["runs_dir"]

    cases = [FakeRoutingCase(ticket_id="TICK-MISS", description="Unseen ticket")]

    with pytest.raises(CacheMiss):
        run(
            cases=cases,
            target="replay",
            run_dir=runs_dir / "run_miss",
            mode="replay",
            cache_root=cache_dir,
            s1_client_override=None,
            tier2_invoker_override=None,
        )


def test_dry_run_makes_zero_inner_calls_and_writes_file(tmp_eval_env):
    """REQ-REVAL-011: Dry-run renders prompts, counts cache hits/misses, writes dry_run.json, zero inner calls."""
    cache_dir = tmp_eval_env["cache_dir"]
    runs_dir = tmp_eval_env["runs_dir"]

    cases = [
        FakeRoutingCase(ticket_id="TICK-DRY-1", description="Can I change my address?"),
        FakeRoutingCase(ticket_id="TICK-DRY-2", description="Billing issue with credit card"),
    ]

    s1_fake = FakeS1Client()
    t2_fake = FakeTier2Invoker()

    run_dir = runs_dir / "run_dry"
    traces = run(
        cases=cases,
        target="policy",
        run_dir=run_dir,
        dry_run=True,
        cache_root=cache_dir,
        s1_client_override=s1_fake,
        tier2_invoker_override=t2_fake,
    )

    assert len(traces) == 2
    for t in traces:
        assert t.is_dry_run is True

    # Zero live model calls made
    assert len(s1_fake.call_history) == 0
    assert len(t2_fake.calls) == 0

    # dry_run.json written
    dry_json_path = run_dir / "dry_run.json"
    assert dry_json_path.is_file()
    data = json.loads(dry_json_path.read_text(encoding="utf-8"))
    assert data["is_dry_run"] is True
    assert data["cases_count"] == 2
    assert "cache_stats" in data
    assert "sample_tier2_prompts" in data
    assert len(data["sample_tier2_prompts"]) == 2


def test_cost_cap_pre_run_abort(tmp_eval_env):
    """REQ-REVAL-011: Pre-run cost estimate exceeding max_cost_usd aborts before any network call."""
    cache_dir = tmp_eval_env["cache_dir"]
    runs_dir = tmp_eval_env["runs_dir"]

    cases = [FakeRoutingCase(ticket_id="TICK-EXP", description="Expensive ticket")]
    s1_fake = FakeS1Client()
    t2_fake = FakeTier2Invoker()

    with pytest.raises(CostCapExceeded):
        run(
            cases=cases,
            target="policy",
            run_dir=runs_dir / "run_cap",
            mode="live",
            max_cost_usd=0.0000001,  # Tiny budget
            cache_root=cache_dir,
            s1_client_override=s1_fake,
            tier2_invoker_override=t2_fake,
        )

    assert len(s1_fake.call_history) == 0
    assert len(t2_fake.calls) == 0


def test_cost_cap_mid_run_abort(tmp_eval_env):
    """REQ-REVAL-011: Cumulative live cost exceeding budget aborts subsequent cases with error cost_cap_abort."""
    cache_dir = tmp_eval_env["cache_dir"]
    runs_dir = tmp_eval_env["runs_dir"]

    # Pre-run estimate uses cache miss counts.
    # Set up 5 cases where Tier 2 is invoked (e.g. uncertain S1).
    cases = [
        FakeRoutingCase(ticket_id=f"TICK-ABORT-{i:02d}", description=f"Ticket description {i}")
        for i in range(5)
    ]

    s1_fake = FakeS1Client(
        default_pred=make_prediction(
            intent="other",
            decision="auto_rag",
            escalate_prob=0.25,  # Uncertainty band -> invokes Tier 2
            intent_prob=0.50,
        )
    )
    # Tier 2 with high token usage (e.g. 1000 in, 500 out -> $0.00045 per call + S1 0.0005 = 0.00095 per case)
    t2_fake = FakeTier2Invoker(
        usage={"prompt_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}
    )

    # Pre-run estimates based on default expected tokens: 5 * (0.0005 + 0.0000885) = ~0.00294
    # Set max_cost_usd to 0.0035 so pre-run passes, but actual usage ($0.00095/case) exceeds $0.0035 on case 4
    traces = run(
        cases=cases,
        target="policy",
        run_dir=runs_dir / "run_mid_cap",
        mode="live",
        max_cost_usd=0.0035,
        concurrency=1,  # Sequential to test deterministic mid-run abort
        cache_root=cache_dir,
        s1_client_override=s1_fake,
        tier2_invoker_override=t2_fake,
    )

    errors = [t.error for t in traces if t.error == "cost_cap_abort"]
    assert len(errors) > 0


def test_reliability_k_passes_recorded_only_for_tier2(tmp_eval_env):
    """REQ-REVAL-015: Reliability k-passes collected if and only if Tier 2 decided."""
    cache_dir = tmp_eval_env["cache_dir"]
    runs_dir = tmp_eval_env["runs_dir"]

    # Case 1: Tier 1 definitive auto_rag (tau_low < 0.15)
    # Case 2: Tier 1 uncertain (0.15 <= p_esc < 0.35) -> falls back to Tier 2
    cases = [
        FakeRoutingCase(ticket_id="TICK-T1-DECIDED", description="Where is my package?"),
        FakeRoutingCase(ticket_id="TICK-T2-DECIDED", description="Ambiguous situation about return"),
    ]

    s1_fake = FakeS1Client(
        responses={
            "Where is my package?": make_prediction(
                intent="order_status",
                decision="auto_rag",
                escalate_prob=0.05,
                intent_prob=0.92,
            ),
            "Ambiguous": make_prediction(
                intent="other",
                decision="auto_rag",
                escalate_prob=0.25,  # In uncertainty band
                intent_prob=0.50,
            ),
        }
    )

    t2_fake = FakeTier2Invoker(
        pass_responses={
            0: make_prediction(intent="returns_refunds", escalate=False, decision="auto_rag"),
            1: make_prediction(intent="returns_refunds", escalate=True, decision="escalate"),
            2: make_prediction(intent="returns_refunds", escalate=False, decision="auto_rag"),
        }
    )

    traces = run(
        cases=cases,
        target="policy",
        run_dir=runs_dir / "run_rel",
        mode="live",
        reliability_k=3,
        cache_root=cache_dir,
        s1_client_override=s1_fake,
        tier2_invoker_override=t2_fake,
    )

    t1_trace = next(t for t in traces if t.ticket_id == "TICK-T1-DECIDED")
    t2_trace = next(t for t in traces if t.ticket_id == "TICK-T2-DECIDED")

    # Tier 1 decided case must NOT have reliability passes
    assert t1_trace.predicted["tier"] == "tier1_router_s1"
    assert t1_trace.reliability is None

    # Tier 2 decided case MUST have k passes
    assert t2_trace.predicted["tier"] == "tier2_flash"
    assert t2_trace.reliability is not None
    assert len(t2_trace.reliability["passes"]) == 3
    assert t2_trace.reliability["passes"][0]["decision"] == "auto_rag"
    assert t2_trace.reliability["passes"][1]["decision"] == "escalate"


def test_overlay_thresholds_change_decisions_at_zero_cost(tmp_eval_env):
    """REQ-REVAL-011: Candidate overlay thresholds alter routing decisions in replay with zero model calls."""
    cache_dir = tmp_eval_env["cache_dir"]
    runs_dir = tmp_eval_env["runs_dir"]

    # S1 prediction has p_esc = 0.30
    # Under baseline (tau_esc=0.35, tau_low=0.15), 0.30 is uncertain -> routes to Tier 2
    # Under overlay (tau_esc=0.25), 0.30 >= tau_esc -> definitive escalation at Tier 1!
    cases = [FakeRoutingCase(ticket_id="TICK-THRESH", description="I am unhappy with service")]

    s1_fake = FakeS1Client(
        default_pred=make_prediction(
            intent="complaint",
            decision="auto_rag",
            escalate_prob=0.30,
            intent_prob=0.85,
        )
    )
    t2_fake = FakeTier2Invoker()

    # 1. Populate cache in baseline live run
    traces_baseline = run(
        cases=cases,
        target="policy",
        candidate_overlay=None,
        run_dir=runs_dir / "run_base",
        mode="live",
        cache_root=cache_dir,
        s1_client_override=s1_fake,
        tier2_invoker_override=t2_fake,
    )
    assert traces_baseline[0].predicted["tier"] == "tier2_flash"

    # 2. Replay with threshold overlay (tau_esc = 0.25)
    overlay = {"router": {"tau_esc": 0.25}}
    traces_candidate = run(
        cases=cases,
        target="replay",
        candidate_overlay=overlay,
        run_dir=runs_dir / "run_cand",
        mode="replay",
        cache_root=cache_dir,
        s1_client_override=None,  # Zero live calls
        tier2_invoker_override=None,
    )

    # Decision flipped to Tier 1 definitive escalation at zero model cost!
    assert traces_candidate[0].predicted["tier"] == "tier1_router_s1"
    assert traces_candidate[0].predicted["decision"] == "escalate"
    assert traces_candidate[0].cost_usd_total == 0.0005  # only cached S1 amortized cost


def test_missing_prompt_version_raises_clear_error(tmp_eval_env):
    """REQ-REVAL-011: Referencing missing prompt version in candidate overlay fails clearly."""
    runs_dir = tmp_eval_env["runs_dir"]
    cases = [FakeRoutingCase(ticket_id="TICK-V2", description="Some text")]

    with pytest.raises(FileNotFoundError, match="Tier 2 prompt version 'v99'"):
        run(
            cases=cases,
            candidate_overlay={"prompts": {"tier2_router_version": "v99"}},
            run_dir=runs_dir / "run_v99",
        )


def test_concurrency_determinism(tmp_eval_env):
    """REQ-REVAL-011: Results with concurrency 1 vs 8 are identical and sorted by ticket_id."""
    cache_dir = tmp_eval_env["cache_dir"]
    runs_dir = tmp_eval_env["runs_dir"]

    cases = [
        FakeRoutingCase(ticket_id=f"TICK-{i:03d}", description=f"Text for ticket {i}")
        for i in [9, 3, 7, 1, 5, 2, 8, 4, 6, 0]
    ]

    s1_fake = FakeS1Client()
    t2_fake = FakeTier2Invoker()

    # Pre-populate cache
    run(
        cases=cases,
        target="policy",
        run_dir=runs_dir / "run_pop",
        mode="live",
        cache_root=cache_dir,
        s1_client_override=s1_fake,
        tier2_invoker_override=t2_fake,
    )

    res_seq = run(
        cases=cases,
        target="replay",
        run_dir=runs_dir / "run_c1",
        concurrency=1,
        cache_root=cache_dir,
    )

    res_par = run(
        cases=cases,
        target="replay",
        run_dir=runs_dir / "run_c8",
        concurrency=8,
        cache_root=cache_dir,
    )

    assert len(res_seq) == len(res_par) == 10
    ticket_ids_seq = [r.ticket_id for r in res_seq]
    ticket_ids_par = [r.ticket_id for r in res_par]

    assert ticket_ids_seq == sorted(ticket_ids_seq)
    assert ticket_ids_seq == ticket_ids_par

    for r1, r2 in zip(res_seq, res_par):
        assert r1.predicted == r2.predicted
        assert r1.decision_raw == r2.decision_raw


def test_cli_heldout_guard_enforced(tmp_eval_env, monkeypatch):
    """REQ-REVAL-011 / REQ-REVAL-010: CLI requires --preregistration on heldout split."""
    tmp_path = tmp_eval_env["tmp_path"]
    dataset_dir = tmp_path / "routing_v1"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "heldout.jsonl").write_text(
        json.dumps({"ticket_id": "T1", "description": "desc", "subject": "subj"}) + "\n"
    )

    # 1. Missing preregistration fails
    with pytest.raises(ValueError, match="--preregistration ID is required"):
        cli_main([
            "run",
            "--split", "heldout",
            "--dataset", str(dataset_dir),
            "--dry-run",
        ])


def test_live_s1_client_uses_production_endpoint_wiring(monkeypatch, tmp_path):
    # REQ-REVAL-011: the policy target must build RouterS1Client exactly like production
    # (dedicated endpoint + dedicated base URL), not with library defaults.
    import ticket_router.models.router_s1_client as s1mod
    from ticket_router.common.settings import Settings

    captured = {}

    class _Spy:
        def __init__(self, endpoint=None, base_url=None, **kw):
            captured["endpoint"], captured["base_url"] = endpoint, base_url

        def route(self, text):  # pragma: no cover - not called in dry wiring check
            raise RuntimeError("no network in tests")

    monkeypatch.setattr(s1mod, "RouterS1Client", _Spy)
    from evals.routing import runner
    from tests.evals.routing.fakes import FakeTier2Invoker

    case = FakeRoutingCase(ticket_id="TICK-WIRE", description="Where is my order?")
    runner.run([case], target="policy", candidate_overlay={}, run_dir=tmp_path / "r", mode="live",
               tier2_invoker_override=FakeTier2Invoker(), cache_root=tmp_path / "c")
    s = Settings.from_env()
    assert captured == {"endpoint": s.router_s1_endpoint, "base_url": s.router_s1_base_url}
