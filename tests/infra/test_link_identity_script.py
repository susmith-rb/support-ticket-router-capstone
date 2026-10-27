"""Hermetic unit tests for administrative account linking and identity test user seeding scripts.

# REQ-IDENT-006: Administrative Account Linking & Deterministic Demo Seeding
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts.link_customer_identity import (
    link_identity,
    suspend_customer,
    unlink_identity,
)
from scripts.link_customer_identity import (
    parse_args as parse_link_args,
)
from scripts.seed_identity_test_users import (
    main as seed_main,
)
from scripts.seed_identity_test_users import (
    mint_id_token,
    sign_up_or_in_user,
)
from ticket_router.store.fake import FakeCustomerRepo


# REQ-IDENT-006
def test_link_unlink_and_suspend_operations():
    """Verify linking, unlinking, and suspension logic with audit event recording."""
    repo = FakeCustomerRepo()
    issuer = "https://securetoken.google.com/susmith-fde-ticket-router"
    subject = "user-uid-42"
    customer_id = "cust_1001"
    actor = "admin@example.com"

    # 1. Link identity
    link_res = link_identity(
        repo=repo,
        issuer=issuer,
        subject=subject,
        customer_id=customer_id,
        actor=actor,
        email_verified=True,
    )
    assert link_res["action"] == "link"
    assert link_res["customer_id"] == customer_id
    assert link_res["linked"] is True
    assert link_res["email_verified"] is True

    # Verify resolution
    res = repo.resolve(issuer, subject)
    assert res.customer_id == customer_id
    assert res.status == "active"
    assert res.linked is True
    assert res.email_verified is True

    # Verify audit trail
    events = repo.list_events(customer_id)
    assert any(e.event_type == "identity_linked" and e.actor == actor for e in events)

    # 2. Suspend customer
    suspend_res = suspend_customer(repo=repo, customer_id=customer_id, actor=actor)
    assert suspend_res["suspended"] is True

    res_suspended = repo.resolve(issuer, subject)
    assert res_suspended.status == "suspended"
    assert res_suspended.linked is True

    events_after_suspend = repo.list_events(customer_id)
    assert any(e.event_type == "customer_suspended" and e.actor == actor for e in events_after_suspend)

    # 3. Unlink identity
    unlink_res = unlink_identity(repo=repo, issuer=issuer, subject=subject, actor=actor)
    assert unlink_res["unlinked"] is True

    res_unlinked = repo.resolve(issuer, subject)
    assert res_unlinked.customer_id is None
    assert res_unlinked.status == "unlinked"
    assert res_unlinked.linked is False

    events_after_unlink = repo.list_events(customer_id)
    assert any(e.event_type == "identity_unlinked" and e.actor == actor for e in events_after_unlink)


# REQ-IDENT-006
def test_link_customer_identity_cli_parsing():
    """Verify argument parsing for all subcommands in link_customer_identity."""
    args_link = parse_link_args([
        "link",
        "--issuer",
        "https://securetoken.google.com/test",
        "--subject",
        "sub-1",
        "--customer-id",
        "cust_1001",
        "--email-verified",
    ])
    assert args_link.command == "link"
    assert args_link.customer_id == "cust_1001"
    assert args_link.email_verified is True

    args_unlink = parse_link_args([
        "unlink",
        "--issuer",
        "https://securetoken.google.com/test",
        "--subject",
        "sub-1",
    ])
    assert args_unlink.command == "unlink"
    assert args_unlink.subject == "sub-1"

    args_suspend = parse_link_args([
        "suspend",
        "--customer-id",
        "cust_1002",
    ])
    assert args_suspend.command == "suspend"
    assert args_suspend.customer_id == "cust_1002"


# REQ-IDENT-006
def test_seed_identity_rest_mocking():
    """Verify sign_up_or_in_user and mint_id_token against mocked Identity Platform responses."""
    # Test new user signup
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "localId": "new-user-uid",
            "idToken": "fake-jwt-token",
        }
        mock_post.return_value = mock_resp

        res = sign_up_or_in_user(
            api_key="test-api-key",
            email="new@example.com",
            password="secretpassword123",
        )
        assert res["localId"] == "new-user-uid"
        assert res["idToken"] == "fake-jwt-token"

    # Test existing user fallback
    with patch("requests.post") as mock_post:
        # First call fails with EMAIL_EXISTS, second call succeeds with signInWithPassword
        mock_resp_exists = MagicMock()
        mock_resp_exists.status_code = 400
        mock_resp_exists.json.return_value = {
            "error": {"message": "EMAIL_EXISTS"}
        }

        mock_resp_signin = MagicMock()
        mock_resp_signin.status_code = 200
        mock_resp_signin.json.return_value = {
            "localId": "existing-user-uid",
            "idToken": "fresh-jwt-token",
        }

        mock_post.side_effect = [mock_resp_exists, mock_resp_signin]

        res = sign_up_or_in_user(
            api_key="test-api-key",
            email="existing@example.com",
            password="secretpassword123",
        )
        assert res["localId"] == "existing-user-uid"
        assert res["idToken"] == "fresh-jwt-token"

    # Test mint_id_token
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"idToken": "minted-token-xyz"}
        mock_post.return_value = mock_resp

        token = mint_id_token("api-key", "u@example.com", "pw")
        assert token == "minted-token-xyz"


# REQ-IDENT-006
def test_seed_identity_cli_fake_mode(capsys):
    """Verify seed_identity_test_users runs in hermetic fake mode without GCP dependencies."""
    exit_code = seed_main(["--fake", "--link"])
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "mock-uid-1001" in captured
    assert "mock-uid-1002" in captured

    # Test printing id token
    exit_code_token = seed_main(["--fake", "--print-id-token", "1001"])
    assert exit_code_token == 0
    token_out = capsys.readouterr().out.strip()
    assert token_out == "mock-id-token-1001"
