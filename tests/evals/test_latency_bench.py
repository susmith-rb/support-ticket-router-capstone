"""Tests for live per-path latency benchmark with trace-level step attribution.

# REQ-EVAL-011: Live Per-Path Latency Benchmark with Trace-Level Step Attribution
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.latency import cases_path
from evals.latency.bench import (
    CachedTokenProvider,
    build_session_payload,
    build_stream_payload,
    parse_stream_lines,
    plan_requests,
    run_bench,
)
from evals.latency.correlate import (
    aggregate_spans,
    correlate_records,
    parse_decision_log,
)
from evals.latency.report import (
    compute_summary,
    get_next_run_dir,
    percentile,
    write_report_artifacts,
)


class FakeResponse:
    """Mock HTTP response for requests-compatible interface."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        lines: list[str | bytes] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self._lines = lines or []
        self.text = text

    def json(self) -> Any:
        return self._json_data

    def iter_lines(self) -> list[str | bytes]:
        return self._lines


class FakeHttpClient:
    """Mock HTTP client handling session creation, streaming, logging, and trace calls."""

    def __init__(self, fail_trace_with_429_count: int = 1) -> None:
        self.fail_trace_with_429_count = fail_trace_with_429_count
        self.trace_call_count = 0
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        json: Any = None,
        timeout: float = 30.0,
        stream: bool = False,
    ) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})

        # 1. Reasoning Engine Session Create
        if ":query" in url:
            uid = json.get("input", {}).get("user_id", "test-user")
            return FakeResponse(
                status_code=200,
                json_data={
                    "output": {
                        "id": "4551031906157723648",
                        "user_id": uid,
                    }
                },
            )

        # 2. Reasoning Engine StreamQuery
        if ":streamQuery" in url:
            req_str = json.get("input", {}).get("request_json", "{}")
            inner = json_loads_safe(req_str)
            msg_text = inner.get("message", {}).get("parts", [{}])[0].get("text", "")

            # Simulate error 498 on a specific prompt
            if "simulated-error" in msg_text:
                error_line = (
                    '{"code": 498, "message": "HTTPSConnectionPool(host=\'ticket-db-mcp\'): '
                    'Read timed out. (read timeout=0.5)", "errorCode": "498", "errorMessage": "Read timed out"}'
                )
                return FakeResponse(status_code=200, lines=[error_line])

            # Normal success event
            event_line = (
                '{"events": [{"content": {"parts": [{"text": "Hello! How may I assist you today?"}], '
                '"role": "model"}, "invocation_id": "inv-001", "author": "triage_router"}], '
                '"session_id": "4551031906157723648"}'
            )
            return FakeResponse(status_code=200, lines=[event_line])

        # 3. Cloud Logging entries:list
        if "logging.googleapis.com" in url:
            # Return structured log with JSON embedded in textPayload, plus irrelevant entries
            entries = [
                {
                    "textPayload": (
                        '[17]      INFO:     {"timestamp":"2026-10-29T16:38:02.000711+00:00",'
                        '"logger":"triage_router","ticket_id":"TICK-45510319",'
                        '"session_id":"4551031906157723648","decision":"auto_rag",'
                        '"tier":"tier1_router_s1","queue":"none","degraded":false,'
                        '"latency_ms":6008.91,"traceparent":"00-9e1675a71a6f3485948750caafa9352a-20e87b4e686aa934-01",'
                        '"predicted_intent":"returns_refunds"}'
                    )
                },
                {
                    "textPayload": "Worker process initialized successfully.",
                },
                {
                    "jsonPayload": {
                        "logger": "other_service",
                        "session_id": "irrelevant-session",
                    }
                },
            ]
            return FakeResponse(status_code=200, json_data={"entries": entries})

        return FakeResponse(status_code=404)

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url, "headers": headers})

        # 4. Cloud Trace fetch
        if "cloudtrace.googleapis.com" in url:
            self.trace_call_count += 1
            if self.trace_call_count <= self.fail_trace_with_429_count:
                return FakeResponse(status_code=429, text="Rate limit exceeded")

            # Return duplicated spans to test deduplication
            spans = [
                {
                    "name": "triage_router_turn",
                    "startTime": "2026-10-29T16:38:02.000000Z",
                    "endTime": "2026-10-29T16:38:08.009000Z",
                },
                {
                    "name": "triage_router_turn",
                    "startTime": "2026-10-29T16:38:02.000000Z",
                    "endTime": "2026-10-29T16:38:08.009000Z",
                },
                {
                    "name": "mcp.ticket_db.resolve_customer",
                    "startTime": "2026-10-29T16:38:02.050000Z",
                    "endTime": "2026-10-29T16:38:02.150000Z",
                },
                {
                    "name": "mcp.ticket_db.upsert_ticket",
                    "startTime": "2026-10-29T16:38:02.202000Z",
                    "endTime": "2026-10-29T16:38:02.496000Z",
                },
                {
                    "name": "a2a.handoff.faq_resolver",
                    "startTime": "2026-10-29T16:38:02.500000Z",
                    "endTime": "2026-10-29T16:38:07.988000Z",
                },
                {
                    "name": "faq_resolver.resolve",
                    "startTime": "2026-10-29T16:38:02.510000Z",
                    "endTime": "2026-10-29T16:38:07.582000Z",
                },
                {
                    "name": "faq_resolver.search",
                    "startTime": "2026-10-29T16:38:02.600000Z",
                    "endTime": "2026-10-29T16:38:03.358000Z",
                },
                {
                    "name": "faq_resolver.rerank",
                    "startTime": "2026-10-29T16:38:03.360000Z",
                    "endTime": "2026-10-29T16:38:03.649000Z",
                },
                {
                    "name": "faq_resolver.generate_answer",
                    "startTime": "2026-10-29T16:38:03.650000Z",
                    "endTime": "2026-10-29T16:38:07.331000Z",
                },
                {
                    "name": "guardrail.model_armor.check",
                    "startTime": "2026-10-29T16:38:02.010000Z",
                    "endTime": "2026-10-29T16:38:02.035000Z",
                },
            ]
            return FakeResponse(status_code=200, json_data={"spans": spans})

        return FakeResponse(status_code=404)


