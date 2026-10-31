"""Cost and quota safeguards for synthetic load testing.

# REQ-LOAD-001: Locust Load Testing Profiles and Cost/Quota Safeguards
"""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

from loadtest.profiles import LoadProfile


class CostGuardError(Exception):
    """Raised when an expensive load test is initiated without explicit user confirmation."""


def check_cost_guard(
    profile: LoadProfile,
    i_understand_cost: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    """Verify that user has explicitly acknowledged cost and quota impacts for heavy profiles.

    Args:
        profile: The LoadProfile to be executed.
        i_understand_cost: Boolean flag from CLI argument.
        env: Environment mapping (defaults to os.environ).

    Returns:
        (is_allowed: bool, status_message: str)
    """
    if not profile.requires_cost_ack:
        return (
            True,
            f"Profile '{profile.name}' is within free tier / lightweight quota limits (~{profile.estimated_requests:,} requests).",
        )

    e = env if env is not None else os.environ
    env_confirmed = e.get("I_UNDERSTAND_COST") == "1" or e.get("CONFIRM_LOAD_COST") == "1"

    if i_understand_cost or env_confirmed:
        return (
            True,
            f"[COST CONFIRMED] Explicit confirmation received. Proceeding with '{profile.name}' "
            f"(~{profile.estimated_requests:,} estimated requests over {profile.duration_s // 60} min).",
        )

    minutes = profile.duration_s // 60
    msg = (
        f"[COST/QUOTA GUARD ACTIVATED] Refusing to start load profile '{profile.name}'.\n"
        f"Estimated workload: ~{profile.estimated_requests:,} requests over {minutes} minutes "
        f"(target {profile.target_qps:.1f} msg/s).\n"
        f"This profile will consume significant Vertex AI Agent Engine, Gemini, and Cloud Run quota.\n"
        f"To execute this test, provide explicit confirmation:\n"
        f"  CLI: --i-understand-cost\n"
        f"  ENV: export I_UNDERSTAND_COST=1"
    )
    return (False, msg)


def assert_cost_guard(
    profile: LoadProfile,
    i_understand_cost: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> None:
    """Enforce cost guard and raise CostGuardError if confirmation is missing."""
    allowed, message = check_cost_guard(profile, i_understand_cost=i_understand_cost, env=env)
    if not allowed:
        raise CostGuardError(message)
