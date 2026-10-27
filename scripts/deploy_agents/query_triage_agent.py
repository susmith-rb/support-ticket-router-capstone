#!/usr/bin/env python3
"""CLI utility to query a deployed TriageRouterAgent on Vertex AI Agent Engine.

# REQ-AGENT-017: Agent Runtime & Vertex AI Agent Engine Deployment
# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution via ticket-db-mcp
# REQ-IDENT-008: End-to-End Identity Platform Verification and Threat Audit

Usage:
    # Query with raw Identity Platform ID token:
    python scripts/deploy_agents/query_triage_agent.py \\
        --resource-name projects/620485621180/locations/us-central1/reasoningEngines/8384262420809383936 \\
        --message "Where is my order ORD-10001?" \\
        --customer-id-token "<JWT>"

    # Query as demo user 1001 or 1002 (mints token automatically):
    python scripts/deploy_agents/query_triage_agent.py \\
        --resource-name projects/620485621180/locations/us-central1/reasoningEngines/8384262420809383936 \\
        --message "Where is my order ORD-10001?" \\
        --demo-user 1001

    # Local dry-run simulation:
    python scripts/deploy_agents/query_triage_agent.py \\
        --resource-name dummy \\
        --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("query_triage_agent")

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

VENV_SITE_PACKAGES = (
    REPO_ROOT / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
)
if VENV_SITE_PACKAGES.is_dir() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

DEFAULT_PROJECT = "susmith-fde-ticket-router"
DEFAULT_REGION = "us-central1"
DEFAULT_USER_ID = "caller-client"


def resolve_demo_user_token(demo_user: str) -> Optional[str]:
    """Obtain Identity Platform ID token for demo user by executing seed_identity_test_users.py.

    Security Invariant: The token is captured directly from stdout and never logged or printed.
    """
    seed_script = REPO_ROOT / "scripts" / "seed_identity_test_users.py"
    if not seed_script.is_file():
        logger.error("Seed script not found at: %s", seed_script)
        return None

    cmd = [
        sys.executable,
        str(seed_script),
        "--print-id-token",
        str(demo_user),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        token = proc.stdout.strip()
        if token:
            return token
        logger.error("Empty token returned for demo user %s", demo_user)
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to mint demo token for user %s: %s (stderr: %s)", demo_user, exc, exc.stderr)
    except Exception as exc:
        logger.error("Unexpected error obtaining demo token for user %s: %s", demo_user, exc)
    return None


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query deployed TriageRouterAgent on Vertex AI Agent Engine"
    )
    parser.add_argument(
        "--resource-name",
        required=True,
        help="Full resource name or ID of the deployed Agent Engine / Reasoning Engine",
    )
    parser.add_argument(
        "--message",
        default="I need help with my recent order delivery status.",
        help="Inbound user message text",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Optional session identifier for stateful tracing (generated if omitted)",
    )
    parser.add_argument(
        "--customer-id-token",
        default=None,
        help="Raw customer Identity Platform ID token (JWT) for cryptographic verification",
    )
    parser.add_argument(
        "--demo-user",
        choices=["1001", "1002"],
        default=None,
        help="Demo customer account (1001 or 1002) to mint token via seed_identity_test_users.py",
    )
    parser.add_argument(
        "--user-id",
        default=DEFAULT_USER_ID,
        help="Opaque caller ID for session management (never used as customer_id)",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("PROJECT_ID", DEFAULT_PROJECT),
        help=f"GCP Project ID (default: {DEFAULT_PROJECT})",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("REGION", DEFAULT_REGION),
        help=f"GCP Region (default: {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Print raw streaming event chunks as they arrive",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution without network requests to Vertex AI",
    )
    return parser.parse_args(args)


def extract_text_from_event(event: Any) -> str:
    """Extract plain text message from stream event without reflection."""
    if isinstance(event, dict):
        content = event.get("content")
        if isinstance(content, dict):
            parts = content.get("parts", [])
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p]
            if texts:
                return " ".join(texts)
        output = event.get("output")
        if isinstance(output, dict) and "answer" in output:
            return str(output["answer"])
        if "text" in event:
            return str(event["text"])
    else:
        content = getattr(event, "content", None)
        if content:
            parts = getattr(content, "parts", None)
            if parts:
                texts = [getattr(p, "text", "") for p in parts if hasattr(p, "text")]
                if texts:
                    return " ".join(texts)
        output = getattr(event, "output", None)
        if isinstance(output, dict) and "answer" in output:
            return str(output["answer"])
    return ""


def extract_ticket_fields_from_event(event: Any) -> Dict[str, Any]:
    """Extract ticket/decision metadata from event output if present."""
    fields: Dict[str, Any] = {}
    output = event.get("output") if isinstance(event, dict) else getattr(event, "output", None)
    if isinstance(output, dict):
        for k in ("ticket_id", "decision", "queue", "answer"):
            if k in output:
                fields[k] = output[k]
    return fields


def run_query(
    resource_name: str,
    message: str,
    session_id: Optional[str] = None,
    customer_id_token: Optional[str] = None,
    demo_user: Optional[str] = None,
    user_id: str = DEFAULT_USER_ID,
    project: str = DEFAULT_PROJECT,
    region: str = DEFAULT_REGION,
    stream: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Execute streaming query against deployed Agent Engine or simulate in dry run.

    Security Invariant: The token is passed via session state and NEVER logged or printed.
    """
    effective_token = customer_id_token
    if not effective_token and demo_user:
        logger.info("Resolving demo user token for user=%s...", demo_user)
        effective_token = resolve_demo_user_token(demo_user)
        if not effective_token:
            logger.warning("Could not resolve token for demo user %s; proceeding unauthenticated", demo_user)

    logger.info("Target Resource:   %s", resource_name)
    logger.info("Project:           %s", project)
    logger.info("Region:            %s", region)
    logger.info("Caller User ID:    %s", user_id)
    logger.info("Authenticated:     %s", bool(effective_token))
    logger.info("Stream Mode:       %s", stream)
    logger.info("Message:           %s", message)

    if dry_run:
        logger.info("[Dry Run] Simulating query to %s without dialing Vertex AI.", resource_name)
        active_sess = session_id or "session-dryrun-001"
        simulated_response = {
            "ticket_id": f"TICK-{active_sess[:8]}",
            "session_id": active_sess,
            "decision": "auto_rag",
            "queue": "automated",
            "calming_ack": None,
            "intent": "shipping_status",
            "answer": "Your package is currently in transit and scheduled for delivery tomorrow.",
            "customer_verified": bool(effective_token),
            "latency_ms": 42.0,
            "dry_run": True,
        }
        return simulated_response

    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=project, location=region)
    engine = agent_engines.get(resource_name)

    initial_state: Dict[str, Any] = {}
    if effective_token:
        initial_state["customer_id_token"] = effective_token

    # Create session with caller user_id and initial session state containing customer_id_token
    logger.info("Creating session on Agent Engine...")
    session = engine.create_session(
        user_id=user_id,
        session_id=session_id,
        state=initial_state if initial_state else None,
    )
    active_session_id = (
        session.get("id") if isinstance(session, dict)
        else getattr(session, "id", None)
        or (getattr(session, "name", "").split("/")[-1] if getattr(session, "name", None) else None)
        or session_id
        or "sess-live"
    )
    logger.info("Active Session ID: %s", active_session_id)

    # Stream query in that session
    events: List[Any] = []
    text_chunks: List[str] = []
    ticket_fields: Dict[str, Any] = {}

    for event in engine.stream_query(
        message=message,
        user_id=user_id,
        session_id=active_session_id,
    ):
        events.append(event)
        if stream:
            print(f"[STREAM EVENT] {event}")
        chunk_text = extract_text_from_event(event)
        if chunk_text:
            text_chunks.append(chunk_text)
        ticket_fields.update(extract_ticket_fields_from_event(event))

    final_text = " ".join(text_chunks).strip() if text_chunks else ""
    return {
        "text": final_text,
        "answer": final_text,
        "session_id": active_session_id,
        "ticket_fields": ticket_fields,
        "ticket_id": ticket_fields.get("ticket_id"),
        "decision": ticket_fields.get("decision"),
        "queue": ticket_fields.get("queue"),
        "event_count": len(events),
        "status": "COMPLETED",
        "dry_run": False,
    }


def main() -> None:
    args = parse_args()
    try:
        result = run_query(
            resource_name=args.resource_name,
            message=args.message,
            session_id=args.session_id,
            customer_id_token=args.customer_id_token,
            demo_user=args.demo_user,
            user_id=args.user_id,
            project=args.project,
            region=args.region,
            stream=args.stream,
            dry_run=args.dry_run,
        )
        print("\n=== Agent Engine Query Result ===")
        print(f"Session ID:    {result.get('session_id')}")
        if result.get("ticket_id"):
            print(f"Ticket ID:     {result.get('ticket_id')}")
        if result.get("decision"):
            print(f"Decision:      {result.get('decision')}")
        if result.get("queue"):
            print(f"Queue:         {result.get('queue')}")
        print(f"Answer:        {result.get('text') or result.get('answer')}")
    except Exception as e:
        logger.error("Query failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