def json_loads_safe(s: str) -> dict[str, Any]:
    try:
        return json.loads(s)
    except Exception:
        return {}


class FakeClock:
    """Mock clock with monotonic tick increments."""

    def __init__(self, start: float = 1000.0, step: float = 0.5) -> None:
        self.current_time = start
        self.step = step

    def tick(self) -> float:
        self.current_time += self.step
        return self.current_time


# REQ-EVAL-011: Main acceptance test
def test_latency_bench_attribution_and_report(tmp_path: Path):
    """End-to-end hermetic test of latency benchmark execution, correlation, and reporting.

    # REQ-EVAL-011
    """
    secret_token = "TEST_SECRET_GCLOUD_BEARER_TOKEN_XY123"
    fake_client = FakeHttpClient(fail_trace_with_429_count=1)

    cases = [
        {
            "case_id": "test_policy_01",
            "path": "policy_faq",
            "message": "What is your return policy?",
            "oauth": False,
            "expected_decision": "auto_rag",
        },
        {
            "case_id": "test_error_01",
            "path": "policy_faq",
            "message": "Please trigger simulated-error condition",
            "oauth": False,
            "expected_decision": "auto_rag",
        },
    ]

    plan = plan_requests(cases=cases, reps=1)
    fake_clock = FakeClock()

    # 1. Run bench
    raw_records = run_bench(
        plan=plan,
        engine_id="test-engine-id",
        project_number="123456",
        region="us-central1",
        token_provider=lambda: secret_token,
        http=fake_client,
        clock=fake_clock.tick,
        sleep=lambda _: None,
        spacing_seconds=0.0,
        max_requests=10,
        dry_run=False,
    )

    assert len(raw_records) == 2

    # Check first record (success)
    r_success = raw_records[0]
    assert r_success["case_id"] == "test_policy_01"
    assert r_success["session_id"] == "4551031906157723648"
    assert r_success["ttfb_ms"] is not None
    assert r_success["client_total_ms"] is not None
    assert r_success["error_code"] is None
    assert r_success["decision_length"] > 0
    assert len(r_success["answer_sha256"]) == 64

    # Check second record (error 498)
    r_err = raw_records[1]
    assert r_err["case_id"] == "test_error_01"
    assert r_err["error_code"] == 498 or r_err["error_code"] == "498"
    assert "Read timed out" in (r_err["error_message"] or "")
    assert len(r_err["error_message"]) <= 200

    # 2. Correlate with logs and traces
    correlated = correlate_records(
        records=raw_records,
        token=secret_token,
        http=fake_client,
        project_id="susmith-fde-ticket-router",
        settle_seconds=0.0,
        sleep=lambda _: None,
    )

    r_corr = correlated[0]
    assert r_corr["trace_id"] == "9e1675a71a6f3485948750caafa9352a"
    assert r_corr["tier"] == "tier1_router_s1"
    assert r_corr["decision"] == "auto_rag"
    assert r_corr["turn_ms"] == 6009.0
    assert r_corr["routing_ms"] == pytest.approx(102.0, abs=2.0)
    assert r_corr["resolve_customer_ms"] == 100.0
    assert r_corr["upsert_ticket_ms"] == pytest.approx(294.0, abs=2.0)
    assert r_corr["a2a_handoff_ms"] == pytest.approx(5488.0, abs=2.0)
    assert r_corr["faq_resolve_ms"] == pytest.approx(5072.0, abs=2.0)
    assert r_corr["search_ms"] == pytest.approx(758.0, abs=2.0)
    assert r_corr["rerank_ms"] == pytest.approx(289.0, abs=2.0)
    assert r_corr["generate_answer_ms"] == pytest.approx(3681.0, abs=2.0)
    assert r_corr["generate_answer_count"] == 1
    assert r_corr["model_armor_ms"] == 25.0
    assert r_corr["agent_engine_overhead_ms"] is not None

    # 3. Compute summary and write artifacts
    summary = compute_summary(correlated)
    assert summary["total_requests"] == 2
    assert summary["total_errors"] == 1
    assert "policy_faq" in summary["paths"]

    out_dir = get_next_run_dir(tmp_path, label="test_run")
    artifacts = write_report_artifacts(out_dir, correlated, summary)

    # 4. Verify artifact contents and zero token persistence
    assert artifacts["requests"].exists()
    assert artifacts["summary"].exists()
    assert artifacts["report"].exists()

    requests_txt = artifacts["requests"].read_text(encoding="utf-8")
    summary_txt = artifacts["summary"].read_text(encoding="utf-8")
    report_txt = artifacts["report"].read_text(encoding="utf-8")

    # Tokens MUST never be logged or written to disk
    assert secret_token not in requests_txt
    assert secret_token not in summary_txt
    assert secret_token not in report_txt

    # Verify report table header
    assert "| Path | N | Errors | p50 Total | p95 Total | p50 Turn | p50 Routing | p50 GenAnswer | Dominant Tier |" in report_txt
    assert "`policy_faq`" in report_txt


