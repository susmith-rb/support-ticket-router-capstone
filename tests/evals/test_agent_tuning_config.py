"""Tests for centralized agent tuning registry, hill-climb protocol, and pre-registered test audit gate.

# REQ-EVAL-008: Controlled Hill-Climbing Protocol & Config Registry
# REQ-EVAL-009: Pre-Registered Final TEST Evaluation Audit Gate
"""

from __future__ import annotations

import json
from pathlib import Path

from ticket_router.config.agent_tuning import (
    AgentTuningConfig,
    get_faq_answer_prompt,
    get_tier2_prompt,
    get_tuning_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HILL_CLIMB_DIR = REPO_ROOT / "evals" / "hill_climb"
HILL_CLIMB_LOG = HILL_CLIMB_DIR / "log.jsonl"
HILL_CLIMB_README = HILL_CLIMB_DIR / "README.md"
TEST_RUNS_LOG = REPO_ROOT / "evals" / "results" / "test_runs.log"


def test_agent_tuning_config_and_hill_climb_invariants():
    # REQ-EVAL-008
    # 1. Default config matches production baseline (zero behavior change)
    cfg = get_tuning_config(reload=True)
    assert isinstance(cfg, AgentTuningConfig)
    assert cfg.tau_esc == 0.35
    assert cfg.tau_low == 0.15
    assert cfg.tau_intent == 0.70
    assert cfg.tier2_router_version == "v1"
    assert cfg.faq_answer_version == "v1"
    assert cfg.search_config == "intent_soft_noisy_0.2"
    assert cfg.top_k == 5
    assert cfg.rerank_enabled is False
    assert cfg.citation_strictness == 0.70

    # 2. Golden test: rendering v1 templates with fixed inputs == exact strings HEAD code produced
    fixed_msg = "Customer says package ORD-999 is damaged and needs immediate replacement."
    expected_tier2_head = (
        f"Evaluate the following support ticket:\n{fixed_msg}\n"
        "Return JSON with: escalate (bool), escalate_prob (float), "
        "sentiment (str: neutral/angry), urgency (str: normal/immediate), "
        "intent (str: billing/shipping/refund/technical/other), intent_prob (float)."
    )
    rendered_tier2 = get_tier2_prompt(fixed_msg, version="v1")
    assert rendered_tier2 == expected_tier2_head, (
        f"Tier-2 v1 prompt does not match HEAD byte-for-byte:\nRendered:\n{rendered_tier2}\nExpected:\n{expected_tier2_head}"
    )

    fixed_query = "What is the return window for electronics?"
    fixed_docs = [{"doc_id": "faq_001", "content": "Electronics may be returned within 14 days of purchase."}]
    fixed_order = {"order_id": "ORD-12345", "status": "DELIVERED"}
    fixed_docs_str = json.dumps(fixed_docs, indent=2)
    fixed_order_str = json.dumps(fixed_order, indent=2)

    # 2a. FAQ answer prompt without feedback
    expected_faq_head_no_fb = (
        f"Customer Inquiry: {fixed_query}\n\n"
        f"Retrieved Documents:\n{fixed_docs_str}\n\n"
        f"Order Context:\n{fixed_order_str}\n\n"
        "Provide a helpful answer grounded strictly in the provided documents.\n"
        "Return valid JSON strictly matching:\n"
        '{\n  "answer": "your answer here",\n  "citations": [{"doc_id": "document_id", "quote": "verbatim text quotation"}]\n}\n'
        "Every answer MUST include at least one citation with a verbatim quotation from the retrieved documents."
    )
    rendered_faq_no_fb = get_faq_answer_prompt(
        query=fixed_query,
        docs_context=fixed_docs_str,
        order_context=fixed_order_str,
        feedback=None,
        version="v1",
    )
    assert rendered_faq_no_fb == expected_faq_head_no_fb, (
        f"FAQ v1 prompt (no feedback) does not match HEAD byte-for-byte:\nRendered:\n{rendered_faq_no_fb}\nExpected:\n{expected_faq_head_no_fb}"
    )

    # 2b. FAQ answer prompt with feedback
    fixed_fb = "Citation validation rejected previous response: uncited_answer (Missing quote). You MUST supply at least one citation with a quote verbatim present in the retrieved articles."
    expected_faq_head_with_fb = (
        f"Customer Inquiry: {fixed_query}\n\n"
        f"Retrieved Documents:\n{fixed_docs_str}\n\n"
        f"Order Context:\n{fixed_order_str}\n\n"
        f"Correction Feedback:\n{fixed_fb}\n\n"
        "Provide a helpful answer grounded strictly in the provided documents.\n"
        "Return valid JSON strictly matching:\n"
        '{\n  "answer": "your answer here",\n  "citations": [{"doc_id": "document_id", "quote": "verbatim text quotation"}]\n}\n'
        "Every answer MUST include at least one citation with a verbatim quotation from the retrieved documents."
    )
    rendered_faq_with_fb = get_faq_answer_prompt(
        query=fixed_query,
        docs_context=fixed_docs_str,
        order_context=fixed_order_str,
        feedback=fixed_fb,
        version="v1",
    )
    assert rendered_faq_with_fb == expected_faq_head_with_fb, (
        f"FAQ v1 prompt (with feedback) does not match HEAD byte-for-byte:\nRendered:\n{rendered_faq_with_fb}\nExpected:\n{expected_faq_head_with_fb}"
    )

    # 3. Hill climb log and documentation exist
    assert HILL_CLIMB_LOG.is_file(), f"Missing {HILL_CLIMB_LOG}"
    assert HILL_CLIMB_README.is_file(), f"Missing {HILL_CLIMB_README}"

    with open(HILL_CLIMB_LOG, "r", encoding="utf-8") as f:
        log_entries = [json.loads(line) for line in f if line.strip()]

    # Log may be empty until the measured DEV baseline is recorded; every
    # entry present must carry the audit fields (no metric-less "keep").
    required = {"attempt_id", "timestamp", "knob_changed", "old_value",
                "new_value", "decision", "commit_hash"}
    for entry in log_entries:
        assert required <= set(entry), f"Incomplete hill-climb entry: {entry}"
        assert entry["decision"] in {"baseline", "keep", "revert"}
        if entry["decision"] in {"baseline", "keep"}:
            assert entry.get("metrics"), f"Kept attempt lacks metrics: {entry}"

    readme_text = HILL_CLIMB_README.read_text(encoding="utf-8")
    assert "DEV Only Partition" in readme_text
    assert "Single-Knob Changes" in readme_text
    assert "auto_resolution_rate" in readme_text
    assert "Strict angry/urgent escalation recall" in readme_text


def test_pre_registered_test_gate_format():
    # REQ-EVAL-009
    assert TEST_RUNS_LOG.is_file(), f"Missing {TEST_RUNS_LOG}"
    log_text = TEST_RUNS_LOG.read_text(encoding="utf-8")
    lines = [line.strip() for line in log_text.splitlines() if line.strip()]

    assert len(lines) >= 1
    # Check that audit line adheres to rag/results/v4 format:
    # <timestamp> | <TAG> | configs=... | queries=... | n=... | ...
    gate_line = lines[0]
    parts = [p.strip() for p in gate_line.split("|")]
    assert len(parts) >= 5, f"Malformed test audit line: {gate_line}"
    assert parts[1] == "FINAL_TEST_RUN_PRE_REGISTERED"
    assert "configs=" in parts[2]
    assert "queries=evals/data/agent_e2e_v1/test.jsonl" in parts[3]
    assert "n=534" in parts[4]
    assert "operator=conductor" in parts[-1]
