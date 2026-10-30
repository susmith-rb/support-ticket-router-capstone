"""Observability: Distributed Tracing and PII-Free Structured Logging.

# REQ-AGENT-014: Observability: Distributed Tracing & PII-Free Structured Logging
# REQ-AUTH-006: mTLS-Aware Google API Transports (certificate-bound agent identity tokens)
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

# Force ADK/GenAI auto-instrumentation to never capture raw prompt/response text
os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "NO_CONTENT")

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
    "customer_context_token",
})

# PII regex detectors to guard against accidental leakage inside string values
_EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_PHONE_REGEX = re.compile(r"\b(?:\+?1[-.]?)?\(?[2-9]\d{2}\)?[-.]?\d{3}[-.]?\d{4}\b")
_CARD_REGEX = re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b")
# W3C trace identifiers are random hex; an all-digit 16-char span id matches _CARD_REGEX
# and caused flaky PII false positives, so trace keys skip the value-pattern scan.
_TRACE_KEYS = frozenset({"traceparent", "trace_id", "span_id"})

# Context variable holding active W3C traceparent for thread/coroutine context propagation
_CURRENT_TRACEPARENT: ContextVar[Optional[str]] = ContextVar("ticket_router_traceparent", default=None)


def get_current_traceparent() -> Optional[str]:
    """Retrieve active W3C traceparent from current coroutine/thread context (REQ-AGENT-014)."""
    return _CURRENT_TRACEPARENT.get()


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
# W3C & Cloud Trace Context Helpers (REQ-AGENT-014)
# -----------------------------------------------------------------------------

_CLOUD_TRACE_REGEX = re.compile(r"^([0-9a-fA-F]{32})/([0-9]+)(?:;o=([0-9]+))?$")


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


def parse_cloud_trace_context(header: str) -> Optional[str]:
    """Parse Google X-Cloud-Trace-Context header into W3C traceparent (REQ-AGENT-014).

    Format: <32-hex-trace-id>/<decimal-uint64-span-id>[;o=<0|1>]
    Returns: 00-<trace_id>-<16-hex-span-id>-<01|00>
    Returns None if malformed, empty, or zero trace/span ID.
    """
    if not header or not isinstance(header, str):
        return None
    match = _CLOUD_TRACE_REGEX.match(header.strip())
    if not match:
        return None
    trace_id_str, span_id_str, options_str = match.groups()
    try:
        trace_id_int = int(trace_id_str, 16)
        span_id_int = int(span_id_str, 10)
    except ValueError:
        return None

    if trace_id_int == 0 or span_id_int == 0:
        return None
    if span_id_int >= (1 << 64):
        return None

    flags = "01"
    if options_str is not None:
        try:
            opts = int(options_str, 10)
            flags = "01" if (opts & 1) else "00"
        except ValueError:
            flags = "01"

    return f"00-{trace_id_str.lower()}-{span_id_int:016x}-{flags}"


def format_span_traceparent(span: Any, fallback: Optional[str] = None) -> Optional[str]:
    """Format span context into standard W3C traceparent or return fallback (REQ-AGENT-014)."""
    raw = getattr(span, "_raw_span", span)
    ctx = getattr(raw, "get_span_context", lambda: None)()
    if ctx is not None:
        trace_id = getattr(ctx, "trace_id", 0)
        span_id = getattr(ctx, "span_id", 0)
        is_remote = getattr(ctx, "is_remote", False)
        is_recording = getattr(raw, "is_recording", lambda: True)()
        if trace_id != 0 and span_id != 0 and is_recording and not is_remote:
            flags = int(getattr(ctx, "trace_flags", 1)) & 0xFF
            return f"00-{trace_id:032x}-{span_id:016x}-{flags:02x}"
    return fallback


# -----------------------------------------------------------------------------
# Cloud Trace Configuration and Lifecycle (REQ-AGENT-014)
# -----------------------------------------------------------------------------

_CLOUD_TRACE_CONFIGURED: bool = False


def reset_tracing_for_tests() -> None:
    """Reset tracing configuration state for isolated unit testing (REQ-AGENT-014)."""
    global _CLOUD_TRACE_CONFIGURED, _TRACER
    _CLOUD_TRACE_CONFIGURED = False
    if _OTEL_AVAILABLE:
        try:
            from opentelemetry import trace
            _TRACER = trace.get_tracer("ticket_router")
        except Exception:
            _TRACER = None


def _create_cloud_trace_exporter(
    project_id: Optional[str] = None,
    client_options: Optional[Any] = None,
) -> Any:
    """Build a CloudTraceSpanExporter backed by a GAPIC mTLS-aware TraceServiceClient (REQ-AUTH-006).

    CloudTraceSpanExporter's default _create_default_client() calls grpc_helpers.create_channel
    directly against 'cloudtrace.googleapis.com:443' with ssl_credentials=None, bypassing GAPIC
    mTLS negotiation and failing with 401 UNAUTHENTICATED under certificate-bound agent identity.
    Using TraceServiceClient with a transport callable lets GAPIC resolve 'cloudtrace.mtls.googleapis.com'
    and the workload SSL certificate when present (plain 'cloudtrace.googleapis.com' on SA runtimes)
    while preserving the exporter's gRPC channel _OPTIONS.
    """
    import opentelemetry.exporter.cloud_trace as cloud_trace_mod  # type: ignore[import-untyped]
    from google.cloud.trace_v2 import TraceServiceClient  # type: ignore[import-untyped]
    from google.cloud.trace_v2.services.trace_service.transports.grpc import (  # type: ignore[import-untyped]
        TraceServiceGrpcTransport,
    )

    exporter_cls = cloud_trace_mod.CloudTraceSpanExporter
    exporter_options = tuple(getattr(cloud_trace_mod, "_OPTIONS", ()) or ())

    def _channel_factory(host: str, **channel_kwargs: Any) -> Any:
        if exporter_options:
            channel_kwargs["options"] = list(exporter_options)
        return TraceServiceGrpcTransport.create_channel(host, **channel_kwargs)

    def _transport_factory(**transport_kwargs: Any) -> Any:
        return TraceServiceGrpcTransport(channel=_channel_factory, **transport_kwargs)

    trace_client = TraceServiceClient(
        transport=_transport_factory,
        client_options=client_options,
    )
    return exporter_cls(project_id=project_id, client=trace_client)


def configure_tracing(
    service_name: str = "ticket_router",
    project_id: Optional[str] = None,
    exporter: Optional[Any] = None,
    force: bool = False,
) -> bool:
    """Configure OpenTelemetry tracing with Cloud Trace export (REQ-AGENT-014, REQ-AUTH-006).

    Lazily imports opentelemetry-sdk and opentelemetry-exporter-gcp-trace.
    Hermetically no-ops when exporter is absent, offline, or disabled.
    """
    global _CLOUD_TRACE_CONFIGURED, _TRACER

    # Invariant: Never capture prompt/response text in telemetry
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "NO_CONTENT"

    if exporter is not None or force:
        enabled = True
    else:
        flag = os.environ.get("ENABLE_CLOUD_TRACE", "").strip().lower()
        if flag in ("0", "false", "no", "off"):
            return False
        if flag in ("1", "true", "yes", "on"):
            enabled = True
        else:
            req_real = os.environ.get("REQUIRE_REAL_DEPS", "").strip().lower() in ("1", "true")
            in_cloud_run = "K_SERVICE" in os.environ
            in_gke = "KUBERNETES_SERVICE_HOST" in os.environ
            enabled = req_real or in_cloud_run or in_gke

    if not enabled:
        return False

    if _CLOUD_TRACE_CONFIGURED and exporter is None and not force:
        return True

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        if exporter is None:
            import opentelemetry.exporter.cloud_trace  # noqa: F401  # type: ignore[import-untyped]
    except (ImportError, Exception) as exc:
        logging.getLogger("ticket_router.common.observability").warning(
            "Tracing dependencies unavailable: %s", exc
        )
        return False

    try:
        if exporter is not None:
            span_exporter = exporter
            processor: Any = SimpleSpanProcessor(span_exporter)
        else:
            eff_project = project_id or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID") or None
            span_exporter = _create_cloud_trace_exporter(project_id=eff_project)
            processor = BatchSpanProcessor(span_exporter)

        current_provider = trace.get_tracer_provider()
        if isinstance(current_provider, TracerProvider):
            active_proc = getattr(current_provider, "_active_span_processor", None)
            existing_procs = getattr(active_proc, "_span_processors", ()) if active_proc else ()
            already_registered = False
            if exporter is None:
                for p in existing_procs:
                    exp = getattr(p, "span_exporter", None)
                    if exp is not None and type(exp).__name__ == "CloudTraceSpanExporter":
                        already_registered = True
                        break
            if not already_registered or exporter is not None:
                current_provider.add_span_processor(processor)
        else:
            provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
            provider.add_span_processor(processor)
            trace.set_tracer_provider(provider)

        _TRACER = trace.get_tracer("ticket_router")
        _CLOUD_TRACE_CONFIGURED = True
        return True
    except Exception as exc:
        logging.getLogger("ticket_router.common.observability").warning(
            "Failed to configure Cloud Trace exporter: %s", exc
        )
        return False


def flush_traces(timeout_millis: int = 1500) -> bool:
    """Safely flush pending spans on the active TracerProvider (REQ-AGENT-014)."""
    if not _OTEL_AVAILABLE:
        return False
    try:
        from opentelemetry import trace
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            return bool(provider.force_flush(timeout_millis=timeout_millis))
        return True
    except Exception as exc:
        logging.getLogger("ticket_router.common.observability").warning("Flush traces failed: %s", exc)
        return False


# -----------------------------------------------------------------------------
# OpenTelemetry Span Helper (no-ops gracefully if opentelemetry is absent)
# -----------------------------------------------------------------------------


class NoOpSpan:
    """Fallback span when opentelemetry is not installed or unconfigured."""

    def __init__(
        self,
        name: str,
        attributes: Optional[Dict[str, Any]] = None,
        traceparent: Optional[str] = None,
    ) -> None:
        self.name = name
        self.attributes = attributes or {}
        self.traceparent = traceparent
        self.ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        clean = sanitize_log_dict({key: value})
        if key in clean:
            self.attributes[key] = clean[key]

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_exception(self, exc: BaseException) -> None:
        pass

    def end(self) -> None:
        self.ended = True


class SpanAdapter:
    """Adapter ensuring unified interface across OTel Span and NoOpSpan."""

    def __init__(self, raw_span: Any, traceparent: Optional[str] = None) -> None:
        self._raw_span = raw_span
        self.traceparent = traceparent
        self.ended = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw_span, name)

    def set_attribute(self, key: str, value: Any) -> None:
        clean = sanitize_log_dict({key: value})
        if key in clean and hasattr(self._raw_span, "set_attribute"):
            self._raw_span.set_attribute(key, clean[key])

    def end(self, *args: Any, **kwargs: Any) -> None:
        self.ended = True
        if hasattr(self._raw_span, "end"):
            self._raw_span.end(*args, **kwargs)


@contextmanager
def start_span(
    name: str,
    attributes: Optional[Dict[str, Any]] = None,
    traceparent: Optional[str] = None,
) -> Iterator[Any]:
    """OTel span helper with W3C traceparent propagation and PII hygiene (REQ-AGENT-014)."""
    sanitized_attrs = sanitize_log_dict(attributes) if attributes else {}
    effective_incoming_tp = traceparent or get_current_traceparent()

    if _OTEL_AVAILABLE and _TRACER is not None:
        parent_ctx = None
        if traceparent:
            parsed = parse_traceparent(traceparent)
            if parsed and int(parsed["trace_id"], 16) != 0 and int(parsed["span_id"], 16) != 0:
                try:
                    from opentelemetry.trace import (
                        NonRecordingSpan,
                        SpanContext,
                        TraceFlags,
                        set_span_in_context,
                    )
                    parent_ctx = set_span_in_context(
                        NonRecordingSpan(
                            SpanContext(
                                trace_id=int(parsed["trace_id"], 16),
                                span_id=int(parsed["span_id"], 16),
                                is_remote=True,
                                trace_flags=TraceFlags(int(parsed["flags"], 16)),
                            )
                        )
                    )
                except Exception:
                    parent_ctx = None
        elif effective_incoming_tp:
            curr_span = trace.get_current_span()
            ctx = getattr(curr_span, "get_span_context", lambda: None)()
            is_valid = getattr(ctx, "is_valid", False)
            if not is_valid:
                parsed = parse_traceparent(effective_incoming_tp)
                if parsed and int(parsed["trace_id"], 16) != 0 and int(parsed["span_id"], 16) != 0:
                    try:
                        from opentelemetry.trace import (
                            NonRecordingSpan,
                            SpanContext,
                            TraceFlags,
                            set_span_in_context,
                        )
                        parent_ctx = set_span_in_context(
                            NonRecordingSpan(
                                SpanContext(
                                    trace_id=int(parsed["trace_id"], 16),
                                    span_id=int(parsed["span_id"], 16),
                                    is_remote=True,
                                    trace_flags=TraceFlags(int(parsed["flags"], 16)),
                                )
                            )
                        )
                    except Exception:
                        parent_ctx = None

        with _TRACER.start_as_current_span(name, context=parent_ctx, attributes=sanitized_attrs) as raw_span:
            active_tp = format_span_traceparent(raw_span, fallback=None)
            if active_tp is None:
                # OTel Tracer returned proxy/noop span (zero SpanContext)
                if effective_incoming_tp:
                    parsed = parse_traceparent(effective_incoming_tp)
                    if parsed:
                        active_tp = f"00-{parsed['trace_id']}-{secrets.token_hex(8)}-{parsed['flags']}"
                    else:
                        active_tp = generate_traceparent()
                else:
                    active_tp = generate_traceparent()

            tok = _CURRENT_TRACEPARENT.set(active_tp)
            adapter = SpanAdapter(raw_span, traceparent=active_tp)
            try:
                yield adapter
            finally:
                adapter.ended = True
                _CURRENT_TRACEPARENT.reset(tok)
    else:
        if effective_incoming_tp:
            parsed = parse_traceparent(effective_incoming_tp)
            if parsed:
                active_tp = f"00-{parsed['trace_id']}-{secrets.token_hex(8)}-{parsed['flags']}"
            else:
                active_tp = generate_traceparent()
        else:
            active_tp = generate_traceparent()

        span = NoOpSpan(name, attributes=sanitized_attrs, traceparent=active_tp)
        tok = _CURRENT_TRACEPARENT.set(active_tp)
        try:
            yield span
        finally:
            span.end()
            _CURRENT_TRACEPARENT.reset(tok)


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
        elif (
            isinstance(val, str)
            and key_lower not in _TRACE_KEYS
            and (_EMAIL_REGEX.search(val) or _CARD_REGEX.search(val))
        ):
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

        eff_traceparent = traceparent or get_current_traceparent()
        if eff_traceparent:
            record["traceparent"] = eff_traceparent

        record.update(sanitized_extra)

        # Assert no forbidden keys leaked into record
        assert is_pii_clean(record), "PII hygiene violation: log entry contains forbidden fields"

        json_line = json.dumps(record, separators=(",", ":"))
        self.logger.log(level, json_line)
        return record