# REQ-EVAL-011: Test bundled cases dataset
def test_cases_v1_schema_and_distribution():
    """Verify that cases_v1.jsonl meets all dataset requirements.

    # REQ-EVAL-011
    """
    path = cases_path()
    assert path.exists(), f"Missing bundled cases file: {path}"

    cases = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                cases.append(json.loads(line))

    assert len(cases) >= 24, f"Expected at least 24 cases (~32), found {len(cases)}"

    expected_paths = {
        "greeting",
        "pleasantry",
        "policy_faq",
        "order_list_auth",
        "order_id_auth",
        "order_anon",
        "out_of_scope",
        "escalation",
    }
    by_path: dict[str, list[dict]] = {}
    case_ids: set[str] = set()

    for c in cases:
        cid = c.get("case_id")
        assert cid, f"Missing case_id in {c}"
        assert cid not in case_ids, f"Duplicate case_id {cid}"
        case_ids.add(cid)

        p = c.get("path")
        assert p in expected_paths, f"Invalid path '{p}' in case {cid}"
        by_path.setdefault(p, []).append(c)

        assert isinstance(c.get("oauth"), bool), f"oauth must be bool in {cid}"
        if p in ("order_list_auth", "order_id_auth"):
            assert c["oauth"] is True, f"Expected oauth=True for path {p} in {cid}"
        else:
            assert c["oauth"] is False, f"Expected oauth=False for path {p} in {cid}"

        dec = c.get("expected_decision")
        if p == "out_of_scope":
            assert dec == "reject_oos", f"Expected reject_oos for {p} in {cid}"
        elif p == "escalation":
            assert dec == "escalate", f"Expected escalate for {p} in {cid}"
        else:
            assert dec == "auto_rag", f"Expected auto_rag for {p} in {cid}"

    for p in expected_paths:
        assert len(by_path.get(p, [])) >= 3, f"Path '{p}' has fewer than 3 cases"


