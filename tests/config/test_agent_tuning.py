"""Targeted unit tests for ticket_router.config.agent_tuning pure logic.

# REQ-CICD-010: Line and Branch Test Coverage Enforcement in CI.
# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds.
# REQ-EVAL-008: Controlled Hill-Climbing Protocol & Config Registry.
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from ticket_router.config import agent_tuning


def test_agent_tuning_load_yaml_simple(tmp_path: Path):
    """Verify load_yaml_simple correctly parses YAML without PyYAML fallback dependency."""
    # Missing file returns {}
    assert agent_tuning.load_yaml_simple(tmp_path / "missing.yaml") == {}

    test_yaml = tmp_path / "tuning.yaml"
    test_yaml.write_text(
        """
# Top-level comment
router:
  tau_esc: 0.65
  tau_low: 0.20
  tau_intent: 0.85
prompts:
  tier2_router_version: "v3"
  faq_answer_version: "v1"
retrieval:
  top_k: 8
  rerank_enabled: true
  citation_strictness: 0.80
""",
        encoding="utf-8",
    )

    data = agent_tuning.load_yaml_simple(test_yaml)
    assert data["router"]["tau_esc"] == 0.65
    assert data["router"]["tau_low"] == 0.20
    assert data["retrieval"]["top_k"] == 8
    assert data["retrieval"]["rerank_enabled"] is True


def test_agent_tuning_deep_merge_and_overlay(tmp_path: Path):
    """Verify _deep_merge_dict and overlay loading."""
    base = {"a": 1, "nested": {"x": 10, "y": 20}}
    override = {"b": 2, "nested": {"y": 99, "z": 100}}
    merged = agent_tuning._deep_merge_dict(base, override)
    assert merged == {"a": 1, "b": 2, "nested": {"x": 10, "y": 99, "z": 100}}

    cfg = agent_tuning.load_tuning_config(
        overlay={"router": {"tau_esc": 0.90}, "retrieval": {"top_k": 12}}
    )
    assert cfg.tau_esc == 0.90
    assert cfg.top_k == 12

    # Global cached access with overlay
    overlay_cfg = agent_tuning.get_tuning_config(overlay={"router": {"tau_esc": 0.42}})
    assert overlay_cfg.tau_esc == 0.42

    # Global reload
    reloaded_cfg = agent_tuning.get_tuning_config(reload=True)
    assert isinstance(reloaded_cfg, agent_tuning.AgentTuningConfig)


def test_agent_tuning_prompt_generation():
    """Verify get_tier2_prompt and get_tier2_system_instruction version fallbacks."""
    # Tier 2 prompt v4
    p_v4 = agent_tuning.get_tier2_prompt("Need return assistance", version="v4")
    assert "Need return assistance" in p_v4

    # Tier 2 prompt v3
    p_v3 = agent_tuning.get_tier2_prompt("Check order status", version="v3")
    assert "Check order status" in p_v3

    # Unknown version fallback prompt
    p_fallback = agent_tuning.get_tier2_prompt("Random message", version="unknown_version_xyz")
    assert "Evaluate the following support ticket:" in p_fallback
    assert "Random message" in p_fallback

    # System instruction v4 vs v3 vs unknown
    inst_v4 = agent_tuning.get_tier2_system_instruction(version="v4")
    assert inst_v4 is not None
    assert "Global Retail Hub" in inst_v4

    inst_v3 = agent_tuning.get_tier2_system_instruction(version="v3")
    assert inst_v3 is not None
    assert "Global Retail Hub" in inst_v3

    inst_unknown = agent_tuning.get_tier2_system_instruction(version="v_non_existent")
    assert inst_unknown is None


def test_agent_tuning_faq_answer_prompt():
    """Verify get_faq_answer_prompt formats docs, orders, and feedback correctly."""
    docs = [{"doc_id": "doc1", "content": "Return window is 30 days."}]
    orders = {"order_id": "ord-123", "status": "DELIVERED"}

    # With list docs and dict orders
    prompt = agent_tuning.get_faq_answer_prompt(
        query="What is the return window?",
        docs_context=docs,
        order_context=orders,
        feedback="Prior answer lacked return window citation.",
        version="v2",
    )
    assert "What is the return window?" in prompt
    assert "doc1" in prompt
    assert "ord-123" in prompt
    assert "Correction Feedback:" in prompt

    # With string docs and empty orders
    prompt_str = agent_tuning.get_faq_answer_prompt(
        query="Where is my order?",
        docs_context="raw text context",
        order_context="",
        version="v2",
    )
    assert "raw text context" in prompt_str
    assert "Where is my order?" in prompt_str


def test_agent_tuning_load_yaml_simple_fallback_without_pyyaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify load_yaml_simple fallback parser when PyYAML is unavailable."""
    test_yaml = tmp_path / "fallback_tuning.yaml"
    test_yaml.write_text(
        """
# Top comment
router:
  tau_esc: 0.55
  tau_low: 0.12
  is_active: true
  is_disabled: false
  iterations: 42
  score: 3.14
  quoted_double: "value\\nwith\\nnewlines"
  quoted_single: 'single_quoted'
  bare_val: plain_text
prompts:
  tier2_router_version: "v4"
""",
        encoding="utf-8",
    )

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("Simulated missing PyYAML")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    data = agent_tuning.load_yaml_simple(test_yaml)
    assert data["router"]["tau_esc"] == 0.55
    assert data["router"]["tau_low"] == 0.12
    assert data["router"]["is_active"] is True
    assert data["router"]["is_disabled"] is False
    assert data["router"]["iterations"] == 42
    assert data["router"]["score"] == 3.14
    assert "value" in data["router"]["quoted_double"]
    assert data["router"]["quoted_single"] == "single_quoted"
    assert data["router"]["bare_val"] == "plain_text"
    assert data["prompts"]["tier2_router_version"] == "v4"
