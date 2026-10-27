"""Hermetic unit tests for scripts/e2e_identity_check.py.

# REQ-IDENT-008: End-to-End Identity Platform Verification and Threat Audit
# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution via ticket-db-mcp
# REQ-IDENT-004: Fail-Closed Anonymous Fallback & Authorization Boundaries
# REQ-IDENT-005: Cryptographic Trust Chain Propagation across A2A & MCP
# REQ-IDENT-007: Identity Threat Mitigations (Anti-Spoofing, Replay, IDOR, Suspension)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from scripts.e2e_identity_check import (
    CheckResult,
    fetch_queue_tickets,
    get_owner_identity_token,
    main,
    parse_args,
    print_results_table,
    run_single_check_a,
    run_single_check_b,
    run_single_check_c,
    run_single_check_d,
)


@pytest.fixture
def mock_session() -> requests.Session:
    return MagicMock(spec=requests.Session)


def test_parse_args():
    """Verify argument parsing defaults and overrides."""
    args = parse_args([])
    assert "reasoningEngines" in args.resource_name
    assert "human-queue" in args.human_queue_url
    assert args.dry_run is False

    custom_args = parse_args(["--resource-name", "custom-res", "--dry-run"])
    assert custom_args.resource_name == "custom-res"
    assert custom_args.dry_run is True


def test_get_owner_identity_token_env_override(monkeypatch):
    """Verify owner token is retrieved from environment variable if present."""
    monkeypatch.setenv("GOOGLE_OWNER_IDENTITY_TOKEN", "mocked.owner.token")
    token = get_owner_identity_token()
    assert token == "mocked.owner.token"


def test_get_owner_identity_token_gcloud_fallback(monkeypatch):
    """Verify owner token falls back to gcloud auth print-identity-token."""
    monkeypatch.delenv("GOOGLE_OWNER_IDENTITY_TOKEN", raising=False)
    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.stdout = "gcloud.token.val\n"
        mock_run.return_value = mock_proc

        token = get_owner_identity_token()
        assert token == "gcloud.token.val"
        mock_run.assert_called_once_with(
            ["gcloud", "auth", "print-identity-token"],
            capture_output=True,
            text=True,
            check=True,
        )


def test_fetch_queue_tickets(mock_session):
    """Verify querying human-queue Cloud Run service."""
    # When token is None, skip query
    assert fetch_queue_tickets(mock_session, "https://queue.app", None) == []

    # When token is provided, verify query
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"ticket_id": "TICK-1", "customer_id": "anon:sess-1"}]
    mock_session.get.return_value = mock_resp

    tickets = fetch_queue_tickets(mock_session, "https://queue.app", "test.token")
    assert len(tickets) == 1
    assert tickets[0]["ticket_id"] == "TICK-1"
    mock_session.get.assert_called_once()


def test_run_single_check_a_dry_run(mock_session):
    """Check (a) passes in dry-run mode."""
    res = run_single_check_a(
        runner_fn=MagicMock(),
        session_pool=mock_session,
        resource_name="res-1",
        human_queue_url="https://queue.app",
        owner_token=None,
        project="test-proj",
        region="us-central1",
        dry_run=True,
    )
    assert res.passed is True
    assert res.check_id == "(a)"


def test_run_single_check_a_pass(mock_session):
    """Check (a) passes when runner returns delivered order info."""
    mock_runner = MagicMock(return_value={
        "text": "Your order ORD-10001 for ApexGlide Wireless Mouse was marked as DELIVERED."
    })
    res = run_single_check_a(
        runner_fn=mock_runner,
        session_pool=mock_session,
        resource_name="res-1",
        human_queue_url="https://queue.app",
        owner_token=None,
        project="test-proj",
        region="us-central1",
        dry_run=False,
    )
    assert res.passed is True
    assert "successfully retrieved" in res.details


def test_run_single_check_a_fail_missing_details(mock_session):
    """Check (a) fails when order details are missing."""
    mock_runner = MagicMock(return_value={"text": "I don't know what you are talking about."})
    res = run_single_check_a(
        runner_fn=mock_runner,
        session_pool=mock_session,
        resource_name="res-1",
        human_queue_url="https://queue.app",
        owner_token=None,
        project="test-proj",
        region="us-central1",
        dry_run=False,
    )
    assert res.passed is False
    assert "did not reference order details" in res.details


def test_run_single_check_a_fail_signin_prompt(mock_session):
    """Check (a) fails if authenticated user is unexpectedly asked to sign in."""
    mock_runner = MagicMock(return_value={
        "text": "For ORD-10001, please sign in to view your details."
    })
    res = run_single_check_a(
        runner_fn=mock_runner,
        session_pool=mock_session,
        resource_name="res-1",
        human_queue_url="https://queue.app",
        owner_token=None,
        project="test-proj",
        region="us-central1",
        dry_run=False,
    )
    assert res.passed is False
    assert "unexpectedly prompted to sign in" in res.details


def test_run_single_check_a_cold_start_retry(mock_session):
    """Check (a) retries once on cold start timeout / calming ack."""
    first_resp = {"text": "Thank you. We logged ticket TICK-123 for human review."}
    second_resp = {"text": "Your order ORD-10001 has been DELIVERED."}
    mock_runner = MagicMock(side_effect=[first_resp, second_resp])

    res = run_single_check_a(
        runner_fn=mock_runner,
        session_pool=mock_session,
        resource_name="res-1",
        human_queue_url="https://queue.app",
        owner_token=None,
        project="test-proj",
        region="us-central1",
        dry_run=False,
    )
    assert mock_runner.call_count == 2
    assert res.passed is True


def test_run_single_check_b_pass(mock_session):
    """Check (b) passes when cross-customer order details are guarded."""
    mock_runner = MagicMock(return_value={
        "text": "We could not find order ORD-10002 in your account."
    })
    res = run_single_check_b(
        runner_fn=mock_runner,
        session_pool=mock_session,
        resource_name="res-1",
        human_queue_url="https://queue.app",
        owner_token=None,
        project="test-proj",
        region="us-central1",
        dry_run=False,
    )
    assert res.passed is True
    assert "strictly hidden" in res.details


def test_run_single_check_b_fail_idor_leak(mock_session):
    """Check (b) fails if cross-customer items (EcoLeather Desk Mat) are leaked."""
    mock_runner = MagicMock(return_value={
        "text": "Order ORD-10002 contains EcoLeather Desk Mat and is shipped to San Francisco."
    })
    res = run_single_check_b(
        runner_fn=mock_runner,
        session_pool=mock_session,
        resource_name="res-1",
        human_queue_url="https://queue.app",
        owner_token=None,
        project="test-proj",
        region="us-central1",
        dry_run=False,
    )
    assert res.passed is False
    assert "BOLA/IDOR leak detected" in res.details


def test_run_single_check_c_pass(mock_session):
    """Check (c) passes when unauthenticated user receives sign-in prompt without order data."""
    mock_runner = MagicMock(return_value={
        "text": "Please sign in to check your order details and track shipments."
    })
    res = run_single_check_c(
        runner_fn=mock_runner,
        session_pool=mock_session,
        resource_name="res-1",
        human_queue_url="https://queue.app",
        owner_token=None,
        project="test-proj",
        region="us-central1",
        dry_run=False,
    )
    assert res.passed is True
    assert "polite sign-in prompt" in res.details


def test_run_single_check_c_fail_order_leak(mock_session):
    """Check (c) fails if unauthenticated user is given order contents."""
    mock_runner = MagicMock(return_value={
        "text": "Your ApexGlide mouse is delivered to Minneapolis. Please sign in."
    })
    res = run_single_check_c(
        runner_fn=mock_runner,
        session_pool=mock_session,
        resource_name="res-1",
        human_queue_url="https://queue.app",
        owner_token=None,
        project="test-proj",
        region="us-central1",
        dry_run=False,
    )
    assert res.passed is False
    assert "exposed order data" in res.details


def test_run_single_check_d_pass_with_human_queue(mock_session):
    """Check (d) verifies calming ack and ticket in human queue with anon: session ID."""
    mock_runner = MagicMock(return_value={
        "session_id": "sess-urgent-123",
        "text": "Thank you for reaching out. A support specialist is reviewing your ticket.",
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"ticket_id": "TICK-URGENT", "customer_id": "anon:sess-urgent-123"}
    ]
    mock_session.get.return_value = mock_resp

    res = run_single_check_d(
        runner_fn=mock_runner,
        session_pool=mock_session,
        resource_name="res-1",
        human_queue_url="https://queue.app",
        owner_token="owner.jwt",
        project="test-proj",
        region="us-central1",
        dry_run=False,
    )
    assert res.passed is True
    assert "Verified ticket customer_id='anon:sess-urgent-123' in human-queue." in res.details


def test_print_results_table():
    """Verify table printing and status aggregation."""
    results_pass = [
        CheckResult("(a)", "Check A", True, "Details A", 1.2),
        CheckResult("(b)", "Check B", True, "Details B", 0.8),
    ]
    assert print_results_table(results_pass) is True

    results_fail = [
        CheckResult("(a)", "Check A", True, "Details A", 1.2),
        CheckResult("(b)", "Check B", False, "Details B", 0.8),
    ]
    assert print_results_table(results_fail) is False


def test_main_cli_dry_run():
    """Verify main() CLI execution in dry-run mode."""
    with patch("sys.argv", ["e2e_identity_check.py", "--dry-run"]):
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 0