# REQ-EVAL-011: Test payload builders
def test_payload_builders():
    """Verify session and stream query JSON payload builders.

    # REQ-EVAL-011
    """
    s_payload = build_session_payload("latbench-aabb1122")
    assert s_payload["class_method"] == "async_create_session"
    assert s_payload["input"]["user_id"] == "latbench-aabb1122"

    # With oauth = False
    p_no_oauth = build_stream_payload(
        message="return policy?",
        user_id="user-1",
        session_id="sess-1",
        oauth=False,
    )
    inner_no_oauth = json.loads(p_no_oauth["input"]["request_json"])
    assert inner_no_oauth["message"]["parts"][0]["text"] == "return policy?"
    assert inner_no_oauth["user_id"] == "user-1"
    assert inner_no_oauth["session_id"] == "sess-1"
    assert "authorizations" not in inner_no_oauth

    # With oauth = True and access_token
    p_oauth = build_stream_payload(
        message="my orders?",
        user_id="user-2",
        session_id="sess-2",
        oauth=True,
        access_token="fake-oauth-access-token-for-tests",
    )
    inner_oauth = json.loads(p_oauth["input"]["request_json"])
    assert inner_oauth["authorizations"]["support-customer-oauth"]["access_token"] == "fake-oauth-access-token-for-tests"


# REQ-EVAL-011: Test stream line parsing
def test_parse_stream_lines_variants():
    """Test parsing of various event streams including errors and empty lines.

    # REQ-EVAL-011
    """
    lines = [
        b'{"events": [{"content": {"parts": [{"text": "First part. "}]}}]}',
        "",
        "   ",
        b'{"events": [{"content": {"parts": [{"text": "Second part."}]}}], "session_id": "sid-999"}',
    ]
    ans, code, msg, sid = parse_stream_lines(lines)
    assert ans == "First part. Second part."
    assert code is None
    assert msg is None
    assert sid == "sid-999"

    # Stream with error event
    err_lines = [
        '{"code": 498, "message": "Upstream timeout occurred", "errorCode": "498"}',
    ]
    ans_e, code_e, msg_e, sid_e = parse_stream_lines(err_lines)
    assert ans_e == ""
    assert code_e == 498 or code_e == "498"
    assert "Upstream timeout" in (msg_e or "")


# REQ-EVAL-011: Test log entry parsing
def test_parse_decision_log():
    """Test parsing structured decision logs from textPayload and jsonPayload.

    # REQ-EVAL-011
    """
    text_entry = {
        "textPayload": (
            '[17] INFO: {"timestamp":"2026-10-29T16:38:02Z","logger":"triage_router",'
            '"session_id":"s-100","traceparent":"00-trace123-span456-01",'
            '"decision":"auto_rag","tier":"tier1_router_s1","latency_ms":123.45}'
        )
    }
    p_text = parse_decision_log(text_entry)
    assert p_text is not None
    assert p_text["session_id"] == "s-100"
    assert p_text["trace_id"] == "trace123"
    assert p_text["decision"] == "auto_rag"
    assert p_text["server_latency_ms"] == 123.45

    json_entry = {
        "jsonPayload": {
            "logger": "triage_router",
            "session_id": "s-200",
            "traceparent": "00-traceABC-spanDEF-01",
            "decision": "escalate",
            "tier": "tier2_gemini_flash",
            "latency_ms": 456.78,
        }
    }
    p_json = parse_decision_log(json_entry)
    assert p_json is not None
    assert p_json["session_id"] == "s-200"
    assert p_json["trace_id"] == "traceABC"
    assert p_json["decision"] == "escalate"

    # Non-triage logger ignored
    other_entry = {"jsonPayload": {"logger": "ticket_db", "session_id": "s-300"}}
    assert parse_decision_log(other_entry) is None


