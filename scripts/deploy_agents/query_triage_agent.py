#!/usr/bin/env python3
"""CLI utility to query a deployed TriageRouterAgent on Vertex AI Agent Engine.

Fulfills REQ-AGENT-017:
- Connects to deployed Agent Engine resource via vertexai.agent_engines.get() or ReasoningEngine.
- Supports streaming (:streamQuery) or synchronous (:query) execution.
- Formats and prints ticket ID, routing decision, queue, and answer/calming acknowledgement.
- Includes --dry-run mode for local test validation.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any

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


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
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
        default="session-cli-test",
        help="Session identifier for stateful tracing",
    )
    parser.add_argument(
        "--customer-id",
        default="cust-cli-test",
        help="Customer account identifier",
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
        help="Use stream_query instead of query",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution without network requests to Vertex AI",
    )
    return parser.parse_args(args)


def run_query(
    resource_name: str,
    message: str,
    session_id: str,
    customer_id: str,
    project: str,
    region: str,
    stream: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute query against deployed Agent Engine or simulate in dry run."""
    logger.info("Target Resource:   %s", resource_name)
    logger.info("Project:           %s", project)
    logger.info("Region:            %s", region)
    logger.info("Session ID:        %s", session_id)
    logger.info("Customer ID:       %s", customer_id)
    logger.info("Stream Mode:       %s", stream)
    logger.info("Message:           %s", message)

    if dry_run:
        logger.info("[Dry Run] Simulating query to %s without dialing Vertex AI.", resource_name)
        simulated_response = {
            "ticket_id": f"TICK-{session_id[:8]}",
            "session_id": session_id,
            "decision": "auto_rag",
            "queue": "automated",
            "calming_ack": None,
            "intent": "shipping_status",
            "answer": "Your package is currently in transit and scheduled for delivery tomorrow.",
            "latency_ms": 42.0,
            "dry_run": True,
        }
        return simulated_response

    import vertexai
    vertexai.init(project=project, location=region)

    engine: Any = None
    try:
        from vertexai import agent_engines
        engine = agent_engines.get(resource_name)
    except (ImportError, Exception):
        from vertexai.preview import reasoning_engines
        engine = reasoning_engines.ReasoningEngine(resource_name)

    query_kwargs = {
        "message": message,
        "session_id": session_id,
        "customer_id": customer_id,
    }

    if stream:
        logger.info("Executing stream_query...")
        stream_chunks: list[Any] = []
        for chunk in engine.stream_query(**query_kwargs):
            stream_chunks.append(chunk)
            print(f"[STREAM EVENT] {chunk}")
        return {"events": stream_chunks, "status": "COMPLETED"}

    logger.info("Executing query...")
    response = engine.query(**query_kwargs)
    return response if isinstance(response, dict) else {"response": str(response)}


def main() -> None:
    args = parse_args()
    try:
        result = run_query(
            resource_name=args.resource_name,
            message=args.message,
            session_id=args.session_id,
            customer_id=args.customer_id,
            project=args.project,
            region=args.region,
            stream=args.stream,
            dry_run=args.dry_run,
        )
        print("\n=== Agent Engine Query Result ===")
        print(json.dumps(result, indent=2))
    except Exception as e:
        logger.error("Query failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
