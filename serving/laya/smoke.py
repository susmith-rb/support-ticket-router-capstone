"""serving/laya/smoke.py — Smoke test and latency benchmark client for router-s1 and reranker.

Validates the deployed Vertex AI Prediction endpoint against the SLA budget:
1. Sends N route requests and N rerank requests.
2. Validates output schemas (typed decisions, confidence probabilities, candidate rankings).
3. Computes mean, p50, and p95 latency.
4. Asserts that route p95 latency meets the 150 ms SLA budget (TDD §2 / §3.6).

Can run against either:
- A live Vertex AI Endpoint (:rawPredict) via gcloud auth / google-auth
- A local or HTTP mock URL (e.g. http://localhost:8080/predict)
"""

import argparse
import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("smoke")

# Sample test inquiries
SAMPLE_ROUTE_TEXTS = [
    "Where is my package? It was supposed to arrive yesterday.",
    "I was charged twice on my credit card for order 12345. Refund this immediately!",
    "Can you explain your international shipping rates and delivery times for Canada?",
    "My item arrived broken and defective. I would like an exchange or return.",
    "How do I reset my account password?",
]

SAMPLE_RERANK_QUERY = "Where is my refund for the returned order?"
SAMPLE_RERANK_CANDIDATES = [
    {"id": "faq_refund_status", "text": "Refund Status and Tracking: Details on refund processing times and tracking bank deposits."},
    {"id": "faq_return_policy", "text": "Return Policy Overview: Items can be returned within 30 days of receipt."},
    {"id": "faq_shipping_times", "text": "Delivery Times and Speeds: Standard and expedited shipping options."},
    {"id": "faq_account_login", "text": "Account Login Issues: Resetting credentials and security verification."},
    {"id": "faq_gift_card", "text": "Gift Card Balance Inquiry: How to redeem and verify gift card funds."},
    {"id": "faq_international", "text": "International Shipping Rates: Customs duties and international transit times."},
]


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for smoke test."""
    parser = argparse.ArgumentParser(description="Smoke test for Vertex AI router and reranker endpoint.")
    parser.add_argument("--endpoint-id", help="Vertex AI Endpoint ID or resource name.")
    parser.add_argument("--project", default="susmith-fde-ticket-router", help="GCP Project ID.")
    parser.add_argument("--region", default="us-central1", help="Vertex AI region.")
    parser.add_argument("--url", help="Direct HTTP URL for prediction (e.g. http://localhost:8080/predict).")
    parser.add_argument("--n-requests", type=int, default=10, help="Number of route and rerank requests to send.")
    parser.add_argument("--sla-budget-ms", type=float, default=150.0, help="Maximum allowed route p95 latency in ms.")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP request timeout in seconds.")
    return parser.parse_args()


def get_gcloud_auth_token() -> str:
    """Retrieve Google Cloud OAuth access token via gcloud CLI or google.auth ADC fallback."""
    cmd = ["gcloud", "auth", "print-access-token"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
        err_msg = res.stderr
    except FileNotFoundError as exc:
        err_msg = str(exc)

    try:
        import google.auth
        from google.auth.transport.requests import Request

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not creds.valid:
            creds.refresh(Request())
        if creds.token:
            return str(creds.token)
    except Exception:
        pass
    raise RuntimeError(f"Failed to obtain gcloud access token: {err_msg}")


def send_predict_request(
    url: str,
    payload: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> Tuple[Dict[str, Any], float]:
    """Send HTTP POST request to endpoint and measure client round-trip latency in ms."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            body = resp.read().decode("utf-8")
            return json.loads(body), elapsed_ms
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        raise RuntimeError(f"HTTP {e.code} error from {url}: {err_body}") from e


def calculate_percentiles(latencies: List[float]) -> Dict[str, float]:
    """Compute mean, min, max, p50, and p95 latency in ms."""
    if not latencies:
        return {"mean": 0.0, "min": 0.0, "max": 0.0, "p50": 0.0, "p95": 0.0}
    s = sorted(latencies)
    n = len(s)
    p50_idx = min(int(n * 0.50), n - 1)
    p95_idx = min(int(n * 0.95), n - 1)
    return {
        "mean": round(sum(s) / n, 2),
        "min": round(s[0], 2),
        "max": round(s[-1], 2),
        "p50": round(s[p50_idx], 2),
        "p95": round(s[p95_idx], 2),
    }


def validate_route_schema(prediction: Dict[str, Any]) -> None:
    """Validate that route prediction adheres to router decision taxonomy."""
    required_keys = ("escalate", "sentiment", "urgency", "intent", "model_version")
    for k in required_keys:
        if k not in prediction:
            raise ValueError(f"Route prediction missing required key: {k}")

    esc = prediction["escalate"]
    if "label" not in esc or "prob" not in esc:
        raise ValueError("Invalid escalate schema in prediction")

    sent = prediction["sentiment"]
    if "label" not in sent or "prob" not in sent:
        raise ValueError("Invalid sentiment schema in prediction")

    urg = prediction["urgency"]
    if "level" not in urg or "label" not in urg or "prob" not in urg:
        raise ValueError("Invalid urgency schema in prediction")

    intent = prediction["intent"]
    if "label" not in intent or "prob" not in intent:
        raise ValueError("Invalid intent schema in prediction")


