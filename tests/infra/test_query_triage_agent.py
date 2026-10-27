"""Hermetic unit tests for scripts/deploy_agents/query_triage_agent.py.

# REQ-AGENT-017: Agent Runtime & Vertex AI Agent Engine Deployment
# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution via ticket-db-mcp
# REQ-IDENT-008: End-to-End Identity Platform Verification and Threat Audit
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from scripts.deploy_agents.query_triage_agent import (
    DEFAULT_PROJECT,
    DEFAULT_REGION,
    DEFAULT_USER_ID,
    extract_text_from_event,
    extract_ticket_fields_from_event,
    main,
    parse_args,
    resolve_demo_user_token,
    run_query,
)


def test_parse_args_defaults():
    """Verify argument parsing default values."""
    args = parse_args(["--resource-name", "projects/123/locations/us-central1/reasoningEngines/456"])
    assert args.resource_name == "projects/123/locations/us-central1/reasoningEngines/456"
    assert args.customer_id_token is None
    assert args.demo_user is None
    assert args.user_id == DEFAULT_USER_ID
    assert args.project == DEFAULT_PROJECT
    assert args.region == DEFAULT_REGION
    assert args.stream is False
    assert args.dry_run is False


def test_parse_args_custom():
    """Verify custom flags for customer token, demo user, stream, and dry-run."""
    args = parse_args(
        [
            "--resource-name",
            "dummy-engine",
            "--message",
            "Status of ORD-999?",
            "--session-id",
            "sess-abc",
            "--customer-id-token",
            "header.payload.sig",
            "--demo-user",
            "1001",
            "--user-id",
            "custom-caller",
            "--stream",
            "--dry-run",
        ]
    )
    assert args.resource_name == "dummy-engine"
    assert args.message == "Status of ORD-999?"
    assert args.session_id == "sess-abc"
    assert args.customer_id_token == "header.payload.sig"
    assert args.demo_user == "1001"
    assert args.user_id == "custom-caller"
    assert args.stream is True
    assert args.dry_run is True


def test_extract_text_from_event_dict_formats():
    """Verify text extraction from various ADK/Gemini streaming event dict payloads."""
    # Format 1: content.parts.text
    event1 = {"content": {"parts": [{"text": "Hello"}, {"text": "world!"}]}}
    assert extract_text_from_event(event1) == "Hello world!"

    # Format 2: output.answer
    event2 = {"output": {"answer": "Your order is delivered."}}
    assert extract_text_from_event(event2) == "Your order is delivered."

    # Format 3: direct text
    event3 = {"text": "Direct message"}
    assert extract_text_from_event(event3) == "Direct message"

    # Empty or unrecognized
    assert extract_text_from_event({}) == ""


def test_extract_text_from_event_object_format():
    """Verify text extraction from ADK object representation."""
    part1 = MagicMock()
    part1.text = "Mocked"
    part2 = MagicMock()
    part2.text = "response"
    content_obj = MagicMock()
    content_obj.parts = [part1, part2]

    event_obj = MagicMock()
    event_obj.content = content_obj
    event_obj.output = None

    assert extract_text_from_event(event_obj) == "Mocked response"


def test_extract_ticket_fields_from_event():
    """Verify ticket metadata extraction."""
    # Dict event
    event = {
        "output": {
            "ticket_id": "TICK-12345",
            "decision": "auto_rag",
            "queue": "automated",
            "answer": "All good.",
            "unrelated": "ignored",
        }
    }
    fields = extract_ticket_fields_from_event(event)
    assert fields == {
        "ticket_id": "TICK-12345",
        "decision": "auto_rag",
        "queue": "automated",
        "answer": "All good.",
    }

    # No output
    assert extract_ticket_fields_from_event({"other": 1}) == {}


def test_resolve_demo_user_token_success():
    """Verify resolve_demo_user_token calls seed_identity_test_users.py correctly."""
    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.stdout = "mocked-jwt-token-for-1001\n"
        mock_run.return_value = mock_proc

        token = resolve_demo_user_token("1001")
        assert token == "mocked-jwt-token-for-1001"
        assert mock_run.call_count == 1
        call_args = mock_run.call_args[0][0]
        assert "--print-id-token" in call_args
        assert "1001" in call_args


def test_resolve_demo_user_token_failure():
    """Verify resolve_demo_user_token gracefully handles subprocess failure."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, ["cmd"], stderr="Error minting")
        token = resolve_demo_user_token("1001")
        assert token is None


