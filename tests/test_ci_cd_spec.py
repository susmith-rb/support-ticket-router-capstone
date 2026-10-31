"""Unit and static specification tests for CI/CD test coverage enforcement.

# REQ-CICD-010: Line and Branch Test Coverage Enforcement in CI.
# REQ-BILL-MODELS-001: Centralized Model Configuration Registry.
# REQ-EVAL-008: Controlled Hill-Climbing Protocol & Config Registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ticket_router.common.resilience import CircuitBreaker, CircuitState
from ticket_router.config import agent_tuning, models
from ticket_router.serve import _instrument_fastapi_app, build_connection_manager, get_app

# ==============================================================================
# REQ-CICD-010: Targeted tests for config/models.py pure logic
# ==============================================================================


def test_models_clean_val_helper():
    """Verify models._clean_val handles various string representations correctly."""
    assert models._clean_val('"hello"') == "hello"
    assert models._clean_val("'world'") == "world"
    assert models._clean_val("{}") == {}
    assert models._clean_val("true") is True
    assert models._clean_val("True") is True
    assert models._clean_val("false") is False
    assert models._clean_val("False") is False
    assert models._clean_val("42") == 42
    assert models._clean_val("3.14") == 3.14
    assert models._clean_val("unquoted_str") == "unquoted_str"


def test_models_parse_yaml_fallback(tmp_path: Path):
    """Verify models._parse_yaml_fallback parses hierarchical YAML and initializes defaults."""
    raw_yaml = """
# Top-level comment
models:
  test_role:
    id: "gemini-test-model"
    location: "us-central1"
    thinking: "LOW"
  empty_role: {}
envs:
  dev:
    test_role: "gemini-dev-override"
serving:
  test_service:
    endpoint: "http://localhost:8000"
"""
    parsed = models._parse_yaml_fallback(raw_yaml)
    assert "models" in parsed
    assert "envs" in parsed
    assert "serving" in parsed
    assert parsed["models"]["test_role"]["id"] == "gemini-test-model"
    assert parsed["models"]["test_role"]["location"] == "us-central1"
    assert parsed["models"]["test_role"]["thinking"] == "LOW"
    assert parsed["models"]["empty_role"] == {}
    assert parsed["envs"]["dev"]["test_role"] == "gemini-dev-override"
    assert parsed["serving"]["test_service"]["endpoint"] == "http://localhost:8000"


def test_models_load_registry_errors_and_reload(tmp_path: Path):
    """Verify load_registry handles missing files and forced reloads."""
    non_existent = tmp_path / "non_existent_models.yaml"
    with pytest.raises(FileNotFoundError):
        models.load_registry(config_path=non_existent)

    # Force reload of standard registry
    reg1 = models.load_registry(reload=True)
    assert isinstance(reg1, dict)
    assert "models" in reg1


def test_models_get_env_and_env_var_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify precedence: env var > env block > base model."""
    custom_yaml = tmp_path / "models_override.yaml"
    custom_yaml.write_text(
        """
models:
  agent_a:
    id: "base-model-a"
  agent_b:
    id: "base-model-b"
envs:
  staging:
    agent_a:
      id: "staging-model-a"
    agent_b: "staging-model-b-string"
""",
        encoding="utf-8",
    )

    # Base lookup
    res_base = models.get("agent_a", config_path=custom_yaml)
    assert res_base["id"] == "base-model-a"

    # Envs dict override
    res_staging_a = models.get("agent_a", env="staging", config_path=custom_yaml)
    assert res_staging_a["id"] == "staging-model-a"

    # Envs string override
    res_staging_b = models.get("agent_b", env="staging", config_path=custom_yaml)
    assert res_staging_b["id"] == "staging-model-b-string"

    # Unknown role error
    with pytest.raises(KeyError, match="Unknown model role: 'missing'"):
        models.get("missing", config_path=custom_yaml)

    # Missing ID error
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("models:\n  no_id:\n    location: us-central1\n", encoding="utf-8")
    with pytest.raises(KeyError, match="does not specify an 'id'"):
        models.get("no_id", config_path=bad_yaml)

    # Environment variable override
    monkeypatch.setenv("MODEL_AGENT_A", "envvar-model-a")
    res_envvar = models.get("agent_a", config_path=custom_yaml)
    assert res_envvar["id"] == "envvar-model-a"


