"""Live benchmark execution engine for Vertex Reasoning Engine / Agent Engine.

# REQ-EVAL-011: Live Per-Path Latency Benchmark with Trace-Level Step Attribution
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

DEFAULT_PROJECT_NUMBER = "620485621180"
DEFAULT_REGION = "us-central1"
DEFAULT_ENGINE_ID = "2170218524805431296"
DEFAULT_MAX_REQUESTS = 200
ESTIMATED_COST_PER_TURN = 0.003


def plan_requests(
    cases: Sequence[dict[str, Any]],
    reps: int = 5,
    paths: Sequence[str] | str | None = None,
    case_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Generate an ordered plan of requests to execute.

    Args:
        cases: Raw test cases (from cases_v1.jsonl).
        reps: Number of repetitions per case.
        paths: Optional filter on case path(s).
        case_filter: Optional substring filter on case_id or message.

    Returns:
        List of planned request dictionaries with `maybe_cold` set.
    """
    if paths is not None:
        if isinstance(paths, str):
            allowed_paths = {p.strip().lower() for p in paths.split(",") if p.strip()}
        else:
            allowed_paths = {p.strip().lower() for p in paths}
    else:
        allowed_paths = None

    selected_cases: list[dict[str, Any]] = []
    for c in cases:
        c_path = str(c.get("path", "")).strip().lower()
        if allowed_paths is not None and c_path not in allowed_paths:
            continue
        c_id = str(c.get("case_id", ""))
        c_msg = str(c.get("message", ""))
        if case_filter:
            flt = case_filter.lower()
            if flt not in c_id.lower() and flt not in c_msg.lower():
                continue
        selected_cases.append(c)

    plan: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for rep in range(1, reps + 1):
        for c in selected_cases:
            p = str(c.get("path", "")).strip().lower()
            is_cold = p not in seen_paths
            seen_paths.add(p)
            plan.append(
                {
                    "case_id": c.get("case_id"),
                    "path": c.get("path"),
                    "message": c.get("message"),
                    "oauth": bool(c.get("oauth", False)),
                    "expected_decision": c.get("expected_decision", "auto_rag"),
                    "rep": rep,
                    "maybe_cold": is_cold,
                }
            )

    return plan


def build_session_payload(user_id: str) -> dict[str, Any]:
    """Build pure JSON payload for session creation."""
    return {
        "class_method": "async_create_session",
        "input": {
            "user_id": user_id,
        },
    }


def build_stream_payload(
    message: str,
    user_id: str,
    session_id: str,
    oauth: bool,
    access_token: str | None = None,
) -> dict[str, Any]:
    """Build pure JSON payload for streamQuery with serialized request_json."""
    inner: dict[str, Any] = {
        "message": {
            "role": "user",
            "parts": [{"text": message}],
        },
        "user_id": user_id,
        "session_id": session_id,
    }
    if oauth and access_token:
        inner["authorizations"] = {
            "support-customer-oauth": {
                "access_token": access_token,
            }
        }

    return {
        "class_method": "streaming_agent_run_with_events",
        "input": {
            "request_json": json.dumps(inner),
        },
    }


def parse_stream_lines(
    lines: Iterable[str | bytes],
) -> tuple[str, str | int | None, str | None, str | None]:
    """Parse streamed lines from Agent Engine streamQuery.

    Returns:
        (answer_text, error_code, error_message, session_id)
    """
    answer_parts: list[str] = []
    error_code: str | int | None = None
    error_message: str | None = None
    session_id: str | None = None

    for raw in lines:
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace").strip()
        else:
            text = str(raw).strip()

        if not text:
            continue

        try:
            line_obj = json.loads(text)
        except Exception:
            continue

        if not isinstance(line_obj, dict):
            continue

        if line_obj.get("session_id"):
            session_id = str(line_obj["session_id"])

        # Check for error event (even if HTTP status was 200)
        if "code" in line_obj or "errorCode" in line_obj:
            code_val = line_obj.get("code")
            if code_val is None:
                code_val = line_obj.get("errorCode")
            error_code = code_val
            msg = line_obj.get("message") or line_obj.get("errorMessage") or ""
            error_message = str(msg)[:200]

        # Extract text from events
        events = line_obj.get("events")
        if isinstance(events, list):
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                content = ev.get("content")
                if isinstance(content, dict):
                    parts = content.get("parts")
                    if isinstance(parts, list):
                        for p in parts:
                            if isinstance(p, dict) and "text" in p:
                                answer_parts.append(str(p["text"]))

    full_answer = "".join(answer_parts)
    return full_answer, error_code, error_message, session_id


def get_default_access_token() -> str:
    """Fetch live access token using ticket_router.common.auth (ADC)."""
    from ticket_router.common.auth import get_access_token

    return get_access_token()


