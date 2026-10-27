"""Tests for distributed tracing and PII-free structured logging.

# REQ-AGENT-014: Observability: Distributed Tracing & PII-Free Structured Logging
"""

import json
import logging
from unittest.mock import MagicMock

from ticket_router.common.observability import (
    StructuredJsonLogger,
    generate_traceparent,
    is_pii_clean,
    parse_traceparent,
    sanitize_log_dict,
    start_span,
)


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