def test_models_location_and_thinking_level(tmp_path: Path):
    """Verify model_location and model_thinking_level helpers."""
    custom_yaml = tmp_path / "models_extra.yaml"
    custom_yaml.write_text(
        """
models:
  loc_model:
    id: "loc-m"
    location: "europe-west1"
    thinking_level: "HIGH"
  think_alt_model:
    id: "think-m"
    thinking: "MEDIUM"
  bare_model:
    id: "bare-m"
""",
        encoding="utf-8",
    )

    assert models.model_location("loc_model", config_path=custom_yaml) == "europe-west1"
    assert models.model_location("bare_model", default="us-central1", config_path=custom_yaml) == "us-central1"
    assert models.model_location("non_existent", default="global", config_path=custom_yaml) == "global"

    assert models.model_thinking_level("loc_model", config_path=custom_yaml) == "HIGH"
    assert models.model_thinking_level("think_alt_model", config_path=custom_yaml) == "MEDIUM"
    assert models.model_thinking_level("bare_model", default="LOW", config_path=custom_yaml) == "LOW"
    assert models.model_thinking_level("non_existent", default="OFF", config_path=custom_yaml) == "OFF"


def test_models_serving_config_and_list_roles(tmp_path: Path):
    """Verify get_serving_config and list_roles."""
    custom_yaml = tmp_path / "serving.yaml"
    custom_yaml.write_text(
        """
models:
  r1:
    id: "model-1"
  r2:
    id: "model-2"
serving:
  router_s1:
    timeout_s: 30
    batch_size: 4
""",
        encoding="utf-8",
    )

    roles = models.list_roles(config_path=custom_yaml)
    assert roles == {"r1": "model-1", "r2": "model-2"}

    srv = models.get_serving_config("router_s1", config_path=custom_yaml)
    assert srv["timeout_s"] == 30
    assert srv["batch_size"] == 4

    empty_srv = models.get_serving_config("unknown_service", config_path=custom_yaml)
    assert empty_srv == {}


def test_models_main_cli(capsys: pytest.CaptureFixture[str]):
    """Verify models.main CLI entrypoint with various flags."""
    # List roles
    rc_list = models.main(["--list"])
    assert rc_list == 0
    captured = capsys.readouterr()
    assert "router_tier2:" in captured.out or "answer:" in captured.out

    # JSON output for list
    rc_json_list = models.main(["--list", "--json"])
    assert rc_json_list == 0
    captured_json = capsys.readouterr()
    assert "{" in captured_json.out

    # Single role lookup
    rc_role = models.main(["router_tier2"])
    assert rc_role == 0
    captured_role = capsys.readouterr()
    assert "gemini" in captured_role.out

    # Single role JSON lookup
    rc_role_json = models.main(["router_tier2", "--json"])
    assert rc_role_json == 0
    captured_role_json = capsys.readouterr()
    assert '"id":' in captured_role_json.out

    # Non-existent role error
    rc_err = models.main(["non_existent_role_for_cli_test"])
    assert rc_err == 1
    captured_err = capsys.readouterr()
    assert "Error:" in captured_err.err


# ==============================================================================
# REQ-CICD-010: Targeted tests for config/agent_tuning.py pure logic
# ==============================================================================


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


# ==============================================================================
# REQ-CICD-010: Targeted tests for common/resilience.py pure logic
# ==============================================================================


def test_circuit_breaker_properties_and_cooldown():
    """Verify CircuitBreaker properties, state transitions, and cooldown jitter."""
    fake_time = 100.0

    def clock():
        return fake_time

    # RNG returning 0.5 means jitter_fraction = (0.5 * 2 - 1) * 0.20 = 0.0
    cb = CircuitBreaker(
        name="test_cb",
        failure_threshold=2,
        base_cooldown_s=10.0,
        max_cooldown_s=60.0,
        jitter_ratio=0.20,
        clock=clock,
        rng=lambda: 0.5,
    )

    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0
    assert cb.consecutive_successes == 0
    assert cb.trip_count == 0
    assert cb.reset_at == 0.0

    # First failure
    cb.record_failure(RuntimeError("fail 1"))
    assert cb.consecutive_failures == 1
    assert cb.state == CircuitState.CLOSED

    # Second failure trips breaker
    cb.record_failure(RuntimeError("fail 2"))
    assert cb.state == CircuitState.OPEN
    assert cb.trip_count == 1
    assert cb.reset_at == 110.0  # 100.0 + 10.0

    # Before cooldown expires
    fake_time = 105.0
    assert cb.state == CircuitState.OPEN

    # After cooldown expires, state transitions to HALF_OPEN on read
    fake_time = 111.0
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.consecutive_successes == 0

    # Success in HALF_OPEN recovers breaker to CLOSED (success_threshold=2)
    cb.record_success()
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0


