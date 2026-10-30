"""Model Armor security and sanitization client for Support Ticket Router.

# REQ-MA-002: Regional Keep-Alive REST Client, Typed Verdicts & Measured Timeout
# REQ-MA-003: Ingress Concurrent Evaluation, Security Policy & Fail-Safe Escalation
# REQ-MA-004: Egress Sanitization for FaqResolver Answers & Fallback Handoff
# REQ-MA-005: In-Region Latency Budget, Concurrent SLA & Degraded Metrics

Provides typed ingress and egress sanitization via Google Cloud Model Armor:
- Targets regional endpoint https://modelarmor.us-central1.rep.googleapis.com
- Persistent pooled keep-alive session (requests.Session + HTTPAdapter)
- Automatic ADC bearer token resolution via ticket_router.common.auth
- CircuitBreaker protection with measured timeout and fail-safe degradation
- Emits model_armor_degraded structured telemetry on outage/timeout
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

import requests
from requests.adapters import HTTPAdapter

from ticket_router.common.auth import get_access_token
from ticket_router.common.observability import StructuredJsonLogger
from ticket_router.common.resilience import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitBreakerOpenError,
    ServiceTimeoutError,
)

logger = logging.getLogger(__name__)

# Default regional endpoint per TDD §3.7 and REQ-MA-002
DEFAULT_MODEL_ARMOR_BASE_URL: str = (
    "https://modelarmor.us-central1.rep.googleapis.com/v1"
)
DEFAULT_TIMEOUT_S: float = 0.700  # REQ-MA-005: max(500 ms, p99 x 1.5); p99 457.7 ms measured from laya-tune 2026-10-28
# Egress screens full FAQ answers (longer text, off the 3 s triage path, inside the 20 s FAQ budget).
DEFAULT_EGRESS_TIMEOUT_S: float = 2.500


@dataclass(frozen=True)
class FilterVerdict:
    """Individual filter outcome from Model Armor (PI/JB, URI, or RAI)."""

    tripped: bool = False
    confidence: str = "UNKNOWN"
    execution_state: str = "EXECUTION_SUCCESS"
    message: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SdpFinding:
    """Sensitive Data Protection inspection finding."""

    info_type: str = ""
    likelihood: str = ""
    location: Any = None  # raw SDP location dict (codepointRange), REQ-STORE-009


@dataclass(frozen=True)
class ModelArmorVerdict:
    """Comprehensive typed verdict from a Model Armor sanitization operation."""

    is_clean: bool
    blocked: bool
    action: str  # "ALLOW" | "BLOCK" | "DEGRADE"
    pi_jb: Optional[FilterVerdict] = None
    malicious_uri: Optional[FilterVerdict] = None
    rai_categories: Dict[str, FilterVerdict] = field(default_factory=dict)
    sdp_findings: List[SdpFinding] = field(default_factory=list)
    sanitized_text: Optional[str] = None
    degraded: bool = False
    reason: Optional[str] = None
    latency_ms: float = 0.0
    raw_filter_results: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_pi_jb(self) -> bool:
        return self.pi_jb is not None and self.pi_jb.tripped

    @property
    def has_malicious_uri(self) -> bool:
        return self.malicious_uri is not None and self.malicious_uri.tripped

    @property
    def has_rai_violation(self) -> bool:
        return any(v.tripped for v in self.rai_categories.values())

    @property
    def has_sdp_findings(self) -> bool:
        return len(self.sdp_findings) > 0

    def to_summary_dict(self) -> Dict[str, Any]:
        """PII-free summary dictionary for ticket metadata and structured logs (REQ-AGENT-014)."""
        rai_tripped = [k for k, v in self.rai_categories.items() if v.tripped]
        return {
            "action": self.action,
            "blocked": self.blocked,
            "is_clean": self.is_clean,
            "degraded": self.degraded,
            "pi_jb_tripped": self.has_pi_jb,
            "malicious_uri_tripped": self.has_malicious_uri,
            "rai_tripped": rai_tripped,
            "sdp_findings_count": len(self.sdp_findings),
            "sdp_info_types": [f.info_type for f in self.sdp_findings if f.info_type],
            "reason": self.reason,
            "latency_ms": self.latency_ms,
        }


@runtime_checkable
class ModelArmorClientProtocol(Protocol):
    """Protocol contract for Model Armor security clients."""

    def sanitize_user_prompt(self, text: str) -> ModelArmorVerdict:
        """Sanitize inbound user prompt before routing or LLM processing."""
        ...

    def sanitize_model_response(self, text: str) -> ModelArmorVerdict:
        """Sanitize outbound synthesized model answer before customer delivery."""
        ...


def _filter_entry(filter_results: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """Return a filter's result dict from the live (and legacy flat) response shapes.

    Live v1 ``sanitizeUserPrompt`` returns ``filterResults`` as a map keyed by filter
    name, each wrapping a typed result, e.g.
    ``{"pi_and_jailbreak": {"piAndJailbreakFilterResult": {...}}}``. Older fixtures
    used the typed key directly at the top level. Both are accepted.
    """
    for key in keys:
        val = filter_results.get(key)
        if isinstance(val, dict) and val:
            return val
    for wrapper in filter_results.values():
        if isinstance(wrapper, dict):
            for key in keys:
                val = wrapper.get(key)
                if isinstance(val, dict) and val:
                    return val
    return {}


def _is_match(d: Dict[str, Any]) -> bool:
    return (
        d.get("matchState") == "MATCH_FOUND"
        or d.get("match_state") == "MATCH_FOUND"
        or d.get("tripped") is True
    )


def _verdict(d: Dict[str, Any]) -> "FilterVerdict":
    return FilterVerdict(
        tripped=_is_match(d),
        confidence=str(d.get("confidenceLevel") or d.get("confidence_level") or "UNKNOWN"),
        execution_state=str(
            d.get("executionState") or d.get("execution_state") or "EXECUTION_SUCCESS"
        ),
        message=d.get("message"),
        details=d,
    )


def redact_sdp_findings(text: str, findings: List[Dict[str, Any]]) -> Optional[str]:
    """Replace SDP finding spans (codepointRange) with ``[REDACTED_<INFO_TYPE>]``.

    Basic SDP inspects only and never returns de-identified text, so redaction is
    applied locally from the finding offsets (REQ-MA-004). Returns None when no
    finding carries a usable range.
    """
    spans = []
    for f in findings:
        loc = f.get("location") or {}
        rng = loc.get("codepointRange") or loc.get("codepoint_range") or {}
        try:
            start, end = int(rng.get("start", 0)), int(rng["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= start < end <= len(text):
            spans.append((start, end, str(f.get("infoType") or f.get("info_type") or "PII")))
    if not spans:
        return None
    out = text
    for start, end, info in sorted(spans, reverse=True):
        out = out[:start] + f"[REDACTED_{info}]" + out[end:]
    return out


def _request_text(request_body: Dict[str, Any]) -> Optional[str]:
    for key in ("userPromptData", "modelResponseData"):
        part = request_body.get(key)
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return part["text"]
    return None


def parse_model_armor_response(
    data: Dict[str, Any], latency_ms: float = 0.0, original_text: Optional[str] = None
) -> ModelArmorVerdict:
    """Parse Model Armor REST response payload into typed ModelArmorVerdict."""
    sanitization_result = (
        data.get("sanitizationResult") or data.get("sanitization_result") or {}
    )
    filter_results = (
        sanitization_result.get("filterResults")
        or sanitization_result.get("filter_results")
        or {}
    )

    pi_verdict = _verdict(
        _filter_entry(filter_results, "piAndJailbreakFilterResult", "pi_and_jailbreak_filter_result")
    )
    uri_verdict = _verdict(
        _filter_entry(filter_results, "maliciousUriFilterResult", "malicious_uri_filter_result")
    )
    csam = _filter_entry(filter_results, "csamFilterFilterResult", "csam_filter_filter_result")

    rai_data = _filter_entry(filter_results, "raiFilterResult", "rai_filter_result")
    rai_categories: Dict[str, FilterVerdict] = {}
    type_results = (
        rai_data.get("raiFilterTypeResults") or rai_data.get("rai_filter_type_results") or {}
    )
    for cat_name, cat_res in type_results.items():
        if isinstance(cat_res, dict):
            rai_categories[cat_name] = _verdict(cat_res)
    if _is_match(csam):
        rai_categories["csam"] = _verdict(csam)

    sdp_data = _filter_entry(filter_results, "sdpFilterResult", "sdp_filter_result")
    inspect_res = (
        sdp_data.get("inspectResult")
        or sdp_data.get("inspect_result")
        or sdp_data.get("sdpInspectionResult")
        or sdp_data.get("sdp_inspection_result")
        or {}
    )
    raw_findings = [f for f in (inspect_res.get("findings") or []) if isinstance(f, dict)]
    sdp_findings: List[SdpFinding] = [
        SdpFinding(
            info_type=str(f.get("infoType") or f.get("info_type") or ""),
            likelihood=str(f.get("likelihood") or ""),
            location=f.get("location") if isinstance(f.get("location"), dict) else None,
        )
        for f in raw_findings
    ]

    deidentify_res = (
        sdp_data.get("deidentifyResult")
        or sdp_data.get("deidentify_result")
        or sdp_data.get("sdpDeidentifyResult")
        or sdp_data.get("sdp_deidentify_result")
        or {}
    )
    deid_data = deidentify_res.get("data") or {}
    sanitized_item = (
        sanitization_result.get("sanitizedItem") or sanitization_result.get("sanitized_item") or {}
    )
    sanitized_text: Optional[str] = (
        (deid_data.get("text") if isinstance(deid_data, dict) else None)
        or deidentify_res.get("deidentifiedContent")
        or deidentify_res.get("deidentified_content")
        or (sanitized_item.get("text") if isinstance(sanitized_item, dict) else None)
    )
    if sanitized_text is None and original_text is not None and raw_findings:
        sanitized_text = redact_sdp_findings(original_text, raw_findings)

    has_pi = pi_verdict.tripped
    has_uri = uri_verdict.tripped
    has_rai = any(v.tripped for v in rai_categories.values())
    has_sdp = len(sdp_findings) > 0

    blocked = has_pi or has_uri or has_rai
    is_clean = not blocked and not has_sdp
    action = "BLOCK" if blocked else "ALLOW"

    return ModelArmorVerdict(
        is_clean=is_clean,
        blocked=blocked,
        action=action,
        pi_jb=pi_verdict,
        malicious_uri=uri_verdict,
        rai_categories=rai_categories,
        sdp_findings=sdp_findings,
        sanitized_text=sanitized_text,
        degraded=False,
        reason=None,
        latency_ms=latency_ms,
        raw_filter_results=filter_results,
    )


class ModelArmorClient(ModelArmorClientProtocol):
    """Regional HTTP client communicating with Google Cloud Model Armor REST API.

    Implements:
    - REQ-MA-002: Regional pooled keep-alive session, ADC token, CircuitBreaker
    - REQ-MA-005: Measured latency budgets and degraded structured telemetry
    """

    def __init__(
        self,
        template: str,
        endpoint_base_url: str = DEFAULT_MODEL_ARMOR_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        circuit_breaker: Optional[CircuitBreaker] = None,
        session: Optional[requests.Session] = None,
        token_provider: Optional[Callable[[], str]] = None,
        logger: Optional[StructuredJsonLogger] = None,
        egress_timeout_s: float = DEFAULT_EGRESS_TIMEOUT_S,
    ) -> None:
        self.template = template
        self.endpoint_base_url = endpoint_base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.egress_timeout_s = egress_timeout_s
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            name="model_armor",
            failure_threshold=3,
            base_cooldown_s=30.0,
            max_cooldown_s=300.0,
            jitter_ratio=0.20,
        )
        self.token_provider = token_provider or get_access_token
        self.logger = logger or StructuredJsonLogger(name="model_armor_client")
        self._lock = threading.Lock()

        self._injected_session = session is not None
        self._injected_token_provider = token_provider is not None
        self._custom_auth = self._injected_session or self._injected_token_provider

        if self._injected_session:
            self._session: Optional[requests.Session] = session
            self._is_mtls: bool = bool(getattr(session, "is_mtls", False))
        elif self._injected_token_provider:
            self._session = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=10, pool_maxsize=20, max_retries=0
            )
            self._session.mount("https://", adapter)
            self._session.mount("http://", adapter)
            self._is_mtls = False
        else:
            self._session = None
            self._is_mtls = False

    @property
    def template_name(self) -> str:
        return self.template

    def _get_session(self) -> requests.Session:
        if self._session is None:
            with self._lock:
                if self._session is None:
                    from ticket_router.common.google_http import (
                        create_google_session,
                        to_mtls_url,
                    )

                    # Bounded auth refresh attempts (max 1) for latency predictability
                    sess, is_mtls = create_google_session(max_refresh_attempts=1)
                    # Publish _is_mtls and endpoint rewrite before _session to avoid race conditions
                    self._is_mtls = is_mtls
                    self.endpoint_base_url = to_mtls_url(self.endpoint_base_url, is_mtls)
                    self._session = sess
        return self._session

    @property
    def session(self) -> requests.Session:
        return self._get_session()

    def _call_api(
        self,
        rpc_name: str,
        request_body: Dict[str, Any],
        timeout_s: Optional[float] = None,
    ) -> ModelArmorVerdict:
        """Execute Model Armor REST RPC within circuit breaker and timeout guards."""
        budget_s = self.timeout_s if timeout_s is None else timeout_s
        start_t = time.monotonic()

        def _do_post() -> requests.Response:
            session = self._get_session()
            from ticket_router.common.google_http import to_mtls_url

            is_mtls = bool(getattr(session, "is_mtls", self._is_mtls))
            effective_base_url = to_mtls_url(self.endpoint_base_url, is_mtls)
            url = f"{effective_base_url}/{self.template}:{rpc_name}"

            if self._custom_auth:
                token = self.token_provider()
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
            else:
                headers = {
                    "Content-Type": "application/json",
                }
            return session.post(
                url,
                json=request_body,
                headers=headers,
                timeout=budget_s,
            )

        try:
            resp: requests.Response = self.circuit_breaker.call_with_timeout(
                _do_post, budget_s
            )
            latency_ms = (time.monotonic() - start_t) * 1000.0
            if resp.status_code != 200:
                self.circuit_breaker.record_failure()
                msg = f"Model Armor returned HTTP {resp.status_code}: {resp.text[:200]}"
                self._emit_degraded(msg)
                return ModelArmorVerdict(
                    is_clean=False,
                    blocked=False,
                    action="DEGRADE",
                    degraded=True,
                    reason=msg,
                    latency_ms=latency_ms,
                )

            data = resp.json()
            return parse_model_armor_response(data, latency_ms=latency_ms, original_text=_request_text(request_body))

        except (
            CircuitBreakerOpenError,
            CircuitBreakerError,
            ServiceTimeoutError,
            requests.Timeout,
            requests.RequestException,
            Exception,
        ) as exc:
            latency_ms = (time.monotonic() - start_t) * 1000.0
            # CircuitBreaker.call() already recorded this failure; recording again
            # would halve the effective failure_threshold.
            reason_msg = f"Model Armor call failed ({type(exc).__name__}): {exc}"
            self._emit_degraded(reason_msg)
            return ModelArmorVerdict(
                is_clean=False,
                blocked=False,
                action="DEGRADE",
                degraded=True,
                reason=reason_msg,
                latency_ms=latency_ms,
            )

    def _emit_degraded(self, reason: str) -> None:
        """Emit structured log with model_armor_degraded metric flag (REQ-MA-002, REQ-MA-005)."""
        logger.warning(
            "model_armor_degraded: %s",
            reason,
            extra={"model_armor_degraded": True, "reason": reason, "degraded": True},
        )
        self.logger.logger.warning(
            "model_armor_degraded",
            extra={"model_armor_degraded": True, "degraded": True, "reason": reason},
        )

    def sanitize_user_prompt(self, text: str) -> ModelArmorVerdict:
        """Sanitize inbound user prompt via Model Armor sanitizeUserPrompt."""
        body = {"user_prompt_data": {"text": text}}
        return self._call_api("sanitizeUserPrompt", body)

    def sanitize_model_response(self, text: str) -> ModelArmorVerdict:
        """Sanitize outbound model response via Model Armor sanitizeModelResponse."""
        body = {"model_response_data": {"text": text}}
        return self._call_api("sanitizeModelResponse", body, timeout_s=self.egress_timeout_s)


class FakeModelArmorClient(ModelArmorClientProtocol):
    """Hermetic in-memory test fake for Model Armor."""

    def __init__(
        self,
        default_clean: bool = True,
        block_pi_jb: bool = False,
        block_malicious_uri: bool = False,
        block_rai: bool = False,
        rai_categories: Optional[List[str]] = None,
        sdp_findings: Optional[List[SdpFinding]] = None,
        sdp_sanitized_text: Optional[str] = None,
        fail_with_timeout: bool = False,
        fail_with_error: bool = False,
        circuit_open: bool = False,
        latency_ms: float = 15.0,
    ) -> None:
        self.default_clean = default_clean
        self.block_pi_jb = block_pi_jb
        self.block_malicious_uri = block_malicious_uri
        self.block_rai = block_rai
        self.rai_categories = rai_categories or (["HARASSMENT"] if block_rai else [])
        self.sdp_findings = sdp_findings or []
        self.sdp_sanitized_text = sdp_sanitized_text
        self.fail_with_timeout = fail_with_timeout
        self.fail_with_error = fail_with_error
        self.circuit_open = circuit_open
        self.latency_ms = latency_ms
        self.prompt_calls: List[str] = []
        self.response_calls: List[str] = []

    @property
    def template_name(self) -> str:
        return "projects/test-proj/locations/us-central1/templates/support-router-lean"

    def set_outage(self, enabled: bool) -> None:
        self.circuit_open = enabled

    def _build_verdict(self, text: str) -> ModelArmorVerdict:
        if self.circuit_open:
            return ModelArmorVerdict(
                is_clean=False,
                blocked=False,
                action="DEGRADE",
                degraded=True,
                reason="model_armor_degraded: CircuitBreaker is OPEN",
                latency_ms=self.latency_ms,
            )
        if self.fail_with_timeout:
            return ModelArmorVerdict(
                is_clean=False,
                blocked=False,
                action="DEGRADE",
                degraded=True,
                reason="model_armor_degraded: ServiceTimeoutError (500 ms)",
                latency_ms=self.latency_ms,
            )
        if self.fail_with_error:
            return ModelArmorVerdict(
                is_clean=False,
                blocked=False,
                action="DEGRADE",
                degraded=True,
                reason="model_armor_degraded: ConnectionError",
                latency_ms=self.latency_ms,
            )

        lower = text.lower()
        pi_tripped = self.block_pi_jb or (
            ("ignore" in lower and ("instructions" in lower or "prompt" in lower))
            or ("reveal" in lower and "keys" in lower)
            or "dan" in lower
        )
        uri_tripped = self.block_malicious_uri or (
            "malicious" in lower or "evil.com" in lower or "phish" in lower
        )
        abuse_tripped = self.block_rai or any(
            bad_word in lower
            for bad_word in ["stupid", "idiot", "useless", "go to hell", "f***"]
        )

        pi_v = FilterVerdict(
            tripped=pi_tripped,
            confidence="MEDIUM_AND_ABOVE" if pi_tripped else "UNKNOWN",
        )
        uri_v = FilterVerdict(
            tripped=uri_tripped,
            confidence="HIGH" if uri_tripped else "UNKNOWN",
        )
        rai_cats: Dict[str, FilterVerdict] = {}
        for c in self.rai_categories:
            rai_cats[c] = FilterVerdict(tripped=True, confidence="MEDIUM_AND_ABOVE")
        if abuse_tripped and "HARASSMENT" not in rai_cats:
            rai_cats["HARASSMENT"] = FilterVerdict(
                tripped=True, confidence="MEDIUM_AND_ABOVE"
            )

        blocked = pi_tripped or uri_tripped or bool(rai_cats)

        # SDP simulation
        sanitized = self.sdp_sanitized_text
        findings = list(self.sdp_findings)
        if sanitized is None and "4111 1111 1111 1111" in text:
            sanitized = text.replace("4111 1111 1111 1111", "[CREDIT_CARD_NUMBER]")
            if not findings:
                findings.append(
                    SdpFinding(info_type="CREDIT_CARD_NUMBER", likelihood="VERY_LIKELY")
                )

        has_sdp = len(findings) > 0 or (sanitized is not None and sanitized != text)
        is_clean = not blocked and not has_sdp

        return ModelArmorVerdict(
            is_clean=is_clean,
            blocked=blocked,
            action="BLOCK" if blocked else "ALLOW",
            pi_jb=pi_v,
            malicious_uri=uri_v,
            rai_categories=rai_cats,
            sdp_findings=findings,
            sanitized_text=sanitized,
            degraded=False,
            latency_ms=self.latency_ms,
        )

    def sanitize_user_prompt(self, text: str) -> ModelArmorVerdict:
        self.prompt_calls.append(text)
        return self._build_verdict(text)

    def sanitize_model_response(self, text: str) -> ModelArmorVerdict:
        self.response_calls.append(text)
        return self._build_verdict(text)


def create_model_armor_client(
    template: str,
    enabled: bool = True,
    endpoint_base_url: str = DEFAULT_MODEL_ARMOR_BASE_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    fake_client: Optional[ModelArmorClientProtocol] = None,
) -> Optional[ModelArmorClientProtocol]:
    """Factory helper to construct Model Armor client respecting enabled flag."""
    if fake_client is not None:
        return fake_client
    if not enabled:
        return None
    return ModelArmorClient(
        template=template,
        endpoint_base_url=endpoint_base_url,
        timeout_s=timeout_s,
    )
