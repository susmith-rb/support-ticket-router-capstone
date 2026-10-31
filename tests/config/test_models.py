"""Targeted unit tests for ticket_router.config.models pure logic.

# REQ-CICD-010: Line and Branch Test Coverage Enforcement in CI.
# REQ-BILL-MODELS-001: Centralized Model Configuration Registry.
# REQ-EVAL-008: Controlled Hill-Climbing Protocol & Config Registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ticket_router.config import models


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