def validate_rerank_schema(prediction: List[Dict[str, Any]]) -> None:
    """Validate that rerank prediction returns a sorted list of candidate scores."""
    if not isinstance(prediction, list) or len(prediction) == 0:
        raise ValueError("Rerank prediction must be a non-empty list")
    prev_score = float("inf")
    for item in prediction:
        if "id" not in item or "score" not in item:
            raise ValueError(f"Rerank candidate missing 'id' or 'score': {item}")
        score = float(item["score"])
        # Validate scores are sorted descending
        if score > prev_score + 1e-6:
            raise ValueError(f"Rerank candidates are not sorted descending: {prediction}")
        prev_score = score


def run_smoke_test(args: argparse.Namespace) -> Dict[str, Any]:
    """Execute complete smoke and latency verification suite."""
    if args.url:
        predict_url = args.url
        token = None
    elif args.endpoint_id:
        predict_url = (
            f"https://{args.region}-aiplatform.googleapis.com/v1/projects/"
            f"{args.project}/locations/{args.region}/endpoints/{args.endpoint_id}:rawPredict"
        )
        token = get_gcloud_auth_token()
    else:
        raise ValueError("Must provide either --url or --endpoint-id")

    logger.info("Running smoke test against URL: %s", predict_url)

    # 1. Warm-up
    logger.info("Warming up endpoint with single route request...")
    send_predict_request(
        predict_url,
        {"instances": [{"task": "route", "text": "warmup message"}]},
        token=token,
        timeout=args.timeout,
    )

    # 2. Benchmark Route requests
    route_latencies: List[float] = []
    route_server_latencies: List[float] = []
    logger.info("Sending %d Route requests...", args.n_requests)
    for i in range(args.n_requests):
        text = SAMPLE_ROUTE_TEXTS[i % len(SAMPLE_ROUTE_TEXTS)]
        resp, lat_ms = send_predict_request(
            predict_url,
            {"instances": [{"task": "route", "text": text}]},
            token=token,
            timeout=args.timeout,
        )
        route_latencies.append(lat_ms)
        preds = resp.get("predictions", [])
        if not preds:
            raise ValueError(f"Empty predictions returned for route request {i}")
        validate_route_schema(preds[0])
        if isinstance(preds[0], dict) and "latency_ms" in preds[0]:
            route_server_latencies.append(float(preds[0]["latency_ms"]))

    route_stats = calculate_percentiles(route_latencies)
    logger.info("Route Latencies (ms): mean=%.1f, p50=%.1f, p95=%.1f, max=%.1f",
                route_stats["mean"], route_stats["p50"], route_stats["p95"], route_stats["max"])
    if route_server_latencies:
        route_srv_stats = calculate_percentiles(route_server_latencies)
        logger.info("Route Server Latencies (ms): mean=%.1f, p50=%.1f, p95=%.1f, max=%.1f",
                    route_srv_stats["mean"], route_srv_stats["p50"], route_srv_stats["p95"], route_srv_stats["max"])

    # 3. Benchmark Rerank requests
    rerank_latencies: List[float] = []
    rerank_server_latencies: List[float] = []
    logger.info("Sending %d Rerank requests...", args.n_requests)
    for i in range(args.n_requests):
        resp, lat_ms = send_predict_request(
            predict_url,
            {"instances": [{"task": "rerank", "query": SAMPLE_RERANK_QUERY, "candidates": SAMPLE_RERANK_CANDIDATES}]},
            token=token,
            timeout=args.timeout,
        )
        rerank_latencies.append(lat_ms)
        preds = resp.get("predictions", [])
        if not preds:
            raise ValueError(f"Empty predictions returned for rerank request {i}")
        validate_rerank_schema(preds[0])
        if isinstance(preds[0], list) and preds[0] and isinstance(preds[0][0], dict) and "latency_ms" in preds[0][0]:
            rerank_server_latencies.append(float(preds[0][0]["latency_ms"]))
        elif isinstance(preds[0], dict) and "latency_ms" in preds[0]:
            rerank_server_latencies.append(float(preds[0]["latency_ms"]))

    rerank_stats = calculate_percentiles(rerank_latencies)
    logger.info("Rerank Latencies (ms): mean=%.1f, p50=%.1f, p95=%.1f, max=%.1f",
                rerank_stats["mean"], rerank_stats["p50"], rerank_stats["p95"], rerank_stats["max"])
    if rerank_server_latencies:
        rerank_srv_stats = calculate_percentiles(rerank_server_latencies)
        logger.info("Rerank Server Latencies (ms): mean=%.1f, p50=%.1f, p95=%.1f, max=%.1f",
                    rerank_srv_stats["mean"], rerank_srv_stats["p50"], rerank_srv_stats["p95"], rerank_srv_stats["max"])

    # 4. Check SLA budget
    sla_passed = route_stats["p95"] <= args.sla_budget_ms
    logger.info("SLA Check: Route p95 (%.1f ms) <= Budget (%.1f ms) -> %s",
                route_stats["p95"], args.sla_budget_ms, "PASSED" if sla_passed else "FAILED")

    if not sla_passed:
        logger.warning("Route p95 latency exceeded SLA budget of %.1f ms", args.sla_budget_ms)

    return {
        "route_stats": route_stats,
        "rerank_stats": rerank_stats,
        "sla_passed": sla_passed,
    }


def main():
    args = parse_args()
    try:
        res = run_smoke_test(args)
        if not res["sla_passed"]:
            sys.exit(1)
        sys.exit(0)
    except Exception as e:
        logger.error("Smoke test failed with error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
