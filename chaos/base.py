"""Base class and lifecycle management for chaos engineering experiments.

# REQ-CHAOS-001: Chaos Engineering Fault Injection, Probing, and Safe Restoration
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("chaos.base")

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = REPO_ROOT / "evals" / "results" / "chaos"


class ChaosSafetyError(Exception):
    """Raised when an active chaos experiment is attempted without proper safety confirmation."""


class ChaosAssertionError(Exception):
    """Raised when the probed system behavior diverges from documented resilience invariants."""


class ChaosExperiment(ABC):
    """Abstract base class for resilient, safe chaos experiments.

    Enforces the 6-phase lifecycle:
      1. Baseline measurement
      2. Fault injection
      3. Degraded probe
      4. Assertion of documented fallback behavior (REQ-AGENT-011)
      5. Guaranteed restoration in a finally: block
      6. Post-restore verification & JSON reporting
    """

    def __init__(
        self,
        name: str,
        description: str,
        dry_run: bool = True,
        execute: bool = False,
        confirm_destructive: bool = False,
        out_dir: Optional[Path] = None,
    ):
        self.name = name
        self.description = description
        self.dry_run = dry_run
        self.execute = execute
        self.confirm_destructive = confirm_destructive
        self.out_dir = out_dir or RESULTS_ROOT

    def _validate_safety(self) -> None:
        """Enforce strict confirmation flags before running against live cloud services."""
        if self.execute:
            if not self.confirm_destructive:
                raise ChaosSafetyError(
                    f"[CHAOS SAFETY GATE] Experiment '{self.name}' requested live execution (--execute) "
                    "without explicit safety confirmation (--confirm-destructive). Aborting to prevent outages."
                )
            logger.warning(
                "[CHAOS LIVE EXECUTION CONFIRMED] Running '%s' with LIVE fault injection against Google Cloud!",
                self.name,
            )
        else:
            logger.info("[CHAOS DRY RUN] Running '%s' in safe simulation mode (no mutations).", self.name)

    @abstractmethod
    def record_baseline(self) -> Dict[str, Any]:
        """Measure and record operational state prior to fault injection."""

    @abstractmethod
    def inject_fault(self) -> Dict[str, Any]:
        """Apply the fault to target infrastructure or simulated component."""

    @abstractmethod
    def run_probe(self) -> Dict[str, Any]:
        """Execute a short probe query or identity check while the fault is active."""

    @abstractmethod
    def assert_degraded_behavior(self, probe_result: Dict[str, Any]) -> Dict[str, Any]:
        """Assert that observed behavior complies with documented fallback specifications."""

    @abstractmethod
    def restore_fault(self) -> Dict[str, Any]:
        """Restore initial operational configuration. MUST be called in finally: block."""

    @abstractmethod
    def verify_restoration(self) -> Dict[str, Any]:
        """Verify that infrastructure and probes have returned to healthy baseline."""

    def _save_report(self, report: Dict[str, Any]) -> Path:
        """Persist structured JSON report to evals/results/chaos/."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ts_clean = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", self.name).strip("_")
        report_file = self.out_dir / f"{safe_name}_{ts_clean}.json"

        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        logger.info("Saved chaos experiment report to: %s", report_file)
        return report_file

    def run(self) -> Dict[str, Any]:
        """Execute full 6-phase chaos experiment with guaranteed finally: restoration."""
        self._validate_safety()
        start_ts = datetime.now(timezone.utc).isoformat()
        report: Dict[str, Any] = {
            "experiment": self.name,
            "description": self.description,
            "dry_run": self.dry_run,
            "live_execute": self.execute,
            "timestamp_start": start_ts,
            "phases": {},
            "status": "RUNNING",
        }

        try:
            logger.info("Phase 1: Recording baseline...")
            report["phases"]["baseline"] = self.record_baseline()

            logger.info("Phase 2: Injecting fault (%s)...", self.name)
            report["phases"]["fault_injection"] = self.inject_fault()

            logger.info("Phase 3: Running degraded probe...")
            report["phases"]["probe"] = self.run_probe()

            logger.info("Phase 4: Asserting degraded behavior invariants...")
            report["phases"]["assertion"] = self.assert_degraded_behavior(report["phases"]["probe"])

        finally:
            logger.info("Phase 5: Restoring infrastructure state (finally block)...")
            try:
                restore_result = self.restore_fault()
                report["phases"]["restoration"] = restore_result
            except Exception as restore_exc:
                logger.critical("CRITICAL: Failed restoring fault in %s: %s", self.name, restore_exc)
                report["phases"]["restoration"] = {"status": "FAILED", "error": str(restore_exc)}

        logger.info("Phase 6: Verifying post-restoration health...")
        report["phases"]["post_restore_verification"] = self.verify_restoration()
        report["status"] = "COMPLETED"
        report["timestamp_end"] = datetime.now(timezone.utc).isoformat()

        report_file = self._save_report(report)
        report["report_file"] = str(report_file)
        return report
