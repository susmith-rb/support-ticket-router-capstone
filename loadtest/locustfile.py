"""Locust load test script driving TriageRouter on Vertex AI Agent Engine.

# REQ-LOAD-001: Locust Load Testing Profiles and Cost/Quota Safeguards

Usage:
    # Run smoke test in headless mode:
    locust -f loadtest/locustfile.py --headless --profile=smoke

    # Run sustained test with cost confirmation:
    locust -f loadtest/locustfile.py --headless --profile=sustained --i-understand-cost

    # Run with Locust Web UI:
    locust -f loadtest/locustfile.py
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loadtest.dataset import LoadTestDataset
from loadtest.guard import CostGuardError, assert_cost_guard
from loadtest.profiles import get_profile
from loadtest.stats import compute_load_summary

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("loadtest.locust")

DEFAULT_RESOURCE_NAME = (
    "projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296"
)
DEFAULT_PROJECT = "susmith-fde-ticket-router"
DEFAULT_REGION = "us-central1"
RESULTS_ROOT = REPO_ROOT / "evals" / "results" / "load"

# Global state for Locust run
RUN_RECORDS: List[Dict[str, Any]] = []
RUN_CONFIG: Dict[str, Any] = {
    "profile": "smoke",
    "resource_name": DEFAULT_RESOURCE_NAME,
    "project": DEFAULT_PROJECT,
    "region": DEFAULT_REGION,
    "dry_run": os.environ.get("LOAD_DRY_RUN", "0") == "1",
    "i_understand_cost": os.environ.get("I_UNDERSTAND_COST", "0") == "1",
    "start_time": time.time(),
}

# Try importing Locust components gracefully
try:
    from locust import LoadTestShape, User, between, events, task
    LOCUST_AVAILABLE = True
except ImportError:
    LOCUST_AVAILABLE = False
    User = object  # type: ignore
    LoadTestShape = object  # type: ignore
    def task(func):  # type: ignore
        return func
    def between(a, b):  # type: ignore
        return lambda: a
    events = None  # type: ignore


class TriageAgentUser(User):  # type: ignore
    """Locust User driving TriageRouter turn requests against Vertex AI Agent Engine."""

    wait_time = between(0.01, 0.05)

    def on_start(self) -> None:
        self.dataset = LoadTestDataset(REPO_ROOT)
        self.resource_name = RUN_CONFIG.get("resource_name", DEFAULT_RESOURCE_NAME)
        self.project = RUN_CONFIG.get("project", DEFAULT_PROJECT)
        self.region = RUN_CONFIG.get("region", DEFAULT_REGION)
        self.dry_run = RUN_CONFIG.get("dry_run", False)
        self._tokens: Dict[str, str] = {}
        self.engine = None

        if not self.dry_run:
            try:
                import vertexai
                from vertexai import agent_engines

                vertexai.init(project=self.project, location=self.region)
                self.engine = agent_engines.get(self.resource_name)
            except Exception as exc:
                logger.warning("Could not initialize Agent Engine in on_start: %s", exc)

    def _resolve_token(self, demo_user: Optional[str]) -> Optional[str]:
        if not demo_user:
            return None
        if demo_user in self._tokens:
            return self._tokens[demo_user]
        if self.dry_run:
            self._tokens[demo_user] = f"sim-token-{demo_user}"
            return self._tokens[demo_user]

        try:
            from scripts.deploy_agents.query_triage_agent import resolve_demo_user_token

            tok = resolve_demo_user_token(demo_user)
            if tok:
                self._tokens[demo_user] = tok
                return tok
        except Exception as exc:
            logger.warning("Could not resolve demo token: %s", exc)
        return None

    @task
    def query_turn(self) -> None:
        case = self.dataset.sample()
        t0 = time.perf_counter()
        ts_iso = datetime.now(timezone.utc).isoformat()
        token = self._resolve_token(case.demo_user) if case.oauth else None
        session_id = f"locust-{int(t0 * 1000)}-{random.randint(100, 999)}"

        if self.dry_run:
            is_fast = case.category in ("greeting", "order_auth", "order_anon")
            sleep_s = random.uniform(0.04, 0.12) if is_fast else random.uniform(0.60, 1.80)
            time.sleep(sleep_s)
            lat_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            dec = case.expected_decision or ("escalate" if case.category == "escalate_angry" else "auto_rag")
            tier = "tier0_rules" if case.category == "greeting" else ("tier1_router_s1" if is_fast else "tier2_flash")

            record = {
                "timestamp_iso": ts_iso,
                "case_id": case.case_id,
                "category": case.category,
                "session_id": session_id,
                "latency_ms": lat_ms,
                "success": True,
                "decision": dec,
                "tier": tier,
                "queue": "human-urgent" if dec == "escalate" else "automated",
                "error": "",
            }
            RUN_RECORDS.append(record)

            if LOCUST_AVAILABLE and hasattr(self, "environment") and self.environment:
                self.environment.events.request.fire(
                    request_type="stream_query",
                    name=case.category,
                    response_time=lat_ms,
                    response_length=100,
                    exception=None,
                )
            return

        # Live Agent Engine call
        try:
            if not self.engine:
                import vertexai
                from vertexai import agent_engines

                vertexai.init(project=self.project, location=self.region)
                self.engine = agent_engines.get(self.resource_name)

            initial_state = {"customer_id_token": token} if token else None
            user_id = f"caller-{case.demo_user or 'anon'}"
            session = self.engine.create_session(
                user_id=user_id,
                session_id=session_id,
                state=initial_state,
            )
            active_session_id = getattr(session, "id", None) or session_id

            from scripts.deploy_agents.query_triage_agent import extract_ticket_fields_from_event

            ticket_fields: Dict[str, Any] = {}
            for event in self.engine.stream_query(
                message=case.message,
                user_id=user_id,
                session_id=active_session_id,
            ):
                ticket_fields.update(extract_ticket_fields_from_event(event))

            lat_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            record = {
                "timestamp_iso": ts_iso,
                "case_id": case.case_id,
                "category": case.category,
                "session_id": active_session_id,
                "latency_ms": lat_ms,
                "success": True,
                "decision": ticket_fields.get("decision", "auto_rag"),
                "tier": ticket_fields.get("tier", "unknown"),
                "queue": ticket_fields.get("queue", "none"),
                "error": "",
            }
            RUN_RECORDS.append(record)
            if LOCUST_AVAILABLE and hasattr(self, "environment") and self.environment:
                self.environment.events.request.fire(
                    request_type="stream_query",
                    name=case.category,
                    response_time=lat_ms,
                    response_length=200,
                    exception=None,
                )
        except Exception as exc:
            lat_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            record = {
                "timestamp_iso": ts_iso,
                "case_id": case.case_id,
                "category": case.category,
                "session_id": session_id,
                "latency_ms": lat_ms,
                "success": False,
                "decision": "error",
                "tier": "none",
                "queue": "none",
                "error": str(exc),
            }
            RUN_RECORDS.append(record)
            if LOCUST_AVAILABLE and hasattr(self, "environment") and self.environment:
                self.environment.events.request.fire(
                    request_type="stream_query",
                    name=case.category,
                    response_time=lat_ms,
                    response_length=0,
                    exception=exc,
                )


class TriageLoadShape(LoadTestShape):  # type: ignore
    """Locust dynamic load shape driving profiles: smoke, sustained, spike."""

    def tick(self) -> Optional[tuple[int, float]]:
        """Return (user_count, spawn_rate) or None when test completes."""
        profile_name = RUN_CONFIG.get("profile", "smoke")
        try:
            profile = get_profile(profile_name)
        except Exception:
            return None

        run_time = self.get_run_time()
        if run_time > profile.duration_s:
            return None

        if profile.name == "spike":
            current_qps = profile.get_target_qps_at(run_time)
            target_users = int(max(10, current_qps))
            return (target_users, 10.0)

        return (profile.users, profile.spawn_rate)


if LOCUST_AVAILABLE and events:

    @events.init_command_line_parser.add_listener
    def _add_locust_cli_args(parser: Any) -> None:
        parser.add_argument(
            "--profile",
            choices=["smoke", "sustained", "spike"],
            default="smoke",
            help="Load test profile: smoke, sustained, or spike",
        )
        parser.add_argument(
            "--i-understand-cost",
            action="store_true",
            default=False,
            help="Acknowledge quota and cost consumption for sustained and spike profiles",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Simulate execution without dialing Vertex AI",
        )

    @events.test_start.add_listener
    def _on_test_start(environment: Any, **kwargs: Any) -> None:
        args = environment.parsed_options
        profile_name = getattr(args, "profile", "smoke")
        cost_ack = getattr(args, "i_understand_cost", False) or os.environ.get("I_UNDERSTAND_COST") == "1"
        dry_run = getattr(args, "dry_run", False) or os.environ.get("LOAD_DRY_RUN") == "1"

        RUN_CONFIG["profile"] = profile_name
        RUN_CONFIG["i_understand_cost"] = cost_ack
        RUN_CONFIG["dry_run"] = dry_run
        RUN_CONFIG["start_time"] = time.time()

        profile = get_profile(profile_name)
        try:
            assert_cost_guard(profile, i_understand_cost=cost_ack)
        except CostGuardError as exc:
            logger.error("%s", exc)
            environment.runner.quit()
            raise

    @events.test_stop.add_listener
    def _on_test_stop(environment: Any, **kwargs: Any) -> None:
        profile_name = RUN_CONFIG.get("profile", "smoke")
        duration_s = max(0.1, time.time() - RUN_CONFIG.get("start_time", time.time()))

        # Determine output dir
        out_root = RESULTS_ROOT
        out_root.mkdir(parents=True, exist_ok=True)
        import re

        pattern = re.compile(r"^run_(\d{3})(?:_.*)?$")
        max_num = 0
        for entry in out_root.iterdir():
            if entry.is_dir():
                m = pattern.match(entry.name)
                if m:
                    max_num = max(max_num, int(m.group(1)))
        run_dir = out_root / f"run_{max_num + 1:03d}_{profile_name}"
        run_dir.mkdir(parents=True, exist_ok=False)

        # Write requests.csv
        csv_file = run_dir / "requests.csv"
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            fieldnames = [
                "timestamp_iso",
                "case_id",
                "category",
                "session_id",
                "latency_ms",
                "success",
                "decision",
                "tier",
                "queue",
                "error",
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in RUN_RECORDS:
                writer.writerow({k: r.get(k, "") for k in fieldnames})

        # Write summary.json
        summary = compute_load_summary(RUN_RECORDS, duration_s=duration_s, profile_name=profile_name)
        summary["dry_run"] = RUN_CONFIG.get("dry_run", False)
        json_file = run_dir / "summary.json"
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        logger.info("Saved load test artifacts to %s", run_dir)
