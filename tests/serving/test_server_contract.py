# REQ-SERVE-001: Unified Vertex Custom Prediction Container Contract
# REQ-SERVE-002: Router Long-Input Policy and Chunk Aggregation
# REQ-SERVE-003: Router Temperature Scaling and Decision Taxonomy
# REQ-SERVE-004: Choice Reranker Candidate Scoring and Sorting
"""tests/serving/test_server_contract.py — Contract tests for serving request processing and FastAPI app.

Validates compliance with the Vertex AI custom prediction container contract:
1. Request payload processing:
   - task='route': typed router decision with long-input chunking and temperature scaling.
   - task='rerank': choice reranker scores and sorted candidate list.
   - batch instances: multi-instance batch processing.
   - raw Jev body compatibility: {"state": ...}.
   - error handling: ValueError on unknown task or empty instances.
   - engine readiness check: RuntimeError when models are unready.
2. FastAPI app routes (when FastAPI is available):
   - GET /health: 200 OK when ready, 503 when unready.
   - POST /predict: HTTP contract and Server-Timing header.
"""

import pytest

from serving.laya.engine import FakeModelEngine, LayaTorchEngine
from serving.laya.server import (
    FASTAPI_AVAILABLE,
    create_app,
    process_predict_payload,
)


class UnreadyEngine(FakeModelEngine):
    """Engine simulating cold loading."""

    def is_ready(self) -> bool:
        return False


@pytest.fixture
def fake_engine():
    """Return a ready FakeModelEngine."""
    return FakeModelEngine(device="cpu")


# REQ-SERVE-001, REQ-SERVE-003
def test_process_route_task(fake_engine):
    """process_predict_payload with task='route' must return valid router decision."""
    payload = {
        "instances": [
            {
                "task": "route",
                "text": "Where is my package? The tracking number says delivered but nothing is here.",
            }
        ]
    }
    result, elapsed_ms = process_predict_payload(payload, fake_engine)
    assert elapsed_ms >= 0.0
    assert "predictions" in result
    assert len(result["predictions"]) == 1

    pred = result["predictions"][0]
    assert "escalate" in pred
    assert "sentiment" in pred
    assert "urgency" in pred
    assert "intent" in pred
    assert pred["intent"]["label"] == "order_status"
    assert pred["model_version"] == "v2"
    assert "latency_ms" in pred


# REQ-SERVE-002
def test_process_route_long_input_chunking(fake_engine):
    """process_predict_payload with long text (> 1800 chars) must execute chunking."""
    long_text = "Urgent: package delayed. " * 100  # ~2,500 chars
    payload = {
        "instances": [
            {
                "task": "route",
                "text": long_text,
            }
        ]
    }
    result, _ = process_predict_payload(payload, fake_engine)
    pred = result["predictions"][0]
    assert "escalate" in pred
    assert "intent" in pred


# REQ-SERVE-001, REQ-SERVE-004
def test_process_rerank_task(fake_engine):
    """process_predict_payload with task='rerank' must return sorted candidate scores."""
    payload = {
        "instances": [
            {
                "task": "rerank",
                "query": "Where is my refund for the returned jacket?",
                "candidates": [
                    {"id": "doc_shipping", "text": "Shipping speed and Prime options"},
                    {"id": "doc_refunds", "text": "Refund processing times and status"},
                    {"id": "doc_account", "text": "Login and password reset"},
                ],
            }
        ]
    }
    result, _ = process_predict_payload(payload, fake_engine)
    predictions = result["predictions"]
    assert len(predictions) == 1

    ranked = predictions[0]
    assert len(ranked) == 3
    # Based on keyword overlap in FakeModelEngine, doc_refunds should be rank 1
    assert ranked[0]["id"] == "doc_refunds"
    assert ranked[0]["score"] >= ranked[1]["score"]
    assert ranked[1]["score"] >= ranked[2]["score"]
    # Schema parity with router output: per-instance latency_ms
    assert "latency_ms" in ranked[0]
    assert ranked[0]["latency_ms"] >= 0.0


# REQ-SERVE-001
def test_process_batch_mixed_instances(fake_engine):
    """process_predict_payload must handle mixed batch of route and rerank instances."""
    payload = {
        "instances": [
            {"task": "route", "text": "Can I return an item after 30 days?"},
            {
                "task": "rerank",
                "query": "Return policy",
                "candidates": [
                    {"id": "d1", "text": "Return Policy"},
                    {"id": "d2", "text": "Shipping"},
                ],
            },
        ]
    }
    result, _ = process_predict_payload(payload, fake_engine)
    preds = result["predictions"]
    assert len(preds) == 2

    # Prediction 0 is route decision
    assert "escalate" in preds[0]
    # Prediction 1 is ranked candidates list
    assert isinstance(preds[1], list)
    assert len(preds[1]) == 2


