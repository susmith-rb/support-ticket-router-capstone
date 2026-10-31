"""Tests for Gemini 429 fault-injection hook and degraded human escalation.

# REQ-CHAOS-001: Chaos Engineering Fault Injection, Probing, and Safe Restoration
# REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies
# REQ-AGENT-025: Gemini Call Deadlines, Hedged Requests, and Fallback
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from google.genai.errors import ClientError

from chaos.experiment_gemini_429 import Gemini429ChaosExperiment
from ticket_router.agents.triage_router.policy import RouterThresholds, TriagePolicy
from ticket_router.models.tier2_gemini import (
    CHAOS_SIMULATE_GEMINI_429_ENV,
    check_gemini_429_hook,
    create_tier2_invoker,
    invoke_tier2_gemini,
    invoke_tier3_gemini,
)
from ticket_router.retrieval.answer_client import GeminiAnswerClient

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_gemini_429_hook_off_no_raise(monkeypatch: pytest.MonkeyPatch):
    """Verify hook off -> no exception raised by check_gemini_429_hook."""
    # 1. Unset environment variable
    monkeypatch.delenv(CHAOS_SIMULATE_GEMINI_429_ENV, raising=False)
    check_gemini_429_hook()  # Must not raise

    # 2. Set to '0' or arbitrary non-'1' value
    monkeypatch.setenv(CHAOS_SIMULATE_GEMINI_429_ENV, "0")
    check_gemini_429_hook()  # Must not raise

    monkeypatch.setenv(CHAOS_SIMULATE_GEMINI_429_ENV, "false")
    check_gemini_429_hook()  # Must not raise


def test_gemini_429_hook_on_raises_client_error(monkeypatch: pytest.MonkeyPatch):
    """Verify hook on -> raises google.genai.errors.ClientError with code 429."""
    monkeypatch.setenv(CHAOS_SIMULATE_GEMINI_429_ENV, "1")

    # 1. Direct hook check
    with pytest.raises(ClientError) as exc_info:
        check_gemini_429_hook()
    assert exc_info.value.code == 429

    # 2. Tier 2 Gemini call before network call
    with pytest.raises(ClientError) as exc_info:
        invoke_tier2_gemini("Customer return question")
    assert exc_info.value.code == 429

    # 3. Tier 3 Gemini call before network call
    with pytest.raises(ClientError) as exc_info:
        invoke_tier3_gemini("Complex dispute query")
    assert exc_info.value.code == 429

    # 4. FaqResolver answer client before network call
    ans_client = GeminiAnswerClient(project="test-proj", location="us-central1")
    with pytest.raises(ClientError) as exc_info:
        ans_client.generate_answer(
            query="What is the return window?",
            retrieved_docs=[{"id": "doc1", "content": "Return window is 30 days."}],
        )
    assert exc_info.value.code == 429


def test_tier2_path_under_hook_degrades_to_human_escalation(monkeypatch: pytest.MonkeyPatch):
    """Verify Tier 2 path under hook -> degraded human escalation per REQ-AGENT-011."""
    monkeypatch.setenv(CHAOS_SIMULATE_GEMINI_429_ENV, "1")

    class _FailingS1Client:
        def route(self, text: str):
            raise RuntimeError("router-s1 service unavailable")

    # Router S1 is unavailable, triggering fallback to Tier 2 Flash
    fake_s1 = _FailingS1Client()
    tier2_invoker = create_tier2_invoker()

    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        s1_client=fake_s1,
        tier2_invoker=tier2_invoker,
    )

    decision = policy.evaluate(
        "I need help with a broken shipment",
        session_id="sess_chaos_429",
        customer_id="cust_chaos_429",
    )

    # Per REQ-AGENT-011: must gracefully degrade to human escalation without failing the turn
    assert decision.decision == "escalate"
    assert decision.tier == "fallback_degraded"
    assert decision.queue in ("human-standard", "human-urgent")
    assert decision.degraded is True
    assert "t2_error" in decision.details
    assert "429" in decision.details["t2_error"] or "RESOURCE_EXHAUSTED" in decision.details["t2_error"]


def test_gemini_429_chaos_experiment_execute_mode(tmp_path: Path):
    """Verify Gemini429ChaosExperiment execute mode sets env, runs probe, asserts degraded behavior, and restores."""
    exp = Gemini429ChaosExperiment(
        dry_run=False,
        execute=True,
        confirm_destructive=True,
        out_dir=tmp_path,
    )

    report = exp.run()
    assert report["status"] == "COMPLETED"
    assert report["phases"]["fault_injection"]["hook_value"] == "1"
    assert report["phases"]["probe"]["decision"] == "escalate"
    assert report["phases"]["probe"]["degraded"] is True
    assert report["phases"]["assertion"]["passed"] is True
    assert report["phases"]["restoration"]["status"] == "RESTORED"

    # Environment variable MUST be cleared after run
    assert os.environ.get(CHAOS_SIMULATE_GEMINI_429_ENV) is None


def test_gemini_429_never_in_production_manifests_or_terraform():
    """Static test asserting no deploy manifest, Terraform file, or CI config sets CHAOS_SIMULATE_GEMINI_429."""
    search_dirs = [
        REPO_ROOT / "deploy",
        REPO_ROOT / "cicd",
    ]

    for s_dir in search_dirs:
        for file_path in s_dir.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix in (".tf", ".yaml", ".yml", ".json", ".sh", ".tfvars"):
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                assert "CHAOS_SIMULATE_GEMINI_429" not in content, (
                    f"Production manifest or CI config {file_path} must never reference {CHAOS_SIMULATE_GEMINI_429_ENV}"
                )

