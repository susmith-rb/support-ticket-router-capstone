#!/usr/bin/env python3
"""Seed deterministic Identity Platform test accounts for demo and evaluation.

# REQ-IDENT-006: Administrative Account Linking & Deterministic Demo Seeding
Provisions demo users cust_1001 and cust_1002 in Identity Platform using the
restricted Web API key, stores credentials securely in Secret Manager, and
optionally links them to relational customer records in Cloud SQL.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import secrets
import string
import sys
from typing import Any, Dict, Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ticket_router.seed_identity")

DEMO_USERS = [
    {
        "num": "1001",
        "customer_id": "cust_1001",
        "email": "demo-cust-1001@example.com",
        "secret_id": "identity-demo-user-1001",
    },
    {
        "num": "1002",
        "customer_id": "cust_1002",
        "email": "demo-cust-1002@example.com",
        "secret_id": "identity-demo-user-1002",
    },
]


def resolve_web_api_key(
    project_id: str = "susmith-fde-ticket-router",
    secret_id: str = "identity-web-api-key",
) -> Optional[str]:
    """Retrieve Identity Platform Web API key from Secret Manager or environment."""
    env_key = os.environ.get("IDENTITY_WEB_API_KEY")
    if env_key:
        return env_key

    try:
        from ticket_router.common.auth import get_access_token

        token = get_access_token()
    except Exception as e:
        logger.warning("Could not obtain access token for Secret Manager: %s", e)
        return None

    url = (
        f"https://secretmanager.googleapis.com/v1/projects/{project_id}/"
        f"secrets/{secret_id}/versions/latest:access"
    )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            payload_b64 = resp.json().get("payload", {}).get("data", "")
            if payload_b64:
                return base64.b64decode(payload_b64).decode("utf-8")
    except Exception as e:
        logger.warning("Failed retrieving API key from Secret Manager: %s", e)
    return None


def get_or_create_user_password(
    project_id: str,
    secret_id: str,
) -> str:
    """Retrieve existing password from Secret Manager or create a new random password."""
    try:
        from ticket_router.common.auth import get_access_token

        token = get_access_token()
    except Exception:
        token = None

    if token:
        # Check if secret version exists
        access_url = (
            f"https://secretmanager.googleapis.com/v1/projects/{project_id}/"
            f"secrets/{secret_id}/versions/latest:access"
        )
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = requests.get(access_url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                payload_b64 = resp.json().get("payload", {}).get("data", "")
                if payload_b64:
                    return base64.b64decode(payload_b64).decode("utf-8")
        except Exception:
            pass

    # Generate strong random password
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    new_password = "".join(secrets.choice(chars) for _ in range(24))

    if token:
        # Create secret if it does not exist, then add version
        secret_url = f"https://secretmanager.googleapis.com/v1/projects/{project_id}/secrets"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        create_payload = {
            "replication": {"automatic": {}},
            "labels": {"cost_center": "support-ticket-router"},
        }
        requests.post(
            f"{secret_url}?secretId={secret_id}",
            headers=headers,
            json=create_payload,
            timeout=10.0,
        )

        add_version_url = (
            f"https://secretmanager.googleapis.com/v1/projects/{project_id}/"
            f"secrets/{secret_id}:addVersion"
        )
        b64_data = base64.b64encode(new_password.encode("utf-8")).decode("utf-8")
        requests.post(
            add_version_url,
            headers=headers,
            json={"payload": {"data": b64_data}},
            timeout=10.0,
        )

    return new_password


def sign_up_or_in_user(
    api_key: str,
    email: str,
    password: str,
) -> Dict[str, Any]:
    """Register user or sign in if already registered via Identity Platform REST API."""
    signup_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={api_key}"
    payload = {
        "email": email,
        "password": password,
        "returnSecureToken": True,
    }
    resp = requests.post(signup_url, json=payload, timeout=15.0)
    if resp.status_code == 200:
        return resp.json()

    data = resp.json()
    err_msg = data.get("error", {}).get("message", "")
    if "EMAIL_EXISTS" in err_msg:
        signin_url = (
            f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
        )
        signin_resp = requests.post(signin_url, json=payload, timeout=15.0)
        signin_resp.raise_for_status()
        return signin_resp.json()

    resp.raise_for_status()
    return {}


def mint_id_token(
    api_key: str,
    email: str,
    password: str,
) -> str:
    """Mint a fresh customer ID token for end-to-end tests."""
    signin_url = (
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
    )
    payload = {
        "email": email,
        "password": password,
        "returnSecureToken": True,
    }
    resp = requests.post(signin_url, json=payload, timeout=15.0)
    resp.raise_for_status()
    data = resp.json()
    token = data.get("idToken")
    if not token:
        raise RuntimeError("No idToken returned from Identity Platform")
    return token


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed Identity Platform demo accounts cust_1001 & cust_1002 (REQ-IDENT-006)"
    )
    parser.add_argument("--project", default="susmith-fde-ticket-router", help="GCP project ID")
    parser.add_argument("--api-key", default=None, help="Identity Platform Web API key")
    parser.add_argument("--link", action="store_true", help="Link seeded identities in Cloud SQL database")
    parser.add_argument(
        "--print-id-token",
        choices=["1001", "1002"],
        default=None,
        help="Mint and print fresh ID token for specified demo customer",
    )
    parser.add_argument("--fake", action="store_true", help="Simulate seeding without live GCP calls")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    project_id = args.project
    issuer = f"https://securetoken.google.com/{project_id}"

    if args.fake:
        api_key = args.api_key or "fake-api-key"
        if args.print_id_token:
            print(f"mock-id-token-{args.print_id_token}")
            return 0

        demo_results = []
        for user_meta in DEMO_USERS:
            num = user_meta["num"]
            subject = f"mock-uid-{num}"
            demo_results.append({
                "customer_id": user_meta["customer_id"],
                "email": user_meta["email"],
                "subject": subject,
                "issuer": issuer,
            })
            if args.link:
                from ticket_router.store.fake import FakeCustomerRepo

                fake_repo = FakeCustomerRepo()
                fake_repo.link_identity(
                    issuer=issuer,
                    subject=subject,
                    customer_id=user_meta["customer_id"],
                    actor="seed_script",
                    email_verified=True,
                )
        print(json.dumps(demo_results, indent=2))
        return 0

    api_key = args.api_key or resolve_web_api_key(project_id=project_id)
    if not api_key:
        logger.error("Could not resolve Identity Platform Web API key.")
        return 1

    if args.print_id_token:
        target = next((u for u in DEMO_USERS if u["num"] == args.print_id_token), None)
        if not target:
            logger.error("Invalid user selection: %s", args.print_id_token)
            return 1
        password = get_or_create_user_password(project_id=project_id, secret_id=target["secret_id"])
        token = mint_id_token(api_key=api_key, email=target["email"], password=password)
        print(token)
        return 0

    seeded_records = []
    for user_meta in DEMO_USERS:
        password = get_or_create_user_password(
            project_id=project_id,
            secret_id=user_meta["secret_id"],
        )
        auth_res = sign_up_or_in_user(
            api_key=api_key,
            email=user_meta["email"],
            password=password,
        )
        subject = auth_res["localId"]
        rec = {
            "customer_id": user_meta["customer_id"],
            "email": user_meta["email"],
            "subject": subject,
            "issuer": issuer,
        }
        seeded_records.append(rec)
        logger.info("Provisioned Identity Platform user: %s (subject=%s)", user_meta["email"], subject)

        if args.link:
            from scripts.link_customer_identity import build_postgres_repo, link_identity, resolve_password

            db_password = resolve_password(project_id=project_id)
            repo = build_postgres_repo(
                project_id=project_id,
                region="us-central1",
                instance_name="ticket-store",
                database="support",
                user="postgres_admin",
                password=db_password,
            )
            link_identity(
                repo=repo,
                issuer=issuer,
                subject=subject,
                customer_id=user_meta["customer_id"],
                actor="seed_identity_test_users",
                email_verified=True,
            )
            logger.info("Linked %s -> %s in database", subject, user_meta["customer_id"])

    print(json.dumps(seeded_records, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
