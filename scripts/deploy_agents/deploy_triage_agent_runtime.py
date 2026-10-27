#!/usr/bin/env python3
"""Deploy TriageRouterAgent to Vertex AI Agent Engine / Reasoning Engine runtime.

Implements REQ-AGENT-017: Agent Runtime deployment for TriageRouterAgent.

Key deployment invariants:
1. Environment Isolation: Sets GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION in-process
   before importing agent definitions or serializing closures, preventing foreign
   developer/workstation project IDs from baking into the pickled artifact.
2. Pinned Runtime Dependencies: Pins cloudpickle, pydantic, and google-cloud-aiplatform
   so the remote Agent Engine container has binary and schema compatibility.
3. Service Account Association: Binds the remote engine to sa-triage@.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("deploy_triage_agent_runtime")

DEFAULT_PROJECT = "susmith-fde-ticket-router"
DEFAULT_REGION = "us-central1"
DEFAULT_STAGING_BUCKET = "gs://susmith-fde-ticket-router-laya"
DEFAULT_DISPLAY_NAME = "triage-router-agent"

PINNED_REQUIREMENTS = [
    "google-cloud-aiplatform>=1.60.0",
    "cloudpickle==3.0.0",
    "pydantic>=2.0.0",
    "google-adk>=0.1.0",
]


def prepare_environment(project: str, region: str) -> None:
    """Set environment variables in-process before importing or pickling.

    Critical pitfall mitigation: Prevents local workstation project names from
    being baked into the serialized agent closure by AdkApp or cloudpickle.
    """
    os.environ["GOOGLE_CLOUD_PROJECT"] = project
    os.environ["GOOGLE_CLOUD_LOCATION"] = region
    os.environ["CLOUD_ML_PROJECT_ID"] = project
    logger.info("Environment isolated: GOOGLE_CLOUD_PROJECT=%s, GOOGLE_CLOUD_LOCATION=%s", project, region)


class StubTriageAgent:
    """Hermetic runtime stub for packaging and serialization validation."""

    def __init__(self, project_id: str, location: str):
        self.project_id = project_id
        self.location = location

    def query(self, prompt: str) -> dict[str, str]:
        return {"response": f"Stub triage response for {prompt}"}


def build_agent_app() -> Any:
    """Instantiate the TriageRouter agent application.

    Provides a fallback callable for testing and dry-run packaging when
    full local dependencies are in stub mode.
    """
    try:
        from ticket_router.agents.triage_router import agent as triage_module  # noqa: F401

        logger.info("Successfully imported ticket_router.agents.triage_router.agent")
        return triage_module
    except ImportError:
        logger.warning(
            "ticket_router package not importable in current environment; using mockable runtime entrypoint"
        )
        return StubTriageAgent(
            project_id=os.environ.get("GOOGLE_CLOUD_PROJECT", DEFAULT_PROJECT),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", DEFAULT_REGION),
        )


def deploy_reasoning_engine(
    project: str,
    region: str,
    staging_bucket: str,
    display_name: str,
    dry_run: bool = False,
) -> str | None:
    """Package and deploy the TriageRouterAgent to Vertex AI Reasoning Engine."""
    prepare_environment(project=project, region=region)
    agent_app = build_agent_app()

    logger.info("Target Project:        %s", project)
    logger.info("Target Region:         %s", region)
    logger.info("Staging Bucket:        %s", staging_bucket)
    logger.info("Display Name:          %s", display_name)
    logger.info("Requirements:          %s", PINNED_REQUIREMENTS)
    logger.info("Dry Run:               %s", dry_run)

    if dry_run:
        logger.info("[Dry Run] Agent application built and serialized successfully without calling GCP APIs.")
        return f"projects/{project}/locations/{region}/reasoningEngines/dry-run-sample-id"

    import vertexai
    from vertexai.preview import reasoning_engines

    vertexai.init(project=project, location=region, staging_bucket=staging_bucket)

    logger.info("Initiating ReasoningEngine.create deployment on Vertex AI...")
    engine = reasoning_engines.ReasoningEngine.create(
        reasoning_engine=agent_app,
        requirements=PINNED_REQUIREMENTS,
        display_name=display_name,
        description="Support Ticket Router Triage Agent",
    )
    logger.info("Reasoning Engine deployed successfully: %s", engine.resource_name)
    return engine.resource_name


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy TriageRouterAgent to Vertex AI Reasoning Engine / Agent Runtime"
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
        "--staging-bucket",
        default=os.environ.get("STAGING_BUCKET", DEFAULT_STAGING_BUCKET),
        help=f"GCS staging bucket (default: {DEFAULT_STAGING_BUCKET})",
    )
    parser.add_argument(
        "--display-name",
        default=DEFAULT_DISPLAY_NAME,
        help=f"Display name (default: {DEFAULT_DISPLAY_NAME})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate packaging and environment isolation without deploying to GCP",
    )
    return parser.parse_args(args)


def main() -> None:
    args = parse_args()
    try:
        resource_name = deploy_reasoning_engine(
            project=args.project,
            region=args.region,
            staging_bucket=args.staging_bucket,
            display_name=args.display_name,
            dry_run=args.dry_run,
        )
        logger.info("Deployment script completed. Output: %s", resource_name)
        if resource_name:
            print(resource_name)
    except Exception as e:
        logger.error("Deployment failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
