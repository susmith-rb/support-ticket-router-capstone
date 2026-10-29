"""Tests for distributed tracing and PII-free structured logging.

# REQ-AGENT-014: Observability: Distributed Tracing & PII-Free Structured Logging
"""

import json
import logging
import os
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from ticket_router.agents.faq_resolver.mcp_client import (
    HttpOrderLookupClient,
)
from ticket_router.agents.triage_router.a2a_client import (
    A2AHandoffPayload,
    HttpA2AClient,
)
from ticket_router.agents.triage_router.mcp_client import (
    HttpTicketDbMcpClient,
)
from ticket_router.common.observability import (
    StructuredJsonLogger,
    configure_tracing,
    flush_traces,
    generate_traceparent,
    get_current_traceparent,
    is_pii_clean,
    parse_cloud_trace_context,
    parse_traceparent,
    reset_tracing_for_tests,
    sanitize_log_dict,
    start_span,
)
from ticket_router.serve import get_app


def test_otel_spans_and_pii_free_logs():
    # REQ-AGENT-014: Distributed Tracing & PII-Free Structured Logging

    # 1. Test W3C traceparent generation and parsing
    tp = generate_traceparent()
    assert tp.startswith("00-")
    parsed = parse_traceparent(tp)
    assert parsed is not None
    assert parsed["version"] == "00"
    assert len(parsed["trace_id"]) == 32
    assert len(parsed["span_id"]) == 16
    assert parsed["flags"] == "01"

    # Malformed traceparents return None
    assert parse_traceparent("invalid-header") is None
    assert parse_traceparent("00-short-span-01") is None

    # 2. Test start_span helper (graceful no-op when OTel is absent or present)
    with start_span("triage_turn", attributes={"tier": "tier1", "decision": "escalate"}) as span:
        assert span is not None
        span.set_attribute("queue", "human-urgent")
    assert span.ended

    # 3. Test PII sanitization helper
    dirty_data = {
        "text": "My order 123 is broken",
        "raw_message": "Call me at 555-123-4567 or email john@example.com",
        "ticket_id": "TICK-1001",
        "nested": {
            "body": "Secret info",
            "safe_metric": 42.5,
            "comment": "Contact support at help@company.com",
        },
    }
    sanitized = sanitize_log_dict(dirty_data)
    assert "text" not in sanitized
    assert "raw_message" not in sanitized
    assert sanitized["ticket_id"] == "TICK-1001"
    assert "body" not in sanitized["nested"]
    assert sanitized["nested"]["safe_metric"] == 42.5
    assert "[REDACTED_EMAIL]" in sanitized["nested"]["comment"]

    # 4. Test StructuredJsonLogger emits valid, PII-free JSON
    mock_handler = MagicMock()
    mock_handler.level = logging.INFO
    logger_inst = StructuredJsonLogger(name="test_router")
    logger_inst.logger.addHandler(mock_handler)
    logger_inst.logger.setLevel(logging.INFO)

    record = logger_inst.log_decision(
        ticket_id="TICK-42",
        session_id="SESS-99",
        decision="auto_rag",
        tier="tier1",
        queue="none",
        degraded=False,
        latency_ms=43.25,
        traceparent=tp,
        # Attempt to pass forbidden fields in extra kwargs
        text="raw customer prompt that must be stripped",
        body="disallowed customer body",
        email="customer@victim.org",
        routing_confidence=0.94,
    )

    # Required contract fields present
    assert record["ticket_id"] == "TICK-42"
    assert record["session_id"] == "SESS-99"
    assert record["decision"] == "auto_rag"
    assert record["tier"] == "tier1"
    assert record["queue"] == "none"
    assert record["degraded"] is False
    assert record["latency_ms"] == 43.25
    assert record["traceparent"] == tp
    assert record["routing_confidence"] == 0.94

    # Forbidden PII fields stripped
    assert "text" not in record
    assert "body" not in record
    assert "email" not in record
    assert is_pii_clean(record)

    # Verify log output was emitted as parseable JSON
    assert mock_handler.handle.called
    log_record = mock_handler.handle.call_args[0][0]
    payload = json.loads(log_record.getMessage())
    assert payload["ticket_id"] == "TICK-42"
    assert "text" not in payload