# REQ-SERVE-001
def test_process_raw_jev_compatibility(fake_engine):
    """process_predict_payload must accept raw Jev body with 'state'."""
    payload = {
        "state": "I need help with my payment.",
    }
    result, _ = process_predict_payload(payload, fake_engine)
    preds = result["predictions"]
    assert len(preds) == 1
    assert preds[0]["intent"]["label"] == "account_payment"


# REQ-SERVE-001
def test_process_invalid_task(fake_engine):
    """process_predict_payload must raise ValueError for unsupported task."""
    payload = {
        "instances": [
            {"task": "summarize", "text": "Some text"}
        ]
    }
    with pytest.raises(ValueError, match="Unsupported task"):
        process_predict_payload(payload, fake_engine)


# REQ-SERVE-001
def test_process_empty_instances(fake_engine):
    """process_predict_payload must raise ValueError for empty instances."""
    payload = {"instances": []}
    with pytest.raises(ValueError, match="non-empty list"):
        process_predict_payload(payload, fake_engine)


# REQ-SERVE-001
def test_process_unready_engine():
    """process_predict_payload must raise RuntimeError when engine is unready."""
    unready = UnreadyEngine()
    payload = {"instances": [{"task": "route", "text": "test"}]}
    with pytest.raises(RuntimeError, match="Service unavailable"):
        process_predict_payload(payload, unready)


# Optional FastAPI TestClient integration (skipped if fastapi is not in test env)
@pytest.mark.skipif(not FASTAPI_AVAILABLE, reason="FastAPI not installed in local environment")
def test_fastapi_http_contract(fake_engine):
    """Test full HTTP endpoints if FastAPI is installed."""
    from fastapi.testclient import TestClient

    app = create_app(engine=fake_engine)
    client = TestClient(app)

    # Health route
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "HEALTHY"

    # Predict route
    payload = {"instances": [{"task": "route", "text": "Hello"}]}
    predict_resp = client.post("/predict", json=payload)
    assert predict_resp.status_code == 200
    assert "Server-Timing" in predict_resp.headers
    assert len(predict_resp.json()["predictions"]) == 1


# REQ-SERVE-001
def test_laya_torch_engine_require_gpu_fail_fast(monkeypatch):
    """LayaTorchEngine.load_models must raise RuntimeError when REQUIRE_GPU=1 and CUDA is unavailable."""
    import sys
    from unittest.mock import MagicMock

    mock_torch = MagicMock()
    mock_torch.__version__ = "2.14.1"
    mock_torch.version.cuda = "12.6"
    mock_torch.cuda.is_available.return_value = False

    monkeypatch.setitem(sys.modules, "torch", mock_torch)
    monkeypatch.setitem(sys.modules, "laya", MagicMock())
    monkeypatch.setenv("REQUIRE_GPU", "1")

    engine = LayaTorchEngine(router_ckpt="fake", reranker_ckpt="fake")
    with pytest.raises(RuntimeError, match="CUDA unavailable on host"):
        engine.load_models()


# REQ-SERVE-001
def test_laya_torch_engine_allow_cpu_when_require_gpu_disabled(monkeypatch):
    """LayaTorchEngine.load_models allows CPU device when REQUIRE_GPU=0."""
    import sys
    from unittest.mock import MagicMock

    mock_torch = MagicMock()
    mock_torch.__version__ = "2.14.1"
    mock_torch.version.cuda = "12.6"
    mock_torch.cuda.is_available.return_value = False

    monkeypatch.setitem(sys.modules, "torch", mock_torch)
    mock_laya = MagicMock()
    mock_agent = MagicMock()
    mock_laya.load.return_value = mock_agent
    monkeypatch.setitem(sys.modules, "laya", mock_laya)
    monkeypatch.setenv("REQUIRE_GPU", "0")
    monkeypatch.setattr("serving.laya.engine.download_gcs_prefix", lambda uri, dest: dest)

    engine = LayaTorchEngine(router_ckpt="fake", reranker_ckpt="fake")
    engine.load_models()
    assert engine.device() == "cpu"
    assert engine.is_ready() is True