# REQ-EVAL-011: Test span aggregation math
def test_aggregate_spans_dedupe_and_math():
    """Test span deduplication, duration derivation, and routing calculation.

    # REQ-EVAL-011
    """
    spans = [
        # Duplicated triage_router_turn
        {
            "name": "triage_router_turn",
            "startTime": "2026-10-29T12:00:00.000Z",
            "endTime": "2026-10-29T12:00:05.000Z",
        },
        {
            "name": "triage_router_turn",
            "startTime": "2026-10-29T12:00:00.000Z",
            "endTime": "2026-10-29T12:00:05.000Z",
        },
        # resolve_customer inside turn before upsert
        {
            "name": "mcp.ticket_db.resolve_customer",
            "startTime": "2026-10-29T12:00:00.100Z",
            "endTime": "2026-10-29T12:00:00.300Z",
        },
        # upsert_ticket
        {
            "name": "mcp.ticket_db.upsert_ticket",
            "startTime": "2026-10-29T12:00:00.500Z",
            "endTime": "2026-10-29T12:00:00.800Z",
        },
        # Multiple generate_answer spans
        {
            "name": "faq_resolver.generate_answer",
            "startTime": "2026-10-29T12:00:01.000Z",
            "endTime": "2026-10-29T12:00:02.500Z",
        },
        {
            "name": "faq_resolver.generate_answer",
            "startTime": "2026-10-29T12:00:02.600Z",
            "endTime": "2026-10-29T12:00:04.100Z",
        },
    ]

    res = aggregate_spans(spans)
    assert res["turn_ms"] == 5000.0
    assert res["resolve_customer_ms"] == 200.0
    assert res["upsert_ticket_ms"] == 300.0
    # routing_ms = upsert.start (500) - turn.start (0) - resolve_customer (200) = 300 ms
    assert res["routing_ms"] == 300.0
    # generate_answer_ms = 1500 + 1500 = 3000 ms, count = 2
    assert res["generate_answer_ms"] == 3000.0
    assert res["generate_answer_count"] == 2
    # Missing spans should be None
    assert res["search_ms"] is None


# REQ-EVAL-011: Test percentile calculation
def test_percentile_math():
    """Verify linear interpolation percentile calculation.

    # REQ-EVAL-011
    """
    assert percentile([], 50.0) is None
    assert percentile([10.0], 50.0) == 10.0

    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(vals, 0.0) == 10.0
    assert percentile(vals, 50.0) == 30.0
    assert percentile(vals, 100.0) == 50.0
    # Linear interpolation at p25: rank = 0.25 * 4 = 1.0 -> 20.0
    assert percentile(vals, 25.0) == 20.0


# REQ-EVAL-011: Test planning and dry-run
def test_plan_requests_and_dry_run():
    """Verify request planning, cold start attribution, and dry run behavior.

    # REQ-EVAL-011
    """
    cases = [
        {"case_id": "c1", "path": "greeting", "message": "hi", "oauth": False},
        {"case_id": "c2", "path": "greeting", "message": "hello", "oauth": False},
        {"case_id": "c3", "path": "policy_faq", "message": "return policy", "oauth": False},
    ]
    plan = plan_requests(cases, reps=2)
    assert len(plan) == 6

    # Only first occurrence of each path should have maybe_cold=True
    cold_flags = [(p["path"], p["rep"], p["maybe_cold"]) for p in plan]
    assert cold_flags[0] == ("greeting", 1, True)
    assert cold_flags[1] == ("greeting", 1, False)
    assert cold_flags[2] == ("policy_faq", 1, True)
    assert cold_flags[3] == ("greeting", 2, False)

    # Dry run should make zero network calls and return empty list
    fake_client = FakeHttpClient()
    res = run_bench(plan, dry_run=True, http=fake_client)
    assert res == []
    assert len(fake_client.calls) == 0


# REQ-EVAL-011: Test max requests safety cap
def test_max_requests_guard():
    """Verify max requests guard aborts before executing oversized plans.

    # REQ-EVAL-011
    """
    cases = [{"case_id": f"c_{i}", "path": "greeting", "message": "hi"} for i in range(10)]
    plan = plan_requests(cases, reps=1)
    with pytest.raises(ValueError, match="exceeds maximum allowed safety cap"):
        run_bench(plan, max_requests=5)


# REQ-EVAL-011: Test monotonic directory creation
def test_monotonic_run_dir_naming(tmp_path: Path):
    """Verify non-overwriting monotonic run directory creation.

    # REQ-EVAL-011
    """
    d1 = get_next_run_dir(tmp_path)
    assert d1.name == "run_001"
    d2 = get_next_run_dir(tmp_path, label="baseline")
    assert d2.name == "run_002_baseline"
    d3 = get_next_run_dir(tmp_path)
    assert d3.name == "run_003"


# REQ-EVAL-011
def test_cached_token_provider_is_callable_and_caches():
    """CachedTokenProvider must be usable as the default token callable in run_bench / CLI."""
    calls: list[int] = []

    def fetcher() -> str:
        calls.append(1)
        return f"tok-{len(calls)}"

    provider = CachedTokenProvider(ttl_seconds=3600.0, fetcher=fetcher)
    assert provider() == "tok-1"
    assert provider() == "tok-1"
    assert provider.get_token() == "tok-1"
    assert len(calls) == 1
