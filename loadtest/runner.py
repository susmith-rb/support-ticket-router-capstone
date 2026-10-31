"""Standalone CLI runner for synthetic load tests against Vertex AI Agent Engine.

# REQ-LOAD-001: Locust Load Testing Profiles and Cost/Quota Safeguards
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loadtest.dataset import LoadTestCase, LoadTestDataset
from loadtest.guard import CostGuardError, assert_cost_guard
from loadtest.profiles import LoadProfile, get_profile
from loadtest.stats import compute_load_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("loadtest.runner")

DEFAULT_RESOURCE_NAME = (
    "projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296"
)
DEFAULT_PROJECT = "susmith-fde-ticket-router"
DEFAULT_REGION = "us-central1"
RESULTS_ROOT = REPO_ROOT / "evals" / "results" / "load"


def get_next_load_run_dir(out_root: Path, profile_name: str) -> Path:
    """Create next monotonic run directory under evals/results/load/run_XXX_<profile>."""
    out_root.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(r"^run_(\d{3})(?:_.*)?$")
    max_num = 0
    for entry in out_root.iterdir():
        if entry.is_dir():
            m = pattern.match(entry.name)
            if m:
                max_num = max(max_num, int(m.group(1)))
    next_num = max_num + 1
    run_dir = out_root / f"run_{next_num:03d}_{profile_name}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


class LoadTestExecutor:
    """Coordinates concurrent request generation, timing, and metric persistence."""

    def __init__(
        self,
        profile: LoadProfile,
        resource_name: str = DEFAULT_RESOURCE_NAME,
        project: str = DEFAULT_PROJECT,
        region: str = DEFAULT_REGION,
        dry_run: bool = False,
        max_requests: Optional[int] = None,
        duration_s: Optional[int] = None,
        concurrency: Optional[int] = None,
        out_dir: Optional[Path] = None,
    ):
        self.profile = profile
        self.resource_name = resource_name
        self.project = project
        self.region = region
        self.dry_run = dry_run
        self.max_requests = max_requests
        self.duration_s = duration_s or profile.duration_s
        self.concurrency = concurrency or max(1, profile.users)
        self.out_dir = out_dir or get_next_load_run_dir(RESULTS_ROOT, profile.name)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.dataset = LoadTestDataset(REPO_ROOT)
        self.records: List[Dict[str, Any]] = []
        self._records_lock = threading.Lock()
        self._tokens_cache: Dict[str, str] = {}
        self._cached_engine: Any = None
        self._engine_lock = threading.Lock()

    def _get_demo_token(self, demo_user: Optional[str]) -> Optional[str]:
        if not demo_user:
            return None
        if demo_user in self._tokens_cache:
            return self._tokens_cache[demo_user]
        if self.dry_run:
            token = f"simulated-jwt-{demo_user}"
            self._tokens_cache[demo_user] = token
            return token

        # Call scripts/deploy_agents/query_triage_agent.py resolve_demo_user_token
        try:
            from scripts.deploy_agents.query_triage_agent import resolve_demo_user_token

            tok = resolve_demo_user_token(demo_user)
            if tok:
                self._tokens_cache[demo_user] = tok
                return tok
        except Exception as exc:
            logger.warning("Could not resolve demo token for user %s: %s", demo_user, exc)
        return None

    def _get_agent_engine(self) -> Any:
        if self.dry_run:
            return None
        with self._engine_lock:
            if self._cached_engine is None:
                import vertexai
                from vertexai import agent_engines

                vertexai.init(project=self.project, location=self.region)
                self._cached_engine = agent_engines.get(self.resource_name)
            return self._cached_engine

    def execute_single_request(self, case: LoadTestCase, request_idx: int) -> Dict[str, Any]:
        """Execute a single turn and record latency, decision, tier, and status."""
        t0 = time.perf_counter()
        ts_iso = datetime.now(timezone.utc).isoformat()
        session_id = f"load-{self.profile.name}-{request_idx:05d}-{int(t0)}"
        token = self._get_demo_token(case.demo_user) if case.oauth else None

        if self.dry_run:
            # Simulate realistic processing latency: Tier 1 (40-120ms) or Tier 2 / RAG (600-1800ms)
            is_fast_tier = case.category in ("greeting", "order_auth", "order_anon")
            simulated_lat_s = random.uniform(0.04, 0.12) if is_fast_tier else random.uniform(0.60, 1.80)
            time.sleep(simulated_lat_s)
            lat_ms = round((time.perf_counter() - t0) * 1000.0, 2)

            sim_decision = case.expected_decision or ("escalate" if case.category == "escalate_angry" else "auto_rag")
            sim_tier = (
                "tier0_rules"
                if case.category == "greeting"
                else ("tier1_router_s1" if is_fast_tier else "tier2_flash")
            )
            sim_queue = "human-urgent" if sim_decision == "escalate" else "automated"

            record = {
                "timestamp_iso": ts_iso,
                "request_idx": request_idx,
                "case_id": case.case_id,
                "category": case.category,
                "session_id": session_id,
                "latency_ms": lat_ms,
                "success": True,
                "decision": sim_decision,
                "tier": sim_tier,
                "queue": sim_queue,
                "error": "",
            }
            return record

        # Live Vertex AI Agent Engine invocation
        try:
            engine = self._get_agent_engine()
            initial_state: Dict[str, Any] = {}
            if token:
                initial_state["customer_id_token"] = token

            user_id = f"caller-{case.demo_user or 'anon'}"
            session = engine.create_session(
                user_id=user_id,
                session_id=session_id,
                state=initial_state if initial_state else None,
            )
            active_session_id = getattr(session, "id", None) or session_id

            from scripts.deploy_agents.query_triage_agent import extract_ticket_fields_from_event

            ticket_fields: Dict[str, Any] = {}
            for event in engine.stream_query(
                message=case.message,
                user_id=user_id,
                session_id=active_session_id,
            ):
                ticket_fields.update(extract_ticket_fields_from_event(event))

            lat_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            return {
                "timestamp_iso": ts_iso,
                "request_idx": request_idx,
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
        except Exception as exc:
            lat_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            logger.error("Request %d failed (%s): %s", request_idx, case.case_id, exc)
            return {
                "timestamp_iso": ts_iso,
                "request_idx": request_idx,
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

    def run(self) -> Dict[str, Any]:
        """Execute the load test according to profile shape and write output artifacts."""
        logger.info("Starting load test run: profile=%s, dry_run=%s", self.profile.name, self.dry_run)
        logger.info("Destination directory: %s", self.out_dir)

        start_time = time.time()
        end_time = start_time + self.duration_s
        req_counter = 0

        # Worker pool for concurrent query execution
        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            futures = []

            while time.time() < end_time:
                if self.max_requests is not None and req_counter >= self.max_requests:
                    logger.info("Reached maximum request limit (%d). Concluding dispatch.", self.max_requests)
                    break

                elapsed_s = time.time() - start_time
                target_qps = self.profile.get_target_qps_at(elapsed_s)

                case = self.dataset.sample()
                req_counter += 1
                fut = executor.submit(self.execute_single_request, case, req_counter)
                futures.append(fut)

                # Pace requests according to current target QPS
                sleep_s = 1.0 / max(target_qps, 0.1)
                time.sleep(sleep_s)

            # Wait for all in-flight requests to complete
            for fut in futures:
                try:
                    rec = fut.result()
                    self.records.append(rec)
                except Exception as exc:
                    logger.error("Future failed: %s", exc)

        total_elapsed = time.time() - start_time
        logger.info(
            "Load test execution complete: %d requests collected in %.2fs",
            len(self.records),
            total_elapsed,
        )

        # Write CSV output
        csv_file = self.out_dir / "requests.csv"
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            fieldnames = [
                "timestamp_iso",
                "request_idx",
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
            for r in self.records:
                writer.writerow({k: r.get(k, "") for k in fieldnames})
        logger.info("Wrote request trace to: %s", csv_file)

        # Compute summary and write JSON output
        summary = compute_load_summary(self.records, duration_s=total_elapsed, profile_name=self.profile.name)
        summary["resource_name"] = self.resource_name
        summary["project"] = self.project
        summary["region"] = self.region
        summary["dry_run"] = self.dry_run
        summary["concurrency"] = self.concurrency

        json_file = self.out_dir / "summary.json"
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        logger.info("Wrote summary JSON to: %s", json_file)

        return summary


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Locust / Synthetic Load Test Runner for TriageRouter")
    parser.add_argument(
        "--profile",
        choices=["smoke", "sustained", "spike"],
        default="smoke",
        help="Load test profile: smoke (1 msg/s, 2m), sustained (33 msg/s, 20m), or spike (ramp to ~100 msg/s, 5m)",
    )
    parser.add_argument(
        "--resource-name",
        default=DEFAULT_RESOURCE_NAME,
        help="Vertex AI Agent Engine resource name",
    )
    parser.add_argument(
        "--project",
        default=DEFAULT_PROJECT,
        help="Google Cloud project ID",
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help="Google Cloud region",
    )
    parser.add_argument(
        "--i-understand-cost",
        action="store_true",
        default=False,
        help="Explicit user acknowledgment required to run expensive profiles (sustained, spike)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate execution without dialing Vertex AI Agent Engine",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Explicit output directory (defaults to evals/results/load/run_XXX_<profile>)",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="Optional ceiling on total requests to issue",
    )
    parser.add_argument(
        "--duration-s",
        type=int,
        default=None,
        help="Optional override for total run duration in seconds",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Number of concurrent worker threads",
    )
    return parser.parse_args(args)


def main() -> None:
    args = parse_args()
    profile = get_profile(args.profile)

    # Cost / Quota guard check
    try:
        assert_cost_guard(profile, i_understand_cost=args.i_understand_cost)
    except CostGuardError as exc:
        logger.error(str(exc))
        sys.exit(2)

    out_dir_path = Path(args.out_dir) if args.out_dir else None
    executor = LoadTestExecutor(
        profile=profile,
        resource_name=args.resource_name,
        project=args.project,
        region=args.region,
        dry_run=args.dry_run,
        max_requests=args.max_requests,
        duration_s=args.duration_s,
        concurrency=args.concurrency,
        out_dir=out_dir_path,
    )

    summary = executor.run()
    lat = summary.get("latency_ms", {})
    print("\n" + "=" * 60)
    print(f"LOAD TEST SUMMARY: {profile.name.upper()} (dry_run={args.dry_run})")
    print(f"Total Requests:      {summary.get('total_requests')}")
    print(f"Success / Failed:    {summary.get('successful_requests')} / {summary.get('failed_requests')}")
    print(f"Error Rate:          {summary.get('error_rate_pct')}%")
    print(f"Actual QPS:          {summary.get('actual_qps')} msg/s")
    print(f"Latency p50:         {lat.get('p50')} ms")
    print(f"Latency p90:         {lat.get('p90')} ms")
    print(f"Latency p95:         {lat.get('p95')} ms")
    print(f"Latency p99:         {lat.get('p99')} ms")
    print(f"Decisions:           {json.dumps(summary.get('decisions', {}))}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