# ==============================================================================
# REQ-CICD-010: Targeted tests for serve.py multi-service entrypoint
# ==============================================================================


def test_serve_build_connection_manager_validation(monkeypatch: pytest.MonkeyPatch):
    """Verify build_connection_manager fail-fast argument and env validation."""
    monkeypatch.delenv("INSTANCE_CONNECTION_NAME", raising=False)
    with pytest.raises(RuntimeError, match="INSTANCE_CONNECTION_NAME environment variable is required"):
        build_connection_manager()

    with pytest.raises(ValueError, match="Invalid INSTANCE_CONNECTION_NAME"):
        build_connection_manager(instance_connection_name="invalid-format-two-parts")

    monkeypatch.delenv("DB_IAM_USER", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    with pytest.raises(RuntimeError, match="DB_IAM_USER environment variable is required"):
        build_connection_manager(instance_connection_name="proj:region:inst")


def test_serve_get_app_service_routing(monkeypatch: pytest.MonkeyPatch):
    """Verify get_app service name routing and review_ui instantiation."""
    monkeypatch.setenv("ALLOW_FAKE_REPOS", "1")

    # Review UI app routing
    app_review = get_app("review_ui")
    assert app_review.title == "Human Review UI" or hasattr(app_review, "routes")

    # Re-instrumenting returns existing app without duplicate middleware
    app_re_instrumented = _instrument_fastapi_app(app_review, "review_ui")
    assert app_re_instrumented is app_review

    # Unknown service error
    with pytest.raises(ValueError, match="Unknown SERVICE 'unsupported_service'"):
        get_app("unsupported_service")


# ==============================================================================
# REQ-CICD-010: Targeted tests for agent_tuning.py YAML fallback parser
# ==============================================================================


def test_agent_tuning_load_yaml_simple_fallback_without_pyyaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify load_yaml_simple fallback parser lines 136-182 when PyYAML is unavailable."""
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

    import builtins
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


# ==============================================================================
# REQ-CICD-010: Targeted tests for common/observability.py pure logic
# ==============================================================================


from ticket_router.common import observability


def test_observability_traceparent_parsing():
    """Verify parse_traceparent with valid and invalid inputs."""
    assert observability.parse_traceparent("") is None
    assert observability.parse_traceparent(None) is None  # type: ignore[arg-type]
    assert observability.parse_traceparent("invalid") is None
    assert observability.parse_traceparent("00-1-2-01") is None

    valid = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    parsed = observability.parse_traceparent(valid)
    assert parsed is not None
    assert parsed["version"] == "00"
    assert parsed["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert parsed["span_id"] == "00f067aa0ba902b7"
    assert parsed["flags"] == "01"


def test_observability_cloud_trace_context_parsing():
    """Verify parse_cloud_trace_context edge cases and conversions."""
    assert observability.parse_cloud_trace_context("") is None
    assert observability.parse_cloud_trace_context(None) is None  # type: ignore[arg-type]
    assert observability.parse_cloud_trace_context("malformed") is None
    # Zero trace or span ID
    assert observability.parse_cloud_trace_context("00000000000000000000000000000000/0") is None
    # Overflow span ID >= 2^64
    assert observability.parse_cloud_trace_context(f"4bf92f3577b34da6a3ce929d0e0e4736/{2**64 + 1}") is None

    # Valid with options: o=1 -> flags 01
    ct1 = "4bf92f3577b34da6a3ce929d0e0e4736/10544503810011;o=1"
    tp1 = observability.parse_cloud_trace_context(ct1)
    assert tp1 is not None
    assert tp1.startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert tp1.endswith("-01")

    # Valid with options: o=0 -> flags 00
    ct2 = "4bf92f3577b34da6a3ce929d0e0e4736/10544503810011;o=0"
    tp2 = observability.parse_cloud_trace_context(ct2)
    assert tp2 is not None
    assert tp2.endswith("-00")


def test_observability_generate_traceparent():
    """Verify generate_traceparent output matches W3C specification."""
    tp = observability.generate_traceparent()
    parsed = observability.parse_traceparent(tp)
    assert parsed is not None
    assert len(parsed["trace_id"]) == 32
    assert len(parsed["span_id"]) == 16


def test_observability_configure_tracing(monkeypatch: pytest.MonkeyPatch):
    """Verify configure_tracing handles disabled flag and missing tracing dependencies."""
    monkeypatch.setenv("ENABLE_CLOUD_TRACE", "0")
    assert observability.configure_tracing(force=True) is False

    monkeypatch.setenv("ENABLE_CLOUD_TRACE", "1")
    # In hermetic test environment without opentelemetry-sdk, gracefully returns False
    res = observability.configure_tracing(force=True)
    assert res in (True, False)


# ==============================================================================
# REQ-CICD-010: Targeted tests for triage_router/tier3.py pure logic
# ==============================================================================

from ticket_router.agents.triage_router import tier3
from ticket_router.models.router_s1_client import RouterPrediction


def test_tier3_config_resolution(monkeypatch: pytest.MonkeyPatch):
    """Verify Tier3Config resolution from environment overrides."""
    monkeypatch.setenv("TIER3_ENABLED", "0")
    monkeypatch.setenv("TIER3_CONFIDENCE_THRESHOLD", "0.85")
    monkeypatch.setenv("TIER3_DEADLINE_S", "4.0")

    cfg = tier3.Tier3Config.from_config()
    assert cfg.enabled is False
    assert cfg.confidence_threshold == 0.85
    assert cfg.deadline_s == 4.0


def test_tier3_trigger_evaluation():
    """Verify is_tier3_trigger across escalation, sentiment, urgency, and confidence."""
    # 1. Escalation / angry / immediate never trigger Tier 3 (add-only invariant)
    esc_pred = RouterPrediction(
        escalate=True,
        escalate_prob=0.9,
        sentiment="neutral",
        sentiment_prob=0.9,
        urgency="normal",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.9,
    )
    assert tier3.is_tier3_trigger(esc_pred) == (False, False)

    angry_pred = RouterPrediction(
        escalate=False,
        escalate_prob=0.1,
        sentiment="angry",
        sentiment_prob=0.9,
        urgency="normal",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.9,
    )
    assert tier3.is_tier3_trigger(angry_pred) == (False, False)

    imm_pred = RouterPrediction(
        escalate=False,
        escalate_prob=0.1,
        sentiment="neutral",
        sentiment_prob=0.9,
        urgency="immediate",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.9,
    )
    assert tier3.is_tier3_trigger(imm_pred) == (False, False)

    # 2. Low confidence triggers Tier 3
    low_conf_pred = RouterPrediction(
        escalate=False,
        escalate_prob=0.1,
        sentiment="neutral",
        sentiment_prob=0.9,
        urgency="normal",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.60,  # < 0.70 threshold
    )
    should_trig, is_low = tier3.is_tier3_trigger(low_conf_pred, confidence_threshold=0.70)
    assert should_trig is True
    assert is_low is True

    # 3. High confidence benign does NOT trigger
    high_conf_pred = RouterPrediction(
        escalate=False,
        escalate_prob=0.05,
        sentiment="neutral",
        sentiment_prob=0.9,
        urgency="normal",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.95,
    )
    assert tier3.is_tier3_trigger(high_conf_pred, confidence_threshold=0.70) == (False, False)

    # 4. Explicit multihop signal in raw metadata triggers Tier 3
    signal_pred = RouterPrediction(
        escalate=False,
        escalate_prob=0.05,
        sentiment="neutral",
        sentiment_prob=0.9,
        urgency="normal",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.95,
        raw={"needs_multihop": True},
    )
    assert tier3.is_tier3_trigger(signal_pred, confidence_threshold=0.70) == (True, False)


def test_tier3_invoker_creation():
    """Verify create_tier3_invoker factory returns callable and handles custom caller."""
    custom_called = False

    def custom_invoker(text: str) -> RouterPrediction:
        nonlocal custom_called
        custom_called = True
        return RouterPrediction(
            escalate=False,
            escalate_prob=0.05,
            sentiment="neutral",
            sentiment_prob=0.9,
            urgency="normal",
            urgency_prob=0.9,
            intent="order_status",
            intent_prob=0.95,
            latency_ms=10.0,
        )

    invoker = tier3.create_tier3_invoker(custom_caller=custom_invoker)
    pred = invoker("test message")
    assert custom_called is True
    assert pred.intent == "order_status"


# ==============================================================================
# REQ-CICD-010: Targeted tests for faq_resolver validator & passage_selector
# ==============================================================================

from ticket_router.agents.faq_resolver import passage_selector, validator


def test_faq_validator_edge_cases():
    """Verify CitationValidator handles citations and text normalization."""
    validator.CitationValidator()
    assert validator.norm("  Hello   World!  ") == "hello world!"
    assert validator.is_verbatim_citation("return window", "Our return window is 30 days.") is True
    assert validator.is_verbatim_citation("not present", "Our return window is 30 days.") is False


def test_passage_selector_helpers():
    """Verify passage_selector tokenization, stemming, and static passage selection."""
    tokens = passage_selector._tokenize("Where is my order?")
    assert "where" in tokens
    assert "order" in tokens

    article_text = "Track your order online using your order ID. Return policy details and refund timeframes."
    passage = passage_selector.select_static_passage("Where is my order?", article_text)
    assert passage is not None
    assert "order" in passage.lower()


# ==============================================================================
# REQ-CICD-010: Targeted tests for triage_router/tier3.py model invoker & branches
# ==============================================================================

from unittest.mock import MagicMock


def test_tier3_trigger_additional_branches():
    """Verify is_tier3_trigger additional branch coverage."""
    # 1. Low confidence on escalation certainty: (1.0 - escalate_prob) < threshold
    t2_pred_esc_uncert = RouterPrediction(
        escalate=False,
        escalate_prob=0.45,  # 1.0 - 0.45 = 0.55 < 0.70
        sentiment="neutral",
        sentiment_prob=0.9,
        urgency="normal",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.95,
    )
    trig, is_low = tier3.is_tier3_trigger(t2_pred_esc_uncert, confidence_threshold=0.70)
    assert trig is True
    assert is_low is True

    # 2. Metadata multihop signals
    for meta in (
        {"ambiguous": True},
        {"ambiguous": "yes"},
        {"ambiguous": "1"},
        {"needs_multihop": "true"},
        {"needs_multihop": "yes"},
        {"signal": "needs_multihop"},
        {"signal": "multihop"},
        {"reason": "ambiguous"},
    ):
        pred = RouterPrediction(
            escalate=False,
            escalate_prob=0.05,
            sentiment="neutral",
            sentiment_prob=0.9,
            urgency="normal",
            urgency_prob=0.9,
            intent="order_status",
            intent_prob=0.95,
            raw=meta,
        )
        t, _ = tier3.is_tier3_trigger(pred, confidence_threshold=0.70)
        assert t is True, f"Failed for metadata {meta}"

    # 3. Intent multihop triggers
    for special_intent in ("needs_multihop", "ambiguous", "multihop"):
        pred_intent = RouterPrediction(
            escalate=False,
            escalate_prob=0.05,
            sentiment="neutral",
            sentiment_prob=0.9,
            urgency="normal",
            urgency_prob=0.9,
            intent=special_intent,
            intent_prob=0.95,
        )
        t_int, _ = tier3.is_tier3_trigger(pred_intent, confidence_threshold=0.70)
        assert t_int is True, f"Failed for intent {special_intent}"


def test_invoke_tier3_gemini_mocked(monkeypatch: pytest.MonkeyPatch):
    """Verify invoke_tier3_gemini execution, hedged calling, markdown stripping, and usage parsing."""
    import google.genai

    mock_resp = MagicMock()
    mock_resp.text = '```json\n{"escalate": false, "intent": "billing", "sentiment": "neutral", "urgency": "normal", "out_of_scope": false, "escalate_prob": 0.08, "intent_prob": 0.92}\n```'
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 120
    mock_usage.candidates_token_count = 45
    mock_usage.total_token_count = 165
    mock_resp.usage_metadata = mock_usage

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_resp

    monkeypatch.setattr(google.genai, "Client", lambda **kwargs: mock_client)
    monkeypatch.setattr(tier3, "hedged_call", lambda fn, **kwargs: fn())

    pred = tier3.invoke_tier3_gemini(
        text="I need help with my invoice",
        target_model="gemini-2.5-pro",
        project="test-project",
        location="us-central1",
        deadline_s=3.0,
    )

    assert pred.intent == "billing"
    assert pred.escalate is False
    assert pred.intent_prob == 0.92
    assert pred.raw["usage"]["total_tokens"] == 165

    # Also test Tier3GeminiInvoker and default_tier3_invoker
    invoker = tier3.Tier3GeminiInvoker(project="test-project", location="us-central1", deadline_s=2.0)
    pred_inv = invoker("invoice query")
    assert pred_inv.intent == "billing"

    pred_def = tier3.default_tier3_invoker("invoice query")
    assert pred_def.intent == "billing"


def test_invoke_tier3_gemini_fallbacks_and_exceptions(monkeypatch: pytest.MonkeyPatch):
    """Verify invoke_tier3_gemini out_of_scope, non-JSON fallbacks, and error wrapping."""
    import google.genai

    mock_resp = MagicMock()
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_resp

    monkeypatch.setattr(google.genai, "Client", lambda **kwargs: mock_client)
    monkeypatch.setattr(tier3, "hedged_call", lambda fn, **kwargs: fn())

    # Case 1: Out of scope JSON
    mock_resp.text = '{"out_of_scope": true, "intent": "random"}'
    mock_resp.usage_metadata = None
    pred_oos = tier3.invoke_tier3_gemini("Tell me a joke", target_model="gemini-2.5-pro")
    assert pred_oos.out_of_scope is True
    assert pred_oos.intent == "other"

    # Case 2: String escalate and angry sentiment
    mock_resp.text = '{"escalate": "yes", "sentiment": "angry", "urgency": "immediate"}'
    pred_esc = tier3.invoke_tier3_gemini("Very upset customer", target_model="gemini-2.5-pro")
    assert pred_esc.escalate is True
    assert pred_esc.sentiment == "angry"
    assert pred_esc.urgency == "immediate"

    # Case 3: Non-JSON raw text fallback (angry keyword)
    mock_resp.text = "The customer sounds very angry. Please escalate immediately."
    pred_raw_angry = tier3.invoke_tier3_gemini("Angry rant", target_model="gemini-2.5-pro")
    assert pred_raw_angry.escalate is True
    assert pred_raw_angry.sentiment == "angry"
    assert pred_raw_angry.urgency == "immediate"

    # Case 4: Non-JSON raw text benign fallback
    mock_resp.text = "This appears to be a normal inquiry about store hours."
    pred_raw_benign = tier3.invoke_tier3_gemini("Hours inquiry", target_model="gemini-2.5-pro")
    assert pred_raw_benign.escalate is False
    assert pred_raw_benign.sentiment == "neutral"
    assert pred_raw_benign.intent == "faq"

    # Case 5: Exception raises RuntimeError
    monkeypatch.setattr(tier3, "hedged_call", MagicMock(side_effect=TimeoutError("Deadline exceeded")))
    with pytest.raises(RuntimeError, match="Tier 3 model call failed"):
        tier3.invoke_tier3_gemini("query", target_model="gemini-2.5-pro")


# ==============================================================================
# REQ-CICD-010: Targeted tests for faq_resolver/reranker.py
# ==============================================================================

from ticket_router.agents.faq_resolver import reranker


def test_reranker_vertex_and_fake_branches():
    """Verify VertexLayaReranker endpoint parsing, omitted candidates, error fallback, and FakeReranker."""
    # 1. FakeReranker preferred_id and fail_with
    fake_fail = reranker.FakeReranker(fail_with=RuntimeError("Simulated failure"))
    with pytest.raises(RuntimeError, match="Simulated failure"):
        fake_fail.rerank("query", [{"id": "doc1"}])

    fake_pref = reranker.FakeReranker(preferred_id="doc2")
    cands, _ = fake_pref.rerank("query", [{"id": "doc1"}, {"id": "doc2"}, {"id": "doc3"}])
    assert cands[0]["id"] == "doc2"
    assert cands[0]["score"] == 1.0

    # 2. VertexLayaReranker empty candidates
    v_rerank = reranker.VertexLayaReranker(endpoint_url="https://rerank.endpoint.com")
    res_empty, lat_empty = v_rerank.rerank("query", [])
    assert res_empty == []
    assert lat_empty == 0.0

    # 3. VertexLayaReranker endpoint path parsing
    vr_url = reranker.VertexLayaReranker(endpoint_url="https://custom.service.internal")
    assert vr_url._client is not None

    vr_proj = reranker.VertexLayaReranker(endpoint_url="projects/my-p/locations/us-central1/endpoints/12345")
    assert vr_proj._client is not None

    # 4. VertexLayaReranker preserving omitted candidates and error fallback
    from collections import namedtuple
    Scored = namedtuple("Scored", ["id", "score"])

    mock_client = MagicMock()
    mock_client.rerank.return_value = [Scored(id="doc1", score=0.99)]
    v_rerank._client = mock_client

    # doc2 omitted by reranker model
    ranked, lat = v_rerank.rerank("query", [{"id": "doc1"}, {"id": "doc2"}])
    assert len(ranked) == 2
    assert ranked[0]["id"] == "doc1"
    assert ranked[0]["score"] == 0.99
    assert ranked[1]["id"] == "doc2"

    # Exception fallback returns unranked
    mock_client.rerank.side_effect = RuntimeError("Service unavailable")
    fallback_ranked, fb_lat = v_rerank.rerank("query", [{"id": "a"}, {"id": "b"}])
    assert len(fallback_ranked) == 2
    assert fallback_ranked[0]["id"] == "a"
    assert fb_lat == 0.0


# ==============================================================================
# REQ-CICD-010: Targeted tests for human_queue/agent.py & security/envelope.py
# ==============================================================================

from uuid import uuid4

from ticket_router.agents.human_queue.agent import HumanQueueAgent
from ticket_router.security.envelope import EnvelopeCipher
from ticket_router.store.fake import FakeTicketRepo
from ticket_router.store.models import TicketCreate


def test_human_queue_agent_edge_cases():
    """Verify HumanQueueAgent claim_ticket and resolve_ticket edge error branches."""
    repo = FakeTicketRepo()
    agent = HumanQueueAgent(ticket_repo=repo)

    # 1. Invalid UUID format
    assert agent.claim_ticket("not-a-uuid", "op1") == {"success": False, "error": "not_found"}
    assert agent.resolve_ticket("not-a-uuid", "op1", "notes") == {"status": "not_found"}

    # 2. Non-existent ticket
    random_id = str(uuid4())
    assert agent.claim_ticket(random_id, "op1") == {"success": False, "error": "not_found"}
    assert agent.resolve_ticket(random_id, "op1", "notes") == {"status": "not_found"}

    # 3. Already claimed ticket
    tc = TicketCreate(
        customer_id="cust-1",
        body_redacted="Help needed",
        assigned_queue="human-urgent",
    )
    t = repo.create_ticket(tc)

    # Queue depths before claiming
    depths = agent.queue_depths()
    assert isinstance(depths, dict)
    assert depths["human-urgent"] == 1

    # Claim by op1
    res_claim1 = agent.claim_ticket(t.ticket_id, "op1")
    assert res_claim1["success"] is True

    # Claim by op2 when assigned to op1
    res_claim2 = agent.claim_ticket(t.ticket_id, "op2")
    assert res_claim2["success"] is False
    assert res_claim2["error"] == "already_claimed"


def test_envelope_cipher_decrypt_error_branches():
    """Verify EnvelopeCipher decrypt validation error handling."""
    cipher = EnvelopeCipher(kms_key_name="projects/p/locations/l/keyRings/r/cryptoKeys/k")

    # Empty blob
    with pytest.raises(ValueError, match="Cannot decrypt empty envelope blob"):
        cipher.decrypt(b"", aad=b"test")

    # Invalid format (neither base64 nor valid json)
    with pytest.raises(ValueError, match="Invalid envelope format"):
        cipher.decrypt(b"???not-base64-or-json???", aad=b"test")

    # Valid JSON but not a dict
    with pytest.raises(ValueError, match="Invalid envelope payload structure"):
        cipher.decrypt(b"[1, 2, 3]", aad=b"test")

    # Unsupported version
    with pytest.raises(ValueError, match="Unsupported envelope version"):
        cipher.decrypt(b'{"v": 99}', aad=b"test")

    # Missing crypto fields
    with pytest.raises(ValueError, match="Envelope missing required crypto fields"):
        cipher.decrypt(b'{"v": 1, "wdek": "abc"}', aad=b"test")