def test_run_query_dry_run():
    """Verify dry_run simulation does not call Vertex AI."""
    result = run_query(
        resource_name="projects/123/locations/us-central1/reasoningEngines/456",
        message="Where is my order?",
        session_id="dryrun-sess-99",
        customer_id_token="fake.token",
        dry_run=True,
    )
    assert result["dry_run"] is True
    assert result["session_id"] == "dryrun-sess-99"
    assert result["customer_verified"] is True
    assert "delivered" in result["answer"].lower() or "transit" in result["answer"].lower()


def test_run_query_live_authenticated_mocked():
    """Verify run_query passes customer_id_token in session state to Agent Engine."""
    mock_engine = MagicMock()
    mock_session = {"id": "sess-live-42"}
    mock_engine.create_session.return_value = mock_session
    mock_engine.stream_query.return_value = [
        {"content": {"parts": [{"text": "Your package has been delivered."}]}},
        {"output": {"ticket_id": "TICK-LIVE-1", "decision": "auto_rag", "queue": "automated"}},
    ]

    with patch("vertexai.init") as mock_vinit, patch("vertexai.agent_engines.get", return_value=mock_engine) as mock_eget:
        res = run_query(
            resource_name="projects/test/locations/us-central1/reasoningEngines/123",
            message="Check my order ORD-10001",
            session_id="sess-custom-1",
            customer_id_token="valid.customer.jwt",
            user_id="caller-id-test",
            project="test-proj",
            region="us-central1",
            dry_run=False,
        )

        mock_vinit.assert_called_once_with(project="test-proj", location="us-central1")
        mock_eget.assert_called_once_with("projects/test/locations/us-central1/reasoningEngines/123")

        # Invariant: session must be created with customer_id_token in state dictionary
        mock_engine.create_session.assert_called_once_with(
            user_id="caller-id-test",
            session_id="sess-custom-1",
            state={"customer_id_token": "valid.customer.jwt"},
        )

        mock_engine.stream_query.assert_called_once_with(
            message="Check my order ORD-10001",
            user_id="caller-id-test",
            session_id="sess-live-42",
        )

        assert res["text"] == "Your package has been delivered."
        assert res["ticket_id"] == "TICK-LIVE-1"
        assert res["decision"] == "auto_rag"
        assert res["queue"] == "automated"
        assert res["session_id"] == "sess-live-42"
        assert res["status"] == "COMPLETED"


def test_run_query_demo_user_resolves_token():
    """Verify run_query automatically mints demo token if customer_id_token is omitted."""
    mock_engine = MagicMock()
    mock_engine.create_session.return_value = {"id": "sess-demo"}
    mock_engine.stream_query.return_value = [{"content": {"parts": [{"text": "OK"}]}}]

    with patch("scripts.deploy_agents.query_triage_agent.resolve_demo_user_token", return_value="minted.demo.token") as mock_resolve, \
         patch("vertexai.init"), \
         patch("vertexai.agent_engines.get", return_value=mock_engine):

        res = run_query(
            resource_name="dummy-engine",
            message="Hello",
            demo_user="1001",
            dry_run=False,
        )

        mock_resolve.assert_called_once_with("1001")
        mock_engine.create_session.assert_called_once()
        _, kwargs = mock_engine.create_session.call_args
        assert kwargs["state"] == {"customer_id_token": "minted.demo.token"}
        assert res["status"] == "COMPLETED"


def test_main_cli_execution():
    """Verify main() runs and formats output properly."""
    with patch("scripts.deploy_agents.query_triage_agent.run_query") as mock_run_query, \
         patch("sys.argv", ["query_triage_agent.py", "--resource-name", "dummy", "--dry-run"]):
        mock_run_query.return_value = {
            "session_id": "sess-test",
            "ticket_id": "TICK-999",
            "decision": "auto_rag",
            "queue": "automated",
            "text": "All orders delivered.",
        }
        main()
        mock_run_query.assert_called_once()
