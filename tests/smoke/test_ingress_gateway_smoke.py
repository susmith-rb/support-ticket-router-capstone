"""Unit tests for scripts/smoke/ingress_gateway_smoke.py.

Fulfills REQ-AUTH-011 / REQ-MA-007:
- Verify event parsing (text extraction, output fields extraction).
- Verify resource name normalization.
- Verify benign query check, prompt injection check, and pii email check.
- Verify gateway block (403/Model Armor) vs in-agent escalation recording.
- Verify full smoke run and JSON summary generation under dry-run simulation.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scripts.smoke.ingress_gateway_smoke import (
    ADVERSARIAL_PROMPT_INJECTION,
    BENIGN_PROMPT,
    PII_EMAIL_PROMPT,
    CheckResult,
    DryRunMockEngine,
    RestAgentEngineClient,
    check_benign_query,
    check_pii_email,
    check_prompt_injection,
    check_prompt_injection_v1beta1,
    extract_event_output,
    extract_event_text,
    main,
    normalize_resource_name,
    parse_args,
    query_engine_stream,
    run_smoke,
)


def test_normalize_resource_name():
    """Verify numeric ID and full paths normalize correctly."""
    assert (
        normalize_resource_name("2170218524805431296", project="proj-test", region="us-central1")
        == "projects/proj-test/locations/us-central1/reasoningEngines/2170218524805431296"
    )
    full_path = "projects/p/locations/l/reasoningEngines/123"
    assert normalize_resource_name(full_path, project="other", region="other") == full_path
    assert (
        normalize_resource_name("custom-engine", project="p", region="l")
        == "projects/p/locations/l/reasoningEngines/custom-engine"
    )


def test_extract_event_text():
    """Verify text extraction from dicts and objects across event shapes."""
    # Dict with content.parts
    dict_event_parts = {"content": {"parts": [{"text": "Hello"}, {"text": "world"}]}}
    assert extract_event_text(dict_event_parts) == "Hello world"

    # Dict with output.answer
    dict_event_answer = {"output": {"answer": "Resolved reply"}}
    assert extract_event_text(dict_event_answer) == "Resolved reply"

    # Dict with text
    dict_event_text = {"text": "Simple text"}
    assert extract_event_text(dict_event_text) == "Simple text"

    # Object with content.parts
    obj_event = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text="Obj part 1"), SimpleNamespace(text="Obj part 2")])
    )
    assert extract_event_text(obj_event) == "Obj part 1 Obj part 2"

    # Object with output.answer
    obj_answer_event = SimpleNamespace(output={"answer": "Obj answer"})
    assert extract_event_text(obj_answer_event) == "Obj answer"

    # Empty fallback
    assert extract_event_text({}) == ""
    assert extract_event_text(None) == ""


def test_extract_event_output():
    """Verify output field dictionary extraction from dicts and objects."""
    # Dict output
    assert extract_event_output({"output": {"decision": "auto_rag", "tier": "tier1"}}) == {
        "decision": "auto_rag",
        "tier": "tier1",
    }
    # Object output
    assert extract_event_output(SimpleNamespace(output={"decision": "escalate"})) == {
        "decision": "escalate"
    }
    # None or non-dict output
    assert extract_event_output({}) == {}
    assert extract_event_output(SimpleNamespace(output="not-a-dict")) == {}


def test_query_engine_stream():
    """Verify streaming iteration joins text chunks and updates fields."""
    mock_engine = MagicMock()
    mock_engine.stream_query.return_value = [
        {"content": {"parts": [{"text": "Part 1"}]}, "output": {"decision": "auto_rag"}},
        {"content": {"parts": [{"text": "Part 2"}]}, "output": {"queue": "human-standard"}},
    ]

    status, text, fields = query_engine_stream(
        engine=mock_engine,
        message="Hello",
        user_id="u1",
        session_id="s1",
    )
    assert status == 200
    assert text == "Part 1 Part 2"
    assert fields == {"decision": "auto_rag", "queue": "human-standard"}
    mock_engine.stream_query.assert_called_once_with(
        message="Hello", user_id="u1", session_id="s1"
    )


def test_check_benign_query_success_and_failure():
    """Verify check_benign_query records pass on 200 with text, and fail on exception."""
    mock_engine_ok = MagicMock()
    mock_engine_ok.stream_query.return_value = [
        {"content": {"parts": [{"text": "Order ORD-101 is shipping."}]}}
    ]
    res_ok = check_benign_query(mock_engine_ok, user_id="u", session_id="s")
    assert res_ok.name == "benign_query"
    assert res_ok.passed is True
    assert res_ok.status_code == 200
    assert "ORD-101" in res_ok.metadata["answer"]

    mock_engine_err = MagicMock()
    mock_engine_err.stream_query.side_effect = RuntimeError("Service unavailable")
    res_err = check_benign_query(mock_engine_err, user_id="u", session_id="s")
    assert res_err.passed is False
    assert res_err.status_code == 500
    assert "Service unavailable" in res_err.details


def test_check_prompt_injection_gateway_block():
    """Verify check_prompt_injection treats gateway 403 / Model Armor block as PASS."""
    mock_engine = MagicMock()
    exc = RuntimeError("403 Model Armor: Prompt violates content security configurations")
    exc.code = 403
    mock_engine.stream_query.side_effect = exc

    res: CheckResult = check_prompt_injection(mock_engine, user_id="u", session_id="s")
    assert res.passed is True
    assert res.status_code == 403
    assert res.metadata["gateway_blocked"] is True
    assert "blocked at gateway" in res.details


def test_check_prompt_injection_ticket_ack_fails():
    """Verify check_prompt_injection treats ticket acknowledgement as FAIL (REQ-MA-003)."""
    mock_engine = MagicMock()
    mock_engine.stream_query.return_value = [
        {
            "output": {
                "decision": "escalate",
                "tier": "model_armor",
                "ticket_id": "TICK-12345678",
                "answer": "Thank you for contacting support. We have logged ticket #TICK-12345678.",
            }
        }
    ]

    res: CheckResult = check_prompt_injection(mock_engine, user_id="u", session_id="s")
    assert res.passed is False
    assert res.status_code == 200
    assert res.metadata["ticket_created"] is True
    assert "created/acknowledged ticket" in res.details


def test_check_prompt_injection_in_agent_refusal_passes():
    """Verify check_prompt_injection treats in-agent security refusal as PASS."""
    mock_engine = MagicMock()
    mock_engine.stream_query.return_value = [
        {
            "output": {
                "decision": "security_blocked",
                "tier": "model_armor",
                "queue": "none",
                "answer": (
                    "I can't help with that request. For your security, messages that try "
                    "to override my instructions are blocked. If you need support, please "
                    "describe your issue and I'll be glad to help."
                ),
            }
        }
    ]

    res: CheckResult = check_prompt_injection(mock_engine, user_id="u", session_id="s")
    assert res.passed is True
    assert res.status_code == 200
    assert res.metadata["in_agent_security_blocked"] is True
    assert "in-agent security refusal" in res.details


def test_check_prompt_injection_v1beta1_success_and_failure():
    """Verify check_prompt_injection_v1beta1 asserts 200 + refusal text and zero ticket ack."""
    mock_ok = MagicMock()
    mock_ok.stream_query.return_value = [
        {
            "output": {
                "decision": "security_blocked",
                "answer": (
                    "I can't help with that request. For your security, messages that try "
                    "to override my instructions are blocked. If you need support, please "
                    "describe your issue and I'll be glad to help."
                ),
            }
        }
    ]
    res_ok = check_prompt_injection_v1beta1(mock_ok, user_id="ge-user", session_id="ge-sess")
    assert res_ok.passed is True
    assert res_ok.status_code == 200
    assert res_ok.metadata["is_refusal"] is True
    assert res_ok.metadata["ticket_acknowledged"] is False

    # Failure when ticket is acknowledged
    mock_fail = MagicMock()
    mock_fail.stream_query.return_value = [
        {
            "output": {
                "decision": "escalate",
                "ticket_id": "TICK-999",
                "answer": "Logged ticket #TICK-999 for you.",
            }
        }
    ]
    res_fail = check_prompt_injection_v1beta1(mock_fail, user_id="ge-user", session_id="ge-sess")
    assert res_fail.passed is False

    # Failure on exception
    mock_err = MagicMock()
    mock_err.stream_query.side_effect = RuntimeError("500 internal error")
    res_err = check_prompt_injection_v1beta1(mock_err, user_id="ge-user", session_id="ge-sess")
    assert res_err.passed is False
    assert res_err.status_code == 500


def test_rest_agent_engine_client():
    """Verify RestAgentEngineClient SSE line decoding and streaming."""
    client = RestAgentEngineClient(
        resource_name="projects/p/locations/l/reasoningEngines/123",
        project="p",
        region="l",
        access_token="fake-token",
        api_version="v1beta1",
    )
    sess = client.create_session("u")
    assert "id" in sess

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.iter_lines.return_value = [
        b'data: {"output": {"decision": "security_blocked", "answer": "Blocked."}}',
        b"",
        b'data: {"text": "done"}',
    ]

    with patch("requests.post", return_value=mock_resp):
        events = list(client.stream_query("hello", user_id="u", session_id="s"))
        assert len(events) == 2
        assert events[0]["output"]["decision"] == "security_blocked"
        assert events[1]["text"] == "done"


def test_check_pii_email_success_and_failure():
    """Verify check_pii_email passes on HTTP 200 and records details."""
    mock_engine_ok = MagicMock()
    mock_engine_ok.stream_query.return_value = [
        {"output": {"decision": "auto_rag", "answer": "Email updated."}}
    ]
    res_ok = check_pii_email(mock_engine_ok, user_id="u", session_id="s")
    assert res_ok.passed is True
    assert res_ok.status_code == 200
    assert res_ok.metadata["decision"] == "auto_rag"

    mock_engine_err = MagicMock()
    mock_engine_err.stream_query.side_effect = RuntimeError("Bad request")
    res_err = check_pii_email(mock_engine_err, user_id="u", session_id="s")
    assert res_err.passed is False
    assert res_err.status_code == 500


def test_dry_run_mock_engine():
    """Verify DryRunMockEngine behaviors for benign, email, and injection."""
    engine_gw_block = DryRunMockEngine(simulate_gateway_block=True)
    sess = engine_gw_block.create_session("u")
    assert "id" in sess

    # Benign returns answer
    benign_events = list(engine_gw_block.stream_query(BENIGN_PROMPT, "u", sess["id"]))
    assert len(benign_events) == 1
    assert "ORD-10001" in benign_events[0]["output"]["answer"]

    # Email returns answer
    email_events = list(engine_gw_block.stream_query(PII_EMAIL_PROMPT, "u", sess["id"]))
    assert len(email_events) == 1
    assert "customer-help@example.com" in email_events[0]["output"]["answer"]

    # Prompt injection raises 403 when simulate_gateway_block=True
    with pytest.raises(RuntimeError, match="403 Model Armor"):
        list(engine_gw_block.stream_query(ADVERSARIAL_PROMPT_INJECTION, "u", sess["id"]))

    # Prompt injection simulates in-agent security refusal when simulate_gateway_block=False
    engine_in_agent = DryRunMockEngine(simulate_gateway_block=False)
    in_agent_events = list(engine_in_agent.stream_query(ADVERSARIAL_PROMPT_INJECTION, "u", sess["id"]))
    assert in_agent_events[0]["output"]["decision"] == "security_blocked"
    assert in_agent_events[0]["output"]["tier"] == "model_armor"


def test_run_smoke_dry_run():
    """Verify run_smoke runs end-to-end with dry_run=True and outputs PASS summary."""
    summary = run_smoke(
        resource_name="2170218524805431296",
        project="susmith-fde-ticket-router",
        region="us-central1",
        dry_run=True,
    )
    assert summary["overall_status"] == "PASS"
    assert "projects/susmith-fde-ticket-router/locations/us-central1/reasoningEngines/2170218524805431296" in summary["target_engine"]
    assert summary["checks"]["benign_query"]["passed"] is True
    assert summary["checks"]["prompt_injection"]["passed"] is True
    assert summary["checks"]["prompt_injection"]["metadata"]["gateway_blocked"] is True
    assert summary["checks"]["pii_email"]["passed"] is True
    assert summary["checks"]["prompt_injection_v1beta1"]["passed"] is True


def test_run_smoke_with_custom_mock_engine():
    """Verify run_smoke with custom mock engine simulating in-agent security refusal."""
    mock = DryRunMockEngine(simulate_gateway_block=False)
    summary = run_smoke(
        resource_name="my-engine",
        project="p",
        region="l",
        mock_engine=mock,
    )
    assert summary["overall_status"] == "PASS"
    assert summary["checks"]["prompt_injection"]["metadata"]["gateway_blocked"] is False
    assert summary["checks"]["prompt_injection"]["metadata"]["in_agent_security_blocked"] is True
    assert summary["checks"]["prompt_injection_v1beta1"]["passed"] is True


def test_parse_args_and_main(capsys):
    """Verify CLI argument parsing and main execution."""
    args = parse_args(
        [
            "--resource-name",
            "12345",
            "--project",
            "test-p",
            "--region",
            "europe-west1",
            "--user-id",
            "custom-user",
            "--dry-run",
        ]
    )
    assert args.resource_name == "12345"
    assert args.project == "test-p"
    assert args.region == "europe-west1"
    assert args.user_id == "custom-user"
    assert args.dry_run is True

    # Test environment fallback
    with patch.dict(
        os.environ,
        {"TRIAGE_RESOURCE": "999", "PROJECT_ID": "env-p", "REGION": "asia-northeast1"},
    ):
        env_args = parse_args([])
        assert env_args.resource_name == "999"
        assert env_args.project == "env-p"
        assert env_args.region == "asia-northeast1"

    # Test main() with dry-run
    with patch("sys.argv", ["smoke.py", "--dry-run"]):
        main()
    captured = capsys.readouterr().out
    assert "=== Agent Gateway Ingress Smoke Summary ===" in captured
    assert '"overall_status": "PASS"' in captured
