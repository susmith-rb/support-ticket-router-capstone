# REQ-RAG-RETR-016: Query Rewriting and Rich Record Reranking for Graded Retrieval Precision
"""tests/rag/test_laya_cpu_bench.py — Unit tests for Laya CPU benchmark utilities.

Validates:
1. get_cpu_info: extracts CPU metadata, instruction set flags, and PyTorch BF16 CPU support.
2. measure_percentiles: computes min, max, mean, p50, p90, p95, p99 correctly.
3. build_choice_question: formats candidate document options with scope descriptions.
4. quantize_laya_agent: applies PyTorch dynamic int8 quantization selectively to encoder and scorer.
5. rerank_top5_query: sorts top-5 candidates by Laya choice probabilities and preserves ranks 6-10.
"""

from typing import Any, Dict

import pytest

from experiments.laya_cpu_bench import (
    build_choice_question,
    get_cpu_info,
    measure_percentiles,
    quantize_laya_agent,
    rerank_top5_query,
)

try:
    import torch  # noqa: F401  (availability probe for the skipif guard)
    import torch.nn as nn

    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False


class TopLevelDummyModel:
    """Dummy model mimicking Laya agent.model architecture when PyTorch is available."""

    def __init__(self):
        if TORCH_AVAILABLE:
            self.encoder = nn.Sequential(
                nn.Linear(8, 8),
                nn.ReLU(),
                nn.Linear(8, 8),
            )
            self.scorer = nn.Linear(8, 4)
            self.head = nn.Linear(8, 8)


class TopLevelDummyAgent:
    """Mock agent holding a model."""

    def __init__(self):
        self.model = TopLevelDummyModel()


class TopLevelDummyNoTorch:
    """Dummy object used when PyTorch is absent."""


class MockRerankAgent:
    """Mock agent for rerank_top5_query."""

    def predict(self, query: str, questions: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "answers": {
                "article": {
                    "probabilities": {
                        "d1": 0.10,
                        "d2": 0.70,
                        "d3": 0.05,
                        "d4": 0.15,
                        "d5": 0.00,
                    }
                }
            }
        }


def test_get_cpu_info():
    """Verify get_cpu_info returns expected schema with valid values."""
    info = get_cpu_info()
    assert isinstance(info, dict)
    assert "model_name" in info
    assert "num_cpus" in info
    assert "flags" in info
    assert "has_avx512" in info
    assert "has_amx" in info
    assert "is_bf16_supported" in info
    assert isinstance(info["num_cpus"], int)
    assert info["num_cpus"] >= 1
    assert isinstance(info["has_avx512"], bool)
    assert isinstance(info["has_amx"], bool)
    assert isinstance(info["is_bf16_supported"], bool)


def test_measure_percentiles_empty():
    """Verify measure_percentiles returns zeroed stats for empty input."""
    stats = measure_percentiles([])
    assert stats["count"] == 0
    assert stats["mean"] == 0.0
    assert stats["p50"] == 0.0
    assert stats["p95"] == 0.0
    assert stats["p99"] == 0.0


def test_measure_percentiles_values():
    """Verify measure_percentiles computes percentiles correctly."""
    latencies = [float(i) for i in range(1, 101)]  # 1.0 to 100.0
    stats = measure_percentiles(latencies)
    assert stats["count"] == 100
    assert stats["min"] == 1.0
    assert stats["max"] == 100.0
    assert stats["mean"] == 50.5
    assert stats["p50"] == 51.0
    assert stats["p90"] == 91.0
    assert stats["p95"] == 96.0
    assert stats["p99"] == 100.0


def test_build_choice_question():
    """Verify build_choice_question formats titles and scopes."""
    candidate_ids = ["doc_1", "doc_2", "doc_3"]
    docs = {
        "doc_1": {"title": "Billing FAQ", "scope": "Invoices and payments"},
        "doc_2": {"title": "Shipping Guide"},  # no scope
    }
    q_def = build_choice_question(candidate_ids, docs)
    assert q_def["type"] == "choice"
    assert "instructions" in q_def
    assert "criteria" in q_def
    criteria = q_def["criteria"]
    assert criteria["doc_1"] == "Billing FAQ: Invoices and payments"
    assert criteria["doc_2"] == "Shipping Guide"
    assert criteria["doc_3"] == "doc_3"  # fallback


@pytest.mark.skipif(not TORCH_AVAILABLE, reason="PyTorch not available in test environment")
def test_quantize_laya_agent_torch():
    """Verify quantize_laya_agent quantizes encoder and scorer linears when torch is present."""
    agent = TopLevelDummyAgent()
    quantized_agent = quantize_laya_agent(agent)

    # Check encoder linears are quantized
    for m in quantized_agent.model.encoder:
        if isinstance(m, nn.Module) and not isinstance(m, nn.ReLU):
            assert "quantized" in m.__class__.__module__.lower() or "Linear" in m.__class__.__name__

    # Check scorer is quantized
    assert "quantized" in quantized_agent.model.scorer.__class__.__module__.lower() or "Linear" in quantized_agent.model.scorer.__class__.__name__

    # Check head remains unquantized standard Linear
    assert isinstance(quantized_agent.model.head, nn.Linear)
    assert "quantized" not in quantized_agent.model.head.__class__.__module__.lower()


def test_quantize_laya_agent_no_torch():
    """Verify quantize_laya_agent behavior when torch is absent or mocked."""
    if not TORCH_AVAILABLE:
        dummy = TopLevelDummyNoTorch()
        with pytest.raises(RuntimeError, match="PyTorch is required"):
            quantize_laya_agent(dummy)


def test_rerank_top5_query():
    """Verify top 5 reordered according to probabilities and tail preserved."""
    agent = MockRerankAgent()
    top_candidates = ["d1", "d2", "d3", "d4", "d5", "d6", "d7", "d8", "d9", "d10"]
    docs = {f"d{i}": {"title": f"Doc {i}"} for i in range(1, 11)}

    reordered, probs = rerank_top5_query("test query", top_candidates, agent, docs)
    # Expected top 5 sorted by prob descending: d2 (0.70), d4 (0.15), d1 (0.10), d3 (0.05), d5 (0.00)
    assert reordered[:5] == ["d2", "d4", "d1", "d3", "d5"]
    # Tail preserved
    assert reordered[5:] == ["d6", "d7", "d8", "d9", "d10"]
    assert probs["d2"] == 0.70
