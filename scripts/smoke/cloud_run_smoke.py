#!/usr/bin/env python3
"""Cloud Run MCP Services Live Smoke Test Suite.

Port of operator Cloud Run smoke test (/scratch/smoke_cloudrun.sh).
Implements REQ-CD-007: Post-deploy smoke gates for Cloud Run microservices:
1. /health 200 on all 3 services (ticket-db-mcp, order-lookup-mcp, human-queue)
2. Unauthenticated request rejected with 403 Forbidden
3. Idempotent upsert dedup on ticket-db-mcp
4. get_order own order returns 200
5. get_order other customer order rejected (404/denied)
6. get_order with customer_id in body rejected (400)
7. human-queue /queue listing returns 200

Security invariants:
- Uses google-auth ID tokens for run.invoker via google.oauth2.id_token.fetch_id_token (impersonation-free).
- Exits non-zero on any failed check.
- Never logs or prints sensitive authorization tokens.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cloud_run_smoke")

DEFAULT_TICKET_DB_URL = "https://ticket-db-mcp-34mwh7k34a-uc.a.run.app"
DEFAULT_ORDER_LOOKUP_URL = "https://order-lookup-mcp-34mwh7k34a-uc.a.run.app"
DEFAULT_HUMAN_QUEUE_URL = "https://human-queue-34mwh7k34a-uc.a.run.app"


@dataclass
class SmokeCheck:
    name: str
    passed: bool
    status_code: int
    details: str
    latency_ms: float


def get_authenticated_session(audience: str) -> requests.Session:
    """Create a requests session with Google OIDC ID token for the given audience."""
    from ticket_router.common.idtoken import get_id_token

    token = get_id_token(audience)
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
    )
    return session


def run_smoke_checks(
    ticket_db_url: str,
    order_lookup_url: str,
    human_queue_url: str,
    timeout_s: float = 30.0,
) -> list[SmokeCheck]:
    """Execute all Cloud Run MCP service smoke checks."""
    results: list[SmokeCheck] = []

    # 1. Health checks on all 3 services
    services = [
        ("ticket-db-mcp", ticket_db_url),
        ("order-lookup-mcp", order_lookup_url),
        ("human-queue", human_queue_url),
    ]

    for name, base_url in services:
        t0 = time.perf_counter()
        try:
            session = get_authenticated_session(base_url)
            resp = session.get(f"{base_url}/health", timeout=timeout_s)
            latency = (time.perf_counter() - t0) * 1000
            passed = resp.status_code == 200
            details = f"HTTP {resp.status_code}" if passed else f"Expected 200, got {resp.status_code}: {resp.text[:100]}"
            results.append(SmokeCheck(f"{name} /health (authenticated)", passed, resp.status_code, details, latency))
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000
            results.append(SmokeCheck(f"{name} /health (authenticated)", False, 0, str(exc), latency))

    # 2. Unauthenticated check must be 403 Forbidden
    t0 = time.perf_counter()
    try:
        unauth_resp = requests.get(f"{ticket_db_url}/health", timeout=timeout_s)
        latency = (time.perf_counter() - t0) * 1000
        passed = unauth_resp.status_code == 403
        details = f"HTTP {unauth_resp.status_code} (access denied as expected)" if passed else f"Expected 403, got {unauth_resp.status_code}"
        results.append(SmokeCheck("ticket-db-mcp unauthenticated /health", passed, unauth_resp.status_code, details, latency))
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        results.append(SmokeCheck("ticket-db-mcp unauthenticated /health", False, 0, str(exc), latency))

    # 3. Idempotent upsert dedup on ticket-db-mcp
    key = f"smoke:{int(time.time())}"
    body: dict[str, Any] = {
        "idempotency_key": key,
        "customer_id": "cust_1001",
        "channel": "web",
        "body_redacted": "where is my order [ORDER_ID]",
        "decision": {
            "route": "faq",
            "intent": "order_status",
            "escalate": False,
            "sentiment": "neutral",
            "urgency": 0,
            "priority": "human-standard",
        },
    }

    t0 = time.perf_counter()
    try:
        tdb_session = get_authenticated_session(ticket_db_url)
        resp1 = tdb_session.post(f"{ticket_db_url}/tools/upsert_ticket", json=body, timeout=timeout_s)
        resp2 = tdb_session.post(f"{ticket_db_url}/tools/upsert_ticket", json=body, timeout=timeout_s)
        latency = (time.perf_counter() - t0) * 1000

        data1 = resp1.json() if resp1.status_code == 200 else {}
        data2 = resp2.json() if resp2.status_code == 200 else {}
        ticket1 = data1.get("ticket_id") or data1.get("ticket", {}).get("ticket_id")
        ticket2 = data2.get("ticket_id") or data2.get("ticket", {}).get("ticket_id")

        passed = resp1.status_code == 200 and resp2.status_code == 200 and ticket1 is not None and ticket1 == ticket2
        details = (
            f"Idempotent upsert verified (ticket_id={ticket1})"
            if passed
            else f"Failed dedup: r1={resp1.status_code} ({ticket1}), r2={resp2.status_code} ({ticket2})"
        )
        results.append(SmokeCheck("ticket-db-mcp idempotent upsert dedup", passed, resp2.status_code, details, latency))
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        results.append(SmokeCheck("ticket-db-mcp idempotent upsert dedup", False, 0, str(exc), latency))

    # 4. get_order own order (cust_1001 ORD-10001)
    ol_session: Optional[requests.Session] = None
    t0 = time.perf_counter()
    try:
        ol_session = get_authenticated_session(order_lookup_url)
        headers_cust1 = {"X-Customer-ID": "cust_1001"}
        resp_own = ol_session.post(
            f"{order_lookup_url}/tools/get_order",
            headers=headers_cust1,
            json={"order_id": "ORD-10001"},
            timeout=timeout_s,
        )
        latency = (time.perf_counter() - t0) * 1000
        passed = resp_own.status_code == 200
        details = "HTTP 200 order retrieved" if passed else f"Expected 200, got {resp_own.status_code}: {resp_own.text[:100]}"
        results.append(SmokeCheck("order-lookup-mcp get_order own (cust_1001)", passed, resp_own.status_code, details, latency))
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        results.append(SmokeCheck("order-lookup-mcp get_order own (cust_1001)", False, 0, str(exc), latency))

    # 5. get_order other customer's order (cust_1002 requesting ORD-10001 -> expect 404)
    t0 = time.perf_counter()
    try:
        headers_cust2 = {"X-Customer-ID": "cust_1002"}
        resp_other = (ol_session or get_authenticated_session(order_lookup_url)).post(
            f"{order_lookup_url}/tools/get_order",
            headers=headers_cust2,
            json={"order_id": "ORD-10001"},
            timeout=timeout_s,
        )
        latency = (time.perf_counter() - t0) * 1000
        passed = resp_other.status_code == 404
        details = f"HTTP {resp_other.status_code} denied across customer boundary" if passed else f"Expected 404, got {resp_other.status_code}"
        results.append(SmokeCheck("order-lookup-mcp get_order other customer (cust_1002)", passed, resp_other.status_code, details, latency))
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        results.append(SmokeCheck("order-lookup-mcp get_order other customer (cust_1002)", False, 0, str(exc), latency))

    # 6. get_order body customer_id (must be rejected with HTTP 400)
    t0 = time.perf_counter()
    try:
        resp_body_cid = (ol_session or get_authenticated_session(order_lookup_url)).post(
            f"{order_lookup_url}/tools/get_order",
            headers=headers_cust1,
            json={"order_id": "ORD-10001", "customer_id": "cust_1002"},
            timeout=timeout_s,
        )
        latency = (time.perf_counter() - t0) * 1000
        passed = resp_body_cid.status_code == 400
        details = f"HTTP {resp_body_cid.status_code} rejected body customer_id" if passed else f"Expected 400, got {resp_body_cid.status_code}"
        results.append(SmokeCheck("order-lookup-mcp get_order body customer_id rejected", passed, resp_body_cid.status_code, details, latency))
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        results.append(SmokeCheck("order-lookup-mcp get_order body customer_id rejected", False, 0, str(exc), latency))

    # 7. human-queue /queue
    t0 = time.perf_counter()
    try:
        hq_session = get_authenticated_session(human_queue_url)
        resp_hq = hq_session.get(f"{human_queue_url}/queue", timeout=timeout_s)
        latency = (time.perf_counter() - t0) * 1000
        passed = resp_hq.status_code == 200
        details = "HTTP 200 queue listing retrieved" if passed else f"Expected 200, got {resp_hq.status_code}: {resp_hq.text[:100]}"
        results.append(SmokeCheck("human-queue /queue listing", passed, resp_hq.status_code, details, latency))
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        results.append(SmokeCheck("human-queue /queue listing", False, 0, str(exc), latency))

    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live smoke verification of Cloud Run MCP services")
    parser.add_argument(
        "--ticket-db-url",
        default=os.environ.get("TICKET_DB_MCP_URL", DEFAULT_TICKET_DB_URL),
        help=f"Ticket DB MCP URL (default: {DEFAULT_TICKET_DB_URL})",
    )
    parser.add_argument(
        "--order-lookup-url",
        default=os.environ.get("ORDER_LOOKUP_MCP_URL", DEFAULT_ORDER_LOOKUP_URL),
        help=f"Order Lookup MCP URL (default: {DEFAULT_ORDER_LOOKUP_URL})",
    )
    parser.add_argument(
        "--human-queue-url",
        default=os.environ.get("HUMAN_QUEUE_URL", DEFAULT_HUMAN_QUEUE_URL),
        help=f"Human Queue URL (default: {DEFAULT_HUMAN_QUEUE_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Request timeout in seconds (default: 30.0)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger.info("Executing Cloud Run MCP smoke tests...")
    logger.info("Ticket DB URL:    %s", args.ticket_db_url)
    logger.info("Order Lookup URL: %s", args.order_lookup_url)
    logger.info("Human Queue URL:  %s", args.human_queue_url)

    results = run_smoke_checks(
        ticket_db_url=args.ticket_db_url,
        order_lookup_url=args.order_lookup_url,
        human_queue_url=args.human_queue_url,
        timeout_s=args.timeout,
    )

    all_passed = True
    print("\n" + "=" * 78)
    print(f"{'Check':<46} | {'Status':<6} | {'Latency':<9} | Details")
    print("-" * 78)
    for c in results:
        status = "PASS" if c.passed else "FAIL"
        print(f"{c.name:<46} | {status:<6} | {c.latency_ms:>7.1f}ms | {c.details}")
        if not c.passed:
            all_passed = False
    print("=" * 78 + "\n")

    if not all_passed:
        logger.error("Cloud Run smoke checks FAILED.")
        sys.exit(1)

    logger.info("All Cloud Run smoke checks PASSED.")


if __name__ == "__main__":
    main()
