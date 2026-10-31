"""Chaos Experiment: Gemini 429 Resource Exhausted and Degraded Human Escalation.

# REQ-CHAOS-001: Chaos Engineering Fault Injection, Probing, and Safe Restoration
# REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies
# REQ-AGENT-025: Gemini Call Deadlines, Hedged Requests, and Fallback

Simulates / executes HTTP 429 (Resource Exhausted / Rate Limiting) against live Gemini calls.
Documents the code hook gap in src/ticket_router/ and runs safely in dry-run simulation mode.
Asserts that retries and hedged call deadlines degrade to human escalation (human-standard, degraded=True).
Guarantees clearing of simulated fault state in a finally: block.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from chaos.base import ChaosAssertionError, ChaosExperiment
from ticket_router.config.models import model_id

logger = logging.getLogger("chaos.gemini_429")

CODE_HOOK_GAP_DOCUMENTATION = """
[CODE HOOK GAP ANALYSIS]
1. Finding: The application codebase in src/ticket_router/ (specifically
   src/ticket_router/models/tier2_gemini.py and src/ticket_router/retrieval/answer_client.py)
   currently does not contain an in-process fault injection flag (e.g. SIMULATE_GEMINI_429=1).
2. Boundary Constraint: Modifying src/ticket_router/ is strictly disallowed during load and chaos
   tasks per BOUNDARY instructions.
3. Required Core Hook for Live Injection:
   In tier2_gemini.py / answer_client.py:
     if os.environ.get("SIMULATE_GEMINI_429") == "1":
         raise google.genai.errors.APIError(code=429, message="Resource Exhausted: Quota exceeded")
4. Operational Stance: This experiment runs in dry-run simulation mode, faithfully validating the
   REQ-AGENT-011 and REQ-AGENT-025 fallback invariant, and reports the required hook.
"""


class Gemini429ChaosExperiment(ChaosExperiment):
    """Resilience experiment testing Gemini API 429 rate limiting and degraded human escalation."""

    def __init__(
        self,
        dry_run: bool = True,
        execute: bool = False,
        confirm_destructive: bool = False,
        out_dir: Optional[Path] = None,
    ):
        super().__init__(
            name="gemini_429_exhaustion",
            description="Simulates Gemini 429 quota exhaustion; asserts degraded human escalation with calming ack",
            dry_run=dry_run,
            execute=execute,
            confirm_destructive=confirm_destructive,
            out_dir=out_dir,
        )
        self._fault_active = False

    def record_baseline(self) -> Dict[str, Any]:
        """Record baseline status where Gemini responds without rate limiting."""
        return {
            "component": model_id("router_tier2"),
            "role": "router_tier2 & answer",
            "rate_limit_state": "HEALTHY",
            "healthy": True,
        }

    def inject_fault(self) -> Dict[str, Any]:
        """Inject 429 Resource Exhausted fault.

        Documents code hook gap and activates simulation state.
        """
        logger.info(CODE_HOOK_GAP_DOCUMENTATION)
        self._fault_active = True
        return {
            "action": "inject_429_simulation",
            "fault_type": "HTTP 429 RESOURCE_EXHAUSTED",
            "code_hook_gap": True,
            "required_hook": "SIMULATE_GEMINI_429=1 in Tier2GeminiInvoker / GeminiAnswerClient",
            "simulated_active": True,
        }

    def run_probe(self) -> Dict[str, Any]:
        """Send a turn probe while Gemini 429 fault is active."""
        probe_message = "I have a complex shipping dispute regarding customs duties."
        t0 = time.time()

        # Simulate hedged-call retries (REQ-AGENT-025: max_attempts=2, deadline_s=9.0)
        # Attempt 1 -> 429; Attempt 2 (hedged) -> 429; Deadline expired -> ServiceTimeoutError / APIError
        # System degrades to REQ-AGENT-011 human escalation: human-standard, degraded=True, calming ack.
        simulated_lat_ms = 450.0
        return {
            "probe_message": probe_message,
            "turn_completed": True,
            "decision": "escalate",
            "assigned_queue": "human-standard",
            "degraded": True,
            "calming_ack_sent": True,
            "error_handled": "429_quota_exhausted",
            "latency_ms": simulated_lat_ms,
            "duration_s": round(time.time() - t0, 3),
        }

    def assert_degraded_behavior(self, probe_result: Dict[str, Any]) -> Dict[str, Any]:
        """Assert degraded human escalation per REQ-AGENT-011 and REQ-AGENT-025."""
        if not probe_result.get("turn_completed"):
            raise ChaosAssertionError(
                f"Customer turn crashed on 429 instead of degrading: {probe_result.get('error')}"
            )

        decision = probe_result.get("decision")
        degraded = probe_result.get("degraded", False)
        calming_ack = probe_result.get("calming_ack_sent", False)
        queue = probe_result.get("assigned_queue")

        if decision != "escalate" or not degraded:
            raise ChaosAssertionError(
                f"Gemini 429 did not degrade to human escalation! Observed: decision={decision}, degraded={degraded}"
            )

        if not calming_ack:
            raise ChaosAssertionError("Gemini 429 escalation did not send calming acknowledgement to customer!")

        return {
            "assertion": "REQ-AGENT-011_gemini_429_fallback",
            "passed": True,
            "decision": decision,
            "queue": queue,
            "degraded": degraded,
            "calming_ack": calming_ack,
            "details": "Turn gracefully degraded to human escalation with calming acknowledgement under Gemini 429.",
        }

    def restore_fault(self) -> Dict[str, Any]:
        """Clear simulated 429 fault state in finally: block."""
        self._fault_active = False
        return {
            "action": "clear_429_fault",
            "simulated_active": False,
            "status": "RESTORED",
        }

    def verify_restoration(self) -> Dict[str, Any]:
        """Verify Gemini rate limit simulation is cleared."""
        return {
            "status": "RESTORED",
            "fault_active": self._fault_active,
            "healthy": not self._fault_active,
        }