class CachedTokenProvider:
    """Caches access token and refreshes upon TTL expiry."""

    def __init__(
        self,
        ttl_seconds: float = 600.0,
        fetcher: Callable[[], str] = get_default_access_token,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.fetcher = fetcher
        self._cached_token: str | None = None
        self._token_time: float = 0.0

    def get_token(self) -> str:
        now = time.monotonic()
        if self._cached_token is None or (now - self._token_time) >= self.ttl_seconds:
            self._cached_token = self.fetcher()
            self._token_time = now
        return self._cached_token

    def __call__(self) -> str:
        """Allow the provider to be used directly as a token callable."""
        return self.get_token()


def run_bench(
    plan: list[dict[str, Any]],
    engine_id: str = DEFAULT_ENGINE_ID,
    project_number: str = DEFAULT_PROJECT_NUMBER,
    region: str = DEFAULT_REGION,
    token_provider: Callable[[], str] | None = None,
    http: Any | None = None,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
    spacing_seconds: float = 1.0,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    dry_run: bool = False,
    request_timeout: float = 60.0,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Execute live or simulated latency benchmark against Reasoning Engine.

    Args:
        plan: List of planned requests from `plan_requests`.
        engine_id: Vertex AI Reasoning Engine resource ID.
        project_number: GCP project numeric ID.
        region: GCP location/region.
        token_provider: Callable returning bearer token.
        http: HTTP client (e.g. requests.Session() or fake).
        clock: Timing clock function.
        sleep: Sleep function.
        spacing_seconds: Delay between requests.
        max_requests: Safety cap on planned requests.
        dry_run: When True, prints cost estimate and returns without calling API.
        request_timeout: Seconds before request timeout.
        progress_callback: Optional per-request record callback.

    Returns:
        List of per-request telemetry dictionaries.
    """
    if len(plan) > max_requests:
        raise ValueError(
            f"Planned request count {len(plan)} exceeds maximum allowed safety cap "
            f"of {max_requests}. Aborting to prevent unexpected cost."
        )

    estimated_cost = len(plan) * ESTIMATED_COST_PER_TURN
    if dry_run:
        print(f"[DRY-RUN] Planned requests: {len(plan)}")
        print(f"[DRY-RUN] Estimated cost: ~${estimated_cost:.4f} (@ ${ESTIMATED_COST_PER_TURN}/turn)")
        print("[DRY-RUN] Zero network calls made.")
        return []

    if http is None:
        import requests

        http = requests.Session()

    if token_provider is None:
        token_provider = CachedTokenProvider()

    base_url = (
        f"https://{region}-aiplatform.googleapis.com/v1"
        f"/projects/{project_number}/locations/{region}/reasoningEngines/{engine_id}"
    )

    records: list[dict[str, Any]] = []

    for item in plan:
        user_id = f"latbench-{secrets.token_hex(4)}"
        session_id: str | None = None
        http_status: int | None = None
        error_code: str | int | None = None
        error_message: str | None = None
        ttfb_ms: float | None = None
        client_total_ms: float | None = None
        answer_text = ""

        try:
            token = token_provider()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }

            # 1. Create fresh session
            session_payload = build_session_payload(user_id)
            session_resp = http.post(
                f"{base_url}:query",
                headers=headers,
                json=session_payload,
                timeout=request_timeout,
            )
            http_status = session_resp.status_code
            if http_status == 200:
                s_json = session_resp.json()
                out = s_json.get("output") or {}
                session_id = str(out.get("id") or out.get("session_id") or "")
            else:
                error_code = f"session_http_{http_status}"
                error_message = (session_resp.text or "")[:200]

            # 2. Stream query
            if session_id:
                stream_payload = build_stream_payload(
                    message=item["message"],
                    user_id=user_id,
                    session_id=session_id,
                    oauth=item["oauth"],
                    access_token=token if item["oauth"] else None,
                )

                t_start = clock()
                stream_resp = http.post(
                    f"{base_url}:streamQuery",
                    headers=headers,
                    json=stream_payload,
                    timeout=request_timeout,
                    stream=True,
                )
                http_status = stream_resp.status_code

                lines_collected: list[str | bytes] = []
                for line in stream_resp.iter_lines():
                    has_content = line.strip() if isinstance(line, (str, bytes)) else bool(line)
                    if has_content:
                        if ttfb_ms is None:
                            ttfb_ms = (clock() - t_start) * 1000.0
                        lines_collected.append(line)

                t_end = clock()
                client_total_ms = (t_end - t_start) * 1000.0
                if ttfb_ms is None:
                    ttfb_ms = client_total_ms

                ans, err_c, err_m, sid_ret = parse_stream_lines(lines_collected)
                answer_text = ans
                if err_c is not None:
                    error_code = err_c
                    error_message = err_m
                elif http_status != 200:
                    error_code = f"http_{http_status}"
                    error_message = stream_resp.text[:200]
                if sid_ret and not session_id:
                    session_id = sid_ret

        except Exception as exc:
            if error_code is None:
                error_code = "client_exception"
                error_message = str(exc)[:200]
            if client_total_ms is None:
                client_total_ms = 0.0

        decision_len = len(answer_text)
        preview = answer_text[:160] if answer_text else ""
        sha256_hash = hashlib.sha256(answer_text.encode("utf-8")).hexdigest() if answer_text else ""

        rec: dict[str, Any] = {
            "case_id": item["case_id"],
            "path": item["path"],
            "rep": item.get("rep", 1),
            "message": item["message"],
            "oauth": item["oauth"],
            "expected_decision": item["expected_decision"],
            "maybe_cold": item["maybe_cold"],
            "user_id": user_id,
            "session_id": session_id,
            "ttfb_ms": round(ttfb_ms, 2) if ttfb_ms is not None else None,
            "client_total_ms": round(client_total_ms, 2) if client_total_ms is not None else None,
            "http_status": http_status,
            "error_code": error_code,
            "error_message": error_message,
            "answer_preview": preview,
            "answer_sha256": sha256_hash,
            "decision_length": decision_len,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        records.append(rec)

        if progress_callback is not None:
            progress_callback(rec)

        if spacing_seconds > 0:
            sleep(spacing_seconds)

    return records