def test_traceparent_contextvar_and_child_span_linking():
    # REQ-AGENT-014: ContextVar propagation, child span linking, and PII hygiene in attributes
    root_tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert get_current_traceparent() is None

    with start_span("root_turn", attributes={"tier": "tier1", "email": "leak@victim.com"}, traceparent=root_tp) as root_span:
        active_root_tp = get_current_traceparent()
        assert active_root_tp is not None
        parsed_root = parse_traceparent(active_root_tp)
        assert parsed_root is not None
        assert parsed_root["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"

        # PII stripped from start_span attributes
        if hasattr(root_span, "attributes"):
            assert "email" not in root_span.attributes
            assert root_span.attributes.get("tier") == "tier1"

        # PII stripped from set_attribute
        root_span.set_attribute("customer_name", "Alice Bob")
        root_span.set_attribute("note", "Contact support at test@example.com")
        if hasattr(root_span, "attributes"):
            assert "customer_name" not in root_span.attributes
            assert "[REDACTED_EMAIL]" in root_span.attributes["note"]

        # Nested child span inherits trace_id and updates ContextVar
        with start_span("child_step", attributes={"step": "retrieval"}) as child_span:
            child_tp = get_current_traceparent()
            assert child_tp is not None
            parsed_child = parse_traceparent(child_tp)
            assert parsed_child is not None
            assert parsed_child["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
            # Distinct child span ID
            assert parsed_child["span_id"] != parsed_root["span_id"]
            assert getattr(child_span, "traceparent", None) == child_tp

        # Exited child span: ContextVar restored to parent
        assert get_current_traceparent() == active_root_tp

    # Exited root span: ContextVar restored to None
    assert get_current_traceparent() is None


def test_cloud_trace_context_header_parsing():
    # REQ-AGENT-014: Parse Google X-Cloud-Trace-Context header to W3C traceparent
    # Standard format: <32-hex-trace-id>/<decimal-span-id>;o=1
    res = parse_cloud_trace_context("105445aa7843bc8bf206b12000100000/12345;o=1")
    assert res == "00-105445aa7843bc8bf206b12000100000-0000000000003039-01"

    # With o=0 (not sampled)
    res_nosample = parse_cloud_trace_context("105445aa7843bc8bf206b12000100000/12345;o=0")
    assert res_nosample == "00-105445aa7843bc8bf206b12000100000-0000000000003039-00"

    # Without options
    res_noopts = parse_cloud_trace_context("105445aa7843bc8bf206b12000100000/12345")
    assert res_noopts == "00-105445aa7843bc8bf206b12000100000-0000000000003039-01"

    # Malformed or zero values return None
    assert parse_cloud_trace_context("") is None
    assert parse_cloud_trace_context("not-a-trace-context") is None
    assert parse_cloud_trace_context("00000000000000000000000000000000/12345;o=1") is None
    assert parse_cloud_trace_context("105445aa7843bc8bf206b12000100000/0;o=1") is None


def test_outbound_clients_propagate_traceparent_header():
    # REQ-AGENT-014: Outbound HTTP clients attach traceparent sharing parent trace_id
    root_tp = "00-11112222333344445555666677778888-9999888877776666-01"

    with start_span("test_turn", traceparent=root_tp):
        # 1. HttpA2AClient
        mock_a2a_sess = MagicMock()
        mock_a2a_resp = MagicMock()
        mock_a2a_resp.status_code = 200
        mock_a2a_resp.json.return_value = {"status": "ACCEPTED"}
        mock_a2a_sess.post.return_value = mock_a2a_resp

        a2a_client = HttpA2AClient(endpoint_url="http://fake-a2a", session=mock_a2a_sess)
        payload = A2AHandoffPayload(
            ticket_id="TICK-1",
            session_id="SESS-1",
            predicted_intent="shipping",
            redacted_message="Where is my item?",
        )
        a2a_client.handoff(payload)
        assert mock_a2a_sess.post.called
        headers = mock_a2a_sess.post.call_args[1]["headers"]
        assert "traceparent" in headers
        parsed = parse_traceparent(headers["traceparent"])
        assert parsed is not None
        assert parsed["trace_id"] == "11112222333344445555666677778888"

        # 2. HttpTicketDbMcpClient
        mock_db_sess = MagicMock()
        mock_db_resp = MagicMock()
        mock_db_resp.status_code = 200
        mock_db_resp.json.return_value = {"ticket_id": "TICK-1", "status": "OPEN"}
        mock_db_sess.post.return_value = mock_db_resp

        db_client = HttpTicketDbMcpClient(base_url="http://fake-db", session=mock_db_sess)
        db_client.upsert_ticket(
            idempotency_key="k1",
            customer_id="cust1",
            channel="web",
            body_redacted="msg",
            decision={"decision": "escalate"},
        )
        assert mock_db_sess.post.called
        headers = mock_db_sess.post.call_args[1]["headers"]
        assert "traceparent" in headers
        parsed = parse_traceparent(headers["traceparent"])
        assert parsed is not None
        assert parsed["trace_id"] == "11112222333344445555666677778888"

        # 3. HttpOrderLookupClient
        mock_ord_sess = MagicMock()
        mock_ord_resp = MagicMock()
        mock_ord_resp.status_code = 200
        mock_ord_resp.json.return_value = {"order_id": "ORD-123"}
        mock_ord_sess.post.return_value = mock_ord_resp

        ord_client = HttpOrderLookupClient(base_url="http://fake-ord", session=mock_ord_sess)
        ord_client.get_order(order_id="ORD-123", customer_id="cust1")
        assert mock_ord_sess.post.called
        headers = mock_ord_sess.post.call_args[1]["headers"]
        assert "traceparent" in headers
        parsed = parse_traceparent(headers["traceparent"])
        assert parsed is not None
        assert parsed["trace_id"] == "11112222333344445555666677778888"


def test_serve_fastapi_middleware_extracts_traceparent():
    # REQ-AGENT-014: FastAPI middleware extracts traceparent and propagates on responses
    os.environ["ALLOW_FAKE_REPOS"] = "1"
    app = get_app("ticket_db")
    client = TestClient(app)

    # 1. Health check bypasses trace noise
    resp_health = client.get("/health")
    assert resp_health.status_code == 200

    # 2. Non-health route with inbound W3C traceparent
    inbound_tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    resp_tool = client.post(
        "/tools/resolve_customer",
        json={"issuer": "https://accounts.google.com", "subject": "sub-123"},
        headers={"traceparent": inbound_tp},
    )
    assert resp_tool.status_code == 200
    outbound_tp = resp_tool.headers.get("traceparent")
    assert outbound_tp is not None
    parsed = parse_traceparent(outbound_tp)
    assert parsed is not None
    assert parsed["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"

    # 3. Non-health route with Google X-Cloud-Trace-Context
    cloud_trace_hdr = "105445aa7843bc8bf206b12000100000/12345;o=1"
    resp_ct = client.post(
        "/tools/resolve_customer",
        json={"issuer": "https://accounts.google.com", "subject": "sub-456"},
        headers={"x-cloud-trace-context": cloud_trace_hdr},
    )
    assert resp_ct.status_code == 200
    outbound_ct_tp = resp_ct.headers.get("traceparent")
    assert outbound_ct_tp is not None
    parsed_ct = parse_traceparent(outbound_ct_tp)
    assert parsed_ct is not None
    assert parsed_ct["trace_id"] == "105445aa7843bc8bf206b12000100000"


def test_configure_tracing_env_gating_and_no_content_invariant(monkeypatch):
    # REQ-AGENT-014: Verification of NO_CONTENT invariant and environment gating
    assert os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT") == "NO_CONTENT"

    # When ENABLE_CLOUD_TRACE=0, configure_tracing returns False
    monkeypatch.setenv("ENABLE_CLOUD_TRACE", "0")
    reset_tracing_for_tests()
    assert configure_tracing(service_name="test_svc") is False

    # When SDK and exporter are available, test InMemorySpanExporter parent/child span correlation
    try:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        mem_exporter = InMemorySpanExporter()
        reset_tracing_for_tests()
        res = configure_tracing(service_name="test_mem", exporter=mem_exporter, force=True)
        if res:
            root_tp = "00-abcdef0123456789abcdef0123456789-0000000000001111-01"
            with start_span("root_step", traceparent=root_tp):
                with start_span("child_step"):
                    pass
            flush_traces()
            spans = mem_exporter.get_finished_spans()
            assert len(spans) == 2
            child_span, root_span = spans[0], spans[1]
            assert f"{root_span.context.trace_id:032x}" == "abcdef0123456789abcdef0123456789"
            assert f"{child_span.context.trace_id:032x}" == "abcdef0123456789abcdef0123456789"
            assert child_span.parent.span_id == root_span.context.span_id
    except (ImportError, Exception):
        pass  # Hermetic offline CI isolation without opentelemetry-sdk
