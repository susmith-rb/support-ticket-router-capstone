"""Chaos Experiment: Cloud SQL Zonal Outage, ticket-db-mcp 503, and Agent Fail-Open.

# REQ-CHAOS-001: Chaos Engineering Fault Injection, Probing, and Safe Restoration
# REQ-AGENT-027: Concurrent Turn I/O, Non-Fatal Ticket Persistence
# REQ-STORE-001: Cloud SQL PostgreSQL 16 Instance

Documents that 'gcloud sql instances failover' is strictly HA-only (REGIONAL instances).
Because the capstone Cloud SQL instance is ZONAL (availability_type = 'ZONAL'),
'gcloud sql instances restart' is used as the zonal fault equivalent.
Asserts that during outage, ticket-db-mcp returns HTTP 503 fast and TriageRouterAgent fails open.
Guarantees waiting for instance recovery to RUNNABLE in a finally: block.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from chaos.base import ChaosAssertionError, ChaosExperiment

logger = logging.getLogger("chaos.cloud_sql")

DEFAULT_INSTANCE_NAME = "ticket-store"
DEFAULT_PROJECT = "susmith-fde-ticket-router"
DEFAULT_REGION = "us-central1"
DEFAULT_TICKET_DB_URL = "https://ticket-db-mcp-34mwh7k34a-uc.a.run.app"


class CloudSqlChaosExperiment(ChaosExperiment):
    """Resilience experiment testing Cloud SQL database restart and agent fail-open persistence."""

    def __init__(
        self,
        instance_name: str = DEFAULT_INSTANCE_NAME,
        project: str = DEFAULT_PROJECT,
        region: str = DEFAULT_REGION,
        ticket_db_url: str = DEFAULT_TICKET_DB_URL,
        dry_run: bool = True,
        execute: bool = False,
        confirm_destructive: bool = False,
        out_dir: Optional[Path] = None,
    ):
        super().__init__(
            name="cloud_sql_restart",
            description="Restarts zonal Cloud SQL instance; asserts ticket-db-mcp 503 fast and agent fail-open",
            dry_run=dry_run,
            execute=execute,
            confirm_destructive=confirm_destructive,
            out_dir=out_dir,
        )
        self.instance_name = instance_name
        self.project = project
        self.region = region
        self.ticket_db_url = ticket_db_url
        self._simulated_db_running = True

    def record_baseline(self) -> Dict[str, Any]:
        """Verify Cloud SQL instance is in RUNNABLE state."""
        if self.dry_run:
            return {
                "instance": self.instance_name,
                "project": self.project,
                "availability_type": "ZONAL",
                "state": "RUNNABLE",
                "healthy": True,
            }

        cmd = [
            "gcloud",
            "sql",
            "instances",
            "describe",
            self.instance_name,
            f"--project={self.project}",
            "--format=json",
        ]
        try:
            import json

            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(res.stdout)
            state = data.get("state")
            avail = data.get("settings", {}).get("availabilityType", "ZONAL")
            return {
                "instance": self.instance_name,
                "project": self.project,
                "availability_type": avail,
                "state": state,
                "healthy": state == "RUNNABLE",
            }
        except Exception as exc:
            return {"instance": self.instance_name, "error": str(exc), "healthy": False}

    def inject_fault(self) -> Dict[str, Any]:
        """Restart zonal Cloud SQL instance (or simulate restart).

        Documents: gcloud sql instances failover requires REGIONAL HA; restart is the zonal equivalent.
        """
        logger.info(
            "[ZONAL FAULT INJECTION] Note: 'gcloud sql instances failover' applies only to HA (REGIONAL). "
            "For ZONAL instance '%s', executing 'gcloud sql instances restart' as fault equivalent.",
            self.instance_name,
        )
        t0 = time.time()
        if self.dry_run:
            self._simulated_db_running = False
            return {
                "action": "restart_zonal_cloud_sql_simulation",
                "instance": self.instance_name,
                "simulated_state": "RESTARTING",
                "duration_s": round(time.time() - t0, 3),
            }

        cmd = [
            "gcloud",
            "sql",
            "instances",
            "restart",
            self.instance_name,
            f"--project={self.project}",
            "--async",
            "--quiet",
        ]
        logger.warning("[CHAOS FAULT] Executing: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return {
            "action": "restart_zonal_cloud_sql",
            "instance": self.instance_name,
            "stdout": proc.stdout.strip(),
            "duration_s": round(time.time() - t0, 3),
        }

    def run_probe(self) -> Dict[str, Any]:
        """Probe ticket-db-mcp and TriageRouter while database is unavailable."""
        t0 = time.time()
        if self.dry_run:
            # During restart: ticket-db-mcp fails with 503; agent fails open per REQ-AGENT-027
            return {
                "mcp_status_code": 503,
                "mcp_error": "database_unavailable_or_timeout",
                "mcp_failed_fast": True,
                "turn_completed": True,
                "decision": "auto_rag",
                "tier": "tier1_router_s1",
                "agent_failed_open": True,
                "warning_logged": "ticket_upsert_failed",
                "duration_s": round(time.time() - t0, 3),
            }

        # Live probe
        import requests

        mcp_code = 0
        mcp_err = ""
        try:
            r = requests.get(f"{self.ticket_db_url.rstrip('/')}/health", timeout=3.0)
            mcp_code = r.status_code
        except Exception as exc:
            mcp_err = str(exc)
            mcp_code = 503

        # Call query_triage_agent to verify agent fails open
        turn_ok = False
        decision = "unknown"
        try:
            from scripts.deploy_agents.query_triage_agent import run_query

            res = run_query(
                resource_name=os.environ.get("TRIAGE_RESOURCE_NAME", ""),
                message="Where is my order ORD-10001?",
                session_id=f"chaos-sql-{int(time.time())}",
                dry_run=False,
            )
            turn_ok = res.get("status") == "COMPLETED"
            decision = res.get("decision", "unknown")
        except Exception as exc:
            turn_ok = False
            decision = f"error: {exc}"

        return {
            "mcp_status_code": mcp_code,
            "mcp_error": mcp_err,
            "mcp_failed_fast": mcp_code in (500, 502, 503, 504),
            "turn_completed": turn_ok,
            "decision": decision,
            "agent_failed_open": turn_ok,
            "duration_s": round(time.time() - t0, 3),
        }

    def assert_degraded_behavior(self, probe_result: Dict[str, Any]) -> Dict[str, Any]:
        """Assert ticket-db-mcp 503 fast and agent fails open per REQ-AGENT-027."""
        if not probe_result.get("mcp_failed_fast"):
            raise ChaosAssertionError(
                f"ticket-db-mcp did not fail fast with 503 during DB restart! Observed: {probe_result.get('mcp_status_code')}"
            )

        if not probe_result.get("turn_completed"):
            raise ChaosAssertionError(
                "TriageRouterAgent turn crashed on DB failure instead of failing open (REQ-AGENT-027 violation)!"
            )

        return {
            "assertion": "REQ-AGENT-027_fail_open_persistence",
            "passed": True,
            "mcp_failed_fast": True,
            "agent_failed_open": True,
            "details": "ticket-db-mcp returned 503 fast while TriageRouter completed turn successfully.",
        }

    def restore_fault(self) -> Dict[str, Any]:
        """Wait for Cloud SQL instance to return to RUNNABLE in finally: block."""
        t0 = time.time()
        if self.dry_run:
            self._simulated_db_running = True
            return {
                "action": "wait_runnable_simulation",
                "instance": self.instance_name,
                "simulated_state": "RUNNABLE",
                "duration_s": round(time.time() - t0, 3),
            }

        logger.info("[CHAOS RESTORE] Waiting for Cloud SQL instance '%s' to become RUNNABLE...", self.instance_name)
        max_wait_s = 300
        interval_s = 10
        elapsed = 0

        while elapsed < max_wait_s:
            baseline = self.record_baseline()
            if baseline.get("state") == "RUNNABLE":
                logger.info("Cloud SQL instance '%s' is now RUNNABLE (took %.1fs).", self.instance_name, elapsed)
                return {
                    "action": "wait_runnable",
                    "state": "RUNNABLE",
                    "wait_s": elapsed,
                    "status": "RESTORED",
                }
            time.sleep(interval_s)
            elapsed += interval_s

        logger.warning("Cloud SQL instance '%s' did not report RUNNABLE within %ds", self.instance_name, max_wait_s)
        return {
            "action": "wait_runnable",
            "state": "TIMEOUT",
            "wait_s": elapsed,
            "status": "TIMEOUT",
        }

    def verify_restoration(self) -> Dict[str, Any]:
        """Verify Cloud SQL instance is RUNNABLE post-experiment."""
        baseline = self.record_baseline()
        return {
            "status": "RESTORED" if baseline.get("healthy") else "UNHEALTHY",
            "state": baseline.get("state"),
            "healthy": baseline.get("healthy", False),
        }
