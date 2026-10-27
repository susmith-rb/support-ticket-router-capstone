#!/usr/bin/env python3
"""Administrative CLI for customer account linking, unlinking, and suspension.

# REQ-IDENT-006: Administrative Account Linking & Deterministic Demo Seeding
Executes with Cloud SQL IAM or break-glass admin credentials to manage relational
mappings in customer_identities and audit records in identity_events.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

import requests

from ticket_router.store.interfaces import CustomerRepository
from ticket_router.store.postgres import PostgresCustomerRepo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ticket_router.link_customer_identity")

DEFAULT_SECRET_ID = "cloudsql-ticket-store-admin-password"


def resolve_password(
    cli_password: Optional[str] = None,
    secret_id: Optional[str] = DEFAULT_SECRET_ID,
    project_id: str = "susmith-fde-ticket-router",
    version: str = "latest",
) -> Optional[str]:
    """Resolve database password: CLI arg > DB_PASSWORD env > Secret Manager REST."""
    if cli_password:
        return cli_password

    env_password = os.environ.get("DB_PASSWORD")
    if env_password:
        return env_password

    target_secret = secret_id or DEFAULT_SECRET_ID
    try:
        from ticket_router.common.auth import get_access_token

        token = get_access_token()
    except Exception as e:
        logger.warning("Could not obtain access token for Secret Manager: %s", e)
        return None

    url = (
        f"https://secretmanager.googleapis.com/v1/projects/{project_id}/"
        f"secrets/{target_secret}/versions/{version}:access"
    )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            payload_b64 = data.get("payload", {}).get("data", "")
            if payload_b64:
                return base64.b64decode(payload_b64).decode("utf-8")
    except Exception as e:
        logger.warning("Failed retrieving secret from Secret Manager: %s", e)
    return None


def get_connector(token: Optional[str] = None):
    from google.cloud.sql.connector import Connector

    oauth_token = token or os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
    if oauth_token:
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=oauth_token)
        return Connector(credentials=creds)

    try:
        from ticket_router.common.auth import get_access_token

        act = get_access_token()
        if act:
            from google.oauth2.credentials import Credentials

            creds = Credentials(token=act)
            return Connector(credentials=creds)
    except Exception as e:
        logger.debug("Falling back to default Connector auth: %s", e)

    return Connector()


class ConnectorFactory:
    """Callable connection factory avoiding nested functions."""

    def __init__(
        self,
        connector: Any,
        instance_conn_name: str,
        user: str,
        password: Optional[str],
        database: str,
    ) -> None:
        self.connector = connector
        self.instance_conn_name = instance_conn_name
        self.user = user
        self.password = password
        self.database = database

    def create_connection(self):
        from google.cloud.sql.connector import IPTypes

        return self.connector.connect(
            self.instance_conn_name,
            "pg8000",
            user=self.user,
            password=self.password,
            db=self.database,
            ip_type=IPTypes.PUBLIC,
        )


def build_postgres_repo(
    project_id: str,
    region: str,
    instance_name: str,
    database: str,
    user: str,
    password: Optional[str],
) -> PostgresCustomerRepo:
    connector = get_connector()
    factory = ConnectorFactory(
        connector=connector,
        instance_conn_name=f"{project_id}:{region}:{instance_name}",
        user=user,
        password=password,
        database=database,
    )
    return PostgresCustomerRepo(connection_factory=factory.create_connection)


def link_identity(
    repo: CustomerRepository,
    issuer: str,
    subject: str,
    customer_id: str,
    actor: str,
    email_verified: bool = False,
) -> Dict[str, Any]:
    """Map IdP (issuer, subject) to customer_id and record audit event."""
    repo.link_identity(
        issuer=issuer,
        subject=subject,
        customer_id=customer_id,
        actor=actor,
        email_verified=email_verified,
    )
    resolution = repo.resolve(issuer, subject)
    return {
        "action": "link",
        "issuer": issuer,
        "subject": subject,
        "customer_id": resolution.customer_id,
        "status": resolution.status,
        "linked": resolution.linked,
        "email_verified": resolution.email_verified,
    }


def unlink_identity(
    repo: CustomerRepository,
    issuer: str,
    subject: str,
    actor: str,
) -> Dict[str, Any]:
    """Unlink IdP identity and record audit event."""
    success = repo.unlink_identity(issuer=issuer, subject=subject, actor=actor)
    return {
        "action": "unlink",
        "issuer": issuer,
        "subject": subject,
        "unlinked": success,
    }


def suspend_customer(
    repo: CustomerRepository,
    customer_id: str,
    actor: str,
) -> Dict[str, Any]:
    """Suspend customer and record audit event."""
    success = repo.suspend_customer(customer_id=customer_id, actor=actor)
    return {
        "action": "suspend",
        "customer_id": customer_id,
        "suspended": success,
    }


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Administrative customer identity linking and lifecycle management (REQ-IDENT-006)"
    )
    parser.add_argument("--project", default="susmith-fde-ticket-router", help="GCP project ID")
    parser.add_argument("--region", default="us-central1", help="GCP region")
    parser.add_argument("--instance", default="ticket-store", help="Cloud SQL instance name")
    parser.add_argument("--database", default="support", help="Database name")
    parser.add_argument("--user", default="postgres_admin", help="Database user")
    parser.add_argument("--password", default=None, help="Database password")
    parser.add_argument("--password-secret", default=DEFAULT_SECRET_ID, help="Secret Manager secret ID")
    parser.add_argument("--password-secret-version", default="latest", help="Secret Manager secret version")
    parser.add_argument("--actor", default="cli-admin", help="Actor identity for audit event logging")
    parser.add_argument("--fake", action="store_true", help="Use in-memory fake repository for dry testing")

    subparsers = parser.add_subparsers(dest="command", required=True)

    link_parser = subparsers.add_parser("link", help="Link an IdP identity to a customer_id")
    link_parser.add_argument("--issuer", required=True, help="Identity token issuer URL")
    link_parser.add_argument("--subject", required=True, help="IdP subject UID (localId)")
    link_parser.add_argument("--customer-id", required=True, help="Internal customer ID (e.g. cust_1001)")
    link_parser.add_argument(
        "--email-verified",
        action="store_true",
        default=False,
        help="Mark email as verified in identity platform",
    )

    unlink_parser = subparsers.add_parser("unlink", help="Unlink an IdP identity")
    unlink_parser.add_argument("--issuer", required=True, help="Identity token issuer URL")
    unlink_parser.add_argument("--subject", required=True, help="IdP subject UID (localId)")

    suspend_parser = subparsers.add_parser("suspend", help="Suspend a customer account")
    suspend_parser.add_argument("--customer-id", required=True, help="Customer ID to suspend")

    resolve_parser = subparsers.add_parser("resolve", help="Resolve an IdP identity")
    resolve_parser.add_argument("--issuer", required=True, help="Identity token issuer URL")
    resolve_parser.add_argument("--subject", required=True, help="IdP subject UID (localId)")

    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if args.fake:
        from ticket_router.store.fake import FakeCustomerRepo

        repo: CustomerRepository = FakeCustomerRepo()
    else:
        password = resolve_password(
            cli_password=args.password,
            secret_id=args.password_secret,
            project_id=args.project,
            version=args.password_secret_version,
        )
        repo = build_postgres_repo(
            project_id=args.project,
            region=args.region,
            instance_name=args.instance,
            database=args.database,
            user=args.user,
            password=password,
        )

    if args.command == "link":
        res = link_identity(
            repo=repo,
            issuer=args.issuer,
            subject=args.subject,
            customer_id=args.customer_id,
            actor=args.actor,
            email_verified=args.email_verified,
        )
    elif args.command == "unlink":
        res = unlink_identity(
            repo=repo,
            issuer=args.issuer,
            subject=args.subject,
            actor=args.actor,
        )
    elif args.command == "suspend":
        res = suspend_customer(
            repo=repo,
            customer_id=args.customer_id,
            actor=args.actor,
        )
    elif args.command == "resolve":
        resolution = repo.resolve(args.issuer, args.subject)
        res = {
            "action": "resolve",
            "issuer": args.issuer,
            "subject": args.subject,
            "customer_id": resolution.customer_id,
            "status": resolution.status,
            "linked": resolution.linked,
            "email_verified": resolution.email_verified,
        }
    else:
        logger.error("Unknown command: %s", args.command)
        return 1

    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
