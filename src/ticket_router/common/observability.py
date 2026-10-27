"""Observability: Distributed Tracing and PII-Free Structured Logging.

# REQ-AGENT-014: Observability: Distributed Tracing & PII-Free Structured Logging
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

# Disallowed field names that could carry raw message text, customer PII, or credentials
FORBIDDEN_PII_KEYS = frozenset({
    "text",
    "body",
    "message",
    "raw_text",
    "raw_message",
    "raw_query",
    "query",
    "customer_message",
    "email",
    "phone",
    "phone_number",
    "customer_name",
    "first_name",
    "last_name",
    "full_name",
    "ssn",
    "credit_card",
    "card_number",
    "cvv",
    "address",
    "street",
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "unmasked",
    "redaction_map",
})

# PII regex detectors to guard against accidental leakage inside string values
_EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_PHONE_REGEX = re.compile(r"\b(?:\+?1[-.]?)?\(?[2-9]\d{2}\)?[-.]?\d{3}[-.]?\d{4}\b")
_CARD_REGEX = re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b")

# Check if OpenTelemetry is installed
try:
    from opentelemetry import trace  # type: ignore[import-untyped]
    _OTEL_AVAILABLE = True
    _TRACER = trace.get_tracer("ticket_router")
except ImportError:
    _OTEL_AVAILABLE = False
    _TRACER = None


def is_otel_available() -> bool:
    """Return True if opentelemetry is installed and accessible."""
    return _OTEL_AVAILABLE


# -----------------------------------------------------------------------------
# W3C Trace Context Helpers (REQ-AGENT-014)
# -----------------------------------------------------------------------------


def generate_traceparent(version: str = "00", flags: str = "01") -> str:
    """Generate a standard W3C traceparent header string."""
    trace_id = secrets.token_hex(16)  # 32 hex chars
    span_id = secrets.token_hex(8)    # 16 hex chars
    return f"{version}-{trace_id}-{span_id}-{flags}"


def parse_traceparent(header: str) -> Optional[Dict[str, str]]:
    """Parse a W3C traceparent string into component fields."""
    if not header or not isinstance(header, str):
        return None
    parts = header.strip().split("-")
    if len(parts) != 4:
        return None
    version, trace_id, span_id, flags = parts
    if len(version) != 2 or len(trace_id) != 32 or len(span_id) != 16 or len(flags) != 2:
        return None
    return {
        "version": version,
        "trace_id": trace_id,
        "span_id": span_id,
        "flags": flags,
    }


# -----------------------------------------------------------------------------
# OpenTelemetry Span Helper (no-ops gracefully if opentelemetry is absent)
# -----------------------------------------------------------------------------


class NoOpSpan:
    """Fallback span when opentelemetry is not installed."""

    def __init__(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        self.name = name
        self.attributes = attributes or {}
        self.ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_exception(self, exc: BaseException) -> None:
        pass

    def end(self) -> None:
        self.ended = True


@contextmanager
def start_span(
    name: str,
    attributes: Optional[Dict[str, Any]] = None,
    traceparent: Optional[str] = None,
) -> Iterator[Any]:
    """OTel span helper that no-ops if opentelemetry is absent (REQ-AGENT-014)."""
    sanitized_attrs = sanitize_log_dict(attributes) if attributes else {}

    if _OTEL_AVAILABLE and _TRACER is not None:
        with _TRACER.start_as_current_span(name, attributes=sanitized_attrs) as span:
            yield span
    else:
        span = NoOpSpan(name, attributes=sanitized_attrs)
        yield span
        span.end()


# -----------------------------------------------------------------------------
# PII Stripping and Structured JSON Logging (REQ-AGENT-014)
# -----------------------------------------------------------------------------


def sanitize_log_dict(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Recursively strip forbidden PII keys and scrub recognizable PII patterns."""
    if not data or not isinstance(data, dict):
        return {}

    sanitized: Dict[str, Any] = {}
    for key, val in data.items():
        key_lower = str(key).lower()
        if key_lower in FORBIDDEN_PII_KEYS:
            continue  # Strip forbidden field entirely

        if isinstance(val, dict):
            sanitized[key] = sanitize_log_dict(val)
        elif isinstance(val, list):
            sanitized[key] = [
                sanitize_log_dict(item) if isinstance(item, dict) else _sanitize_scalar(item)
                for item in val
            ]
        else:
            sanitized[key] = _sanitize_scalar(val)

    return sanitized


def _sanitize_scalar(val: Any) -> Any:
    """Scrub raw PII patterns from scalar string values."""
    if isinstance(val, str):
        val = _EMAIL_REGEX.sub("[REDACTED_EMAIL]", val)
        val = _PHONE_REGEX.sub("[REDACTED_PHONE]", val)
        val = _CARD_REGEX.sub("[REDACTED_CARD]", val)
    return val


def is_pii_clean(data: Dict[str, Any]) -> bool:
    """Check whether a dictionary contains any forbidden PII keys or patterns."""
    for key, val in data.items():
        key_lower = str(key).lower()
        if key_lower in FORBIDDEN_PII_KEYS:
            return False
        if isinstance(val, dict):
            if not is_pii_clean(val):
                return False
        elif isinstance(val, str) and (_EMAIL_REGEX.search(val) or _CARD_REGEX.search(val)):
            return False
    return True


class StructuredJsonLogger:
    """Emits structured JSON logs strictly free of PII and message text.

    REQ-AGENT-014:
    Emits JSON containing:
    ticket_id, session_id, decision, tier, queue, degraded, latency_ms.
    Customer message bodies, raw queries, unmasked PII, and credentials
    MUST NEVER be written to logs.
    """

    def __init__(self, name: str = "ticket_router") -> None:
        self.name = name
        self.logger = logging.getLogger(name)

    def log_decision(
        self,
        ticket_id: str,
        session_id: str,
        decision: str,
        tier: str,
        queue: str,
        degraded: bool = False,
        latency_ms: float = 0.0,
        traceparent: Optional[str] = None,
        level: int = logging.INFO,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Emit a structured routing decision log entry in JSON format."""
        sanitized_extra = sanitize_log_dict(extra)

        record: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "logger": self.name,
            "ticket_id": ticket_id,
            "session_id": session_id,
            "decision": decision,
            "tier": tier,
            "queue": queue,
            "degraded": bool(degraded),
            "latency_ms": round(float(latency_ms), 2),
        }

        if traceparent:
            record["traceparent"] = traceparent

        record.update(sanitized_extra)

        # Assert no forbidden keys leaked into record
        assert is_pii_clean(record), "PII hygiene violation: log entry contains forbidden fields"

        json_line = json.dumps(record, separators=(",", ":"))
        self.logger.log(level, json_line)
        return record
