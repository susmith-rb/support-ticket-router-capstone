"""Tests for dual-target evaluation runner, cost cap, and telemetry schema.

# REQ-EVAL-004: Dual-Target Agent Evaluation Runner
# REQ-EVAL-005: Per-Case Trace Telemetry Schema
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.agent_e2e import (
    CorpusCitationMatcher,
    DeployedAgentRunner,
    LocalAgentRunner,
    classify_deployed_response,
    compute_token_cost,
    detect_order_disclosed,
    enrich_deployed_traces,
    run_evaluation,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_runner_dual_targets_and_cost_cap(tmp_path: Path):
    # REQ-EVAL-004
    # 1. Local runner execution
    local_runner = LocalAgentRunner()
    sample_case = {
        "case_id": "TEST-LOCAL-01",
        "message": "Hello, how do I return an unopened item?",
        "customer_id": "cust_anon",
        "expected_decision": "auto_rag",
        "expected_escalate": False,
        "expected_priority": "P2_STANDARD",
        "is_strict_angry_urgent": False,
    }
    local_trace = local_runner.run_case(sample_case)
    assert local_trace["case_id"] == "TEST-LOCAL-01"
    assert "decision" in local_trace
    assert "latency_ms" in local_trace
    assert "cost_usd" in local_trace

    # 2. Deployed runner in dry-run mode
    dep_runner = DeployedAgentRunner(dry_run=True)
    dep_trace = dep_runner.run_case(sample_case)
    assert dep_trace["case_id"] == "TEST-LOCAL-01"
    assert dep_trace.get("dry_run") is True
    assert "decision" in dep_trace

    # 3. Cost cap abort check: pre-run cost estimation exceeding cap raises RuntimeError
    with pytest.raises(RuntimeError, match="Estimated run cost .* exceeds configured cost cap"):
        run_evaluation(
            target="local",
            split="dev",
            limit=20,
            cost_cap=0.0001,  # Excessively low cap should trigger abort
            dry_run=False,
        )


def test_trace_telemetry_schema_and_cost():
    # REQ-EVAL-005
    local_runner = LocalAgentRunner()
    case = {
        "case_id": "TEST-TRACE-01",
        "message": "Where is my package ORD-10001?",
        "customer_id": "cust_1001",
        "demo_user": "1001",
        "expected_decision": "auto_rag",
        "expected_escalate": False,
        "expected_priority": "P2_STANDARD",
        "is_strict_angry_urgent": False,
    }
    trace = local_runner.run_case(case)

    # Mandatory schema keys
    required_keys = [
        "case_id",
        "input_message",
        "decision",
        "tiers_used",
        "escalate",
        "priority",
        "queue",
        "ticket_id",
        "answer",
        "citations",
        "customer_verified",
        "latency_ms",
        "stage_latencies_ms",
        "token_usage",
        "cost_usd",
        "predicted_intent",
    ]
    for key in required_keys:
        assert key in trace, f"Missing telemetry trace key: '{key}'"

    assert isinstance(trace["tiers_used"], list)
    assert isinstance(trace["stage_latencies_ms"], dict)
    assert "total" in trace["stage_latencies_ms"]
    assert trace["cost_usd"] >= 0.0

    # Token cost calculation sanity check
    cost = compute_token_cost(input_tokens=1000, output_tokens=200)
    assert 0.001 < cost < 0.002


def test_deployed_runner_mocked_wire_and_enrichment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sys
    import types

    # 1. Order disclosure detector distinguishes genuine record disclosure vs BOLA denial
    assert detect_order_disclosed(
        "I couldn't find order ORD-10002 under your account. Please check the order ID."
    ) is False
    assert detect_order_disclosed(
        "Please sign in to view the status of order ORD-10001."
    ) is False
    assert detect_order_disclosed(
        "Your order ORD-10001 (ApexGlide Wireless Ergonomic Mouse) has been delivered via USPS."
    ) is True

    # 2. Deployed response classifier handles calming acks, OOS, card block, and greetings
    esc_p1 = classify_deployed_response(
        "Thank you for contacting us. We have created ticket #TICK-999 for you and will respond within 15 minutes."
    )
    assert esc_p1["decision"] == "escalate"
    assert esc_p1["escalate"] is True
    assert esc_p1["queue"] == "human-urgent"
    assert esc_p1["priority"] == "P1_URGENT"
    assert esc_p1["ticket_id"] == "TICK-999"

    oos = classify_deployed_response(
        "Thank you for reaching out (ref #TICK-111). I am the Global Retail Hub customer support assistant and can only help with orders, shipping policies, returns and refunds, or account and payment questions."
    )
    assert oos["decision"] == "reject_oos"
    assert oos["escalate"] is False

    # 3. CorpusCitationMatcher produces verbatim quotes from v5 corpus docs
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "faq-returns-refunds-return-policy-and-process.txt").write_text(
        "# How do I return an item?\n\nAlso asked as:\n- return window?\n\n"
        "Most items may be returned within 30 days of delivery for a full refund.\n",
        encoding="utf-8",
    )
    matcher = CorpusCitationMatcher(tmp_path)
    cites = matcher.match_citations(
        query="What is your return window?",
        answer="Most items may be returned within 30 days of delivery for a full refund.",
        predicted_intent="returns_refunds",
    )
    assert len(cites) == 1
    assert cites[0]["doc_id"] == "faq-returns-refunds-return-policy-and-process"
    assert "30 days of delivery" in cites[0]["quote"]

    # 4. DeployedAgentRunner live path with sys.modules vertexai stub (no real vertexai import)
    created_calls = []
    stream_calls = []

    class FakeEngine:
        def create_session(self, **kwargs):
            created_calls.append(kwargs)
            assert "session_id" not in kwargs  # Agent Gateway ENFORCE invariant
            return {"id": "sess-srv-12345678"}

        def stream_query(self, **kwargs):
            stream_calls.append(kwargs)
            return [
                {"content": {"parts": [{"text": "Most items may be returned within 30 days of delivery."}]}}
            ]

    fake_vertexai = types.ModuleType("vertexai")
    fake_vertexai.init = lambda **kw: None
    fake_ae = types.ModuleType("vertexai.agent_engines")
    fake_ae.get = lambda name: FakeEngine()
    fake_vertexai.agent_engines = fake_ae
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.agent_engines", fake_ae)

    import scripts.deploy_agents.query_triage_agent as qta

    monkeypatch.setattr(qta, "resolve_demo_user_token", lambda u: f"fake-jwt-{u}")

    runner = DeployedAgentRunner(dry_run=False, faq_corpus_dir=tmp_path)
    trace = runner.run_case(
        {
            "case_id": "DEP-LIVE-01",
            "source": "retrieval_v1",
            "stratum": "faq",
            "demo_user": "1001",
            "message": "What is your return window?",
            "expected_decision": "auto_rag",
            "expected_escalate": False,
            "expected_intent": "returns",
            "gold_articles": ["faq-returns-refunds-return-policy-and-process"],
        }
    )
    assert len(created_calls) == 1
    assert created_calls[0]["state"] == {"customer_id_token": "fake-jwt-1001"}
    assert stream_calls[0]["session_id"] == "sess-srv-12345678"
    assert trace["session_id"] == "sess-srv-12345678"
    assert trace["decision"] == "auto_rag"
    assert trace["citations"][0]["doc_id"] == "faq-returns-refunds-return-policy-and-process"

    # 5. Cloud Logging enrichment populates predicted_intent, tiers_used, and server_turn
    enriched_n = enrich_deployed_traces(
        [trace],
        citation_matcher=matcher,
        settle_seconds=0.0,
        log_fetcher=lambda sids, since: {
            "sess-srv-12345678": {
                "tier": "tier1_router_s1",
                "predicted_intent": "returns_refunds",
                "decision": "auto_rag",
                "queue": "none",
                "ticket_id": "TICK-REAL-01",
                "server_latency_ms": 845.2,
                "traceparent": "00-abc-def-01",
            }
        },
    )
    assert enriched_n == 1
    assert trace["tiers_used"] == ["tier1_router_s1"]
    assert trace["predicted_intent"] == "returns_refunds"
    assert trace["ticket_id"] == "TICK-REAL-01"
    assert trace["stage_latencies_ms"]["server_turn"] == 845.2
