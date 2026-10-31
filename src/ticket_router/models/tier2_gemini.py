"""Tier 2 and Tier 3 Gemini invocation with call-time 429 fault-injection hook.

# REQ-CHAOS-001: Chaos Engineering Fault Injection, Probing, and Safe Restoration
# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from ticket_router.config.models import model_id
from ticket_router.models.router_s1_client import RouterPrediction

logger = logging.getLogger(__name__)

CHAOS_SIMULATE_GEMINI_429_ENV = "CHAOS_SIMULATE_GEMINI_429"


def check_gemini_429_hook() -> None:
    """Evaluate CHAOS_SIMULATE_GEMINI_429 environment variable at call time.

    When set to '1', raises google.genai.errors.ClientError with code 429
    (Resource Exhausted) before any network call.
    """
    if os.environ.get(CHAOS_SIMULATE_GEMINI_429_ENV) == "1":
        from google.genai.errors import ClientError

        logger.warning(
            "Chaos fault injection active: %s=1 triggering simulated Gemini 429 ClientError",
            CHAOS_SIMULATE_GEMINI_429_ENV,
        )
        raise ClientError(
            429,
            {
                "error": {
                    "code": 429,
                    "message": "Resource has been exhausted (e.g. check quota).",
                    "status": "RESOURCE_EXHAUSTED",
                }
            },
        )


def invoke_tier2_gemini(
    text: str,
    target_model: Optional[str] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
    client: Optional[Any] = None,
    deadline_s: Optional[float] = None,
    hedge_after_s: Optional[float] = None,
) -> RouterPrediction:
    """Execute Tier 2 Gemini call with call-time 429 fault-injection check."""
    check_gemini_429_hook()
    from ticket_router.agents.triage_router.agent import invoke_tier2_gemini as _t2_impl

    resolved_model = target_model or model_id("router_tier2")
    return _t2_impl(text, target_model=resolved_model, project=project, location=location)


class Tier2GeminiInvoker:
    """Callable wrapper for Tier 2 Gemini Flash invocation."""

    def __init__(
        self,
        target_model: Optional[str] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        client: Optional[Any] = None,
    ) -> None:
        self.target_model = target_model
        self.project = project
        self.location = location
        self.client = client

    def __call__(self, text: str) -> RouterPrediction:
        return invoke_tier2_gemini(
            text,
            target_model=self.target_model,
            project=self.project,
            location=self.location,
            client=self.client,
        )


def default_tier2_invoker(text: str) -> RouterPrediction:
    """Convenience callable matching Callable[[str], RouterPrediction] protocol."""
    return invoke_tier2_gemini(text)


def create_tier2_invoker(
    target_model: Optional[str] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
    client: Optional[Any] = None,
) -> Callable[[str], RouterPrediction]:
    """Factory creating a Tier 2 Gemini invoker."""
    return Tier2GeminiInvoker(
        target_model=target_model,
        project=project,
        location=location,
        client=client,
    )


def invoke_tier3_gemini(
    text: str,
    target_model: Optional[str] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
    deadline_s: Optional[float] = None,
) -> RouterPrediction:
    """Execute Tier 3 Gemini call with call-time 429 fault-injection check."""
    check_gemini_429_hook()
    from ticket_router.agents.triage_router.tier3 import invoke_tier3_gemini as _t3_impl

    return _t3_impl(
        text,
        target_model=target_model,
        project=project,
        location=location,
        deadline_s=deadline_s,
    )
