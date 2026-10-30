"""Router-S1 Vertex AI Prediction Endpoint Client.

# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies
# REQ-AGENT-012: Execution Budgets, Service Timeouts, and Latency SLAs
# REQ-AUTH-006: mTLS-Aware Google API Transports (certificate-bound agent identity tokens)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import requests
from google.auth.transport.requests import AuthorizedSession
from requests.adapters import HTTPAdapter

from ticket_router.common.auth import get_access_token
from ticket_router.common.google_http import create_google_session, to_mtls_url
from ticket_router.common.resilience import (
    LAYA_RERANK_TIMEOUT_S,
    ROUTER_S1_TIMEOUT_S,
    CircuitBreaker,
)
from ticket_router.config.models import get_serving_config

logger = logging.getLogger(__name__)

_SERVING_CFG = get_serving_config("router_s1")
DEFAULT_ENDPOINT_RESOURCE = _SERVING_CFG.get(
    "default_endpoint",
    "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1",
)
DEFAULT_ROUTE_TIMEOUT_S = float(_SERVING_CFG.get("route_timeout_s", ROUTER_S1_TIMEOUT_S))
DEFAULT_RERANK_TIMEOUT_S = float(_SERVING_CFG.get("rerank_timeout_s", LAYA_RERANK_TIMEOUT_S))

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
    "?scopes=https://www.googleapis.com/auth/cloud-platform"
)
METADATA_TOKEN_TIMEOUT_S = 0.25
UNBOUND_TOKEN_EXPIRY_SKEW_S = 60.0
_DEDICATED_VERTEX_SUFFIX = ".prediction.vertexai.goog"


def _is_googleapis_url(url: str) -> bool:
    """Return True iff url targets a *.googleapis.com host."""
    hostname = (urllib.parse.urlsplit(url).hostname or "").lower()
    return hostname.endswith(".googleapis.com") and len(hostname) > len(".googleapis.com")


def _is_allowlisted_unbound_mds_url(url: str) -> bool:
    """Return True iff url is HTTPS on a dedicated *.prediction.vertexai.goog host (REQ-AUTH-006.5).

    Unbound metadata-server access tokens are bearer credentials valid across Google APIs;
    restricting them strictly to https://*.prediction.vertexai.goog prevents leaking tokens
    over plaintext http:// or to non-allowlisted hosts.
    """
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower()
    return (
        scheme == "https"
        and hostname.endswith(_DEDICATED_VERTEX_SUFFIX)
        and len(hostname) > len(_DEDICATED_VERTEX_SUFFIX)
    )


def _create_default_session() -> requests.Session:
    """Create thread-safe requests session with keep-alive connection pooling."""
    sess = requests.Session()
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


@dataclass(frozen=True)
class CandidateScore:
    """Reranker score for a knowledge-base article candidate."""
    id: str
    score: float
    latency_ms: float = 0.0


@dataclass(frozen=True)
class RouterPrediction:
    """Typed multi-head prediction returned by the router-s1 encoder."""
    escalate: bool
    escalate_prob: float
    sentiment: str
    sentiment_prob: float
    urgency: str
    urgency_prob: float
    intent: str
    intent_prob: float
    probabilities: Dict[str, Dict[str, float]] = field(default_factory=dict)
    latency_ms: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)
    out_of_scope: bool = False


class RouterS1Client:
    """Client for Vertex AI prediction endpoint serving the fine-tuned router-s1 encoder.

    Follows AGENTS.md rule 4: calls :rawPredict on Vertex endpoints serving Laya.
    Protected by CircuitBreaker (REQ-AGENT-011) with strict timeout budget (REQ-AGENT-012).
    Reuses persistent keep-alive HTTPS connections via requests.Session and HTTPAdapter.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_s: Optional[float] = None,
        route_timeout_s: Optional[float] = None,
        rerank_timeout_s: Optional[float] = None,
        breaker: Optional[CircuitBreaker] = None,
        session: Optional[Any] = None,
        token_provider: Optional[Callable[[], Optional[str]]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.endpoint_resource = endpoint or os.environ.get("ROUTER_S1_ENDPOINT", DEFAULT_ENDPOINT_RESOURCE)
        self.base_url = base_url or os.environ.get("ROUTER_S1_BASE_URL")

        self.route_timeout_s = (
            route_timeout_s
            if route_timeout_s is not None
            else (timeout_s if timeout_s is not None else DEFAULT_ROUTE_TIMEOUT_S)
        )
        self.rerank_timeout_s = (
            rerank_timeout_s
            if rerank_timeout_s is not None
            else (timeout_s if timeout_s is not None else DEFAULT_RERANK_TIMEOUT_S)
        )
        self.timeout_s = self.route_timeout_s
        self.breaker = breaker or CircuitBreaker(name="router_s1")
        self.token_provider = token_provider
        self._clock: Callable[[], float] = clock or time.monotonic
        self._lock = threading.Lock()
        self._token_lock = threading.Lock()
        self._unbound_token: Optional[str] = None
        self._unbound_token_expiry: float = 0.0
        self._predict_url_override: Optional[str] = None

        # Resolve base prediction URL before mTLS host rewrite (REQ-AUTH-006)
        self._raw_predict_url = self._resolve_predict_url()
        self._is_googleapis_host = _is_googleapis_url(self._raw_predict_url)
        self._allow_unbound_mds_token = _is_allowlisted_unbound_mds_url(self._raw_predict_url)

        self._injected_session = session is not None
        self._injected_token_provider = token_provider is not None
        self._custom_auth = self._injected_session or self._injected_token_provider

        if self._injected_session:
            self._is_mtls: bool = getattr(session, "is_mtls", False) is True
            self._uses_unbound_mds_token: bool = False
            self._uses_authorized_session: bool = (
                isinstance(session, AuthorizedSession) and not self._injected_token_provider
            )
            self._session: Optional[Any] = session
        elif self._injected_token_provider:
            self._is_mtls = False
            self._uses_unbound_mds_token = False
            self._uses_authorized_session = False
            self._session = _create_default_session()
        else:
            self._is_mtls = False
            self._uses_unbound_mds_token = False
            self._uses_authorized_session = False
            self._session = None

    def _get_session(self) -> Any:
        """Lazily initialize pooled session when not injected (REQ-AUTH-006).

        - Uses max_refresh_attempts=0 so HTTP 401 costs at most 1 POST within 0.5 s budget.
        - Only for https://*.prediction.vertexai.goog when is_mtls=True, switches to a plain
          pooled requests.Session paired with a cached unbound metadata-server access token
          (REQ-AUTH-006.5); all other non-googleapis or http:// hosts keep the mTLS session.
        - Propagates session/credential errors into CircuitBreaker without caching (REQ-AUTH-006.6).
        - Publishes mode flags before _session to prevent unlocked fast-path races.
        """
        sess_ref = self._session
        if sess_ref is None:
            with self._lock:
                sess_ref = self._session
                if sess_ref is None:
                    google_sess, detected_mtls = create_google_session(
                        pool_connections=10,
                        pool_maxsize=20,
                        max_retries=0,
                        max_refresh_attempts=0,
                    )
                    is_mtls_bool = bool(detected_mtls)
                    if is_mtls_bool and self._allow_unbound_mds_token:
                        google_sess.close()
                        plain_sess = _create_default_session()
                        self._is_mtls = True
                        self._uses_unbound_mds_token = True
                        self._uses_authorized_session = False
                        self._session = plain_sess
                        sess_ref = plain_sess
                    else:
                        self._is_mtls = is_mtls_bool
                        self._uses_unbound_mds_token = False
                        self._uses_authorized_session = True
                        self._session = google_sess
                        sess_ref = google_sess
        return sess_ref

    @property
    def session(self) -> Any:
        return self._get_session()

    @session.setter
    def session(self, value: Any) -> None:
        self._injected_session = value is not None
        self._custom_auth = self._injected_session or self._injected_token_provider
        self._is_mtls = getattr(value, "is_mtls", False) is True
        self._uses_unbound_mds_token = False
        self._uses_authorized_session = (
            isinstance(value, AuthorizedSession) and not self._injected_token_provider
        )
        self._session = value

    @property
    def is_mtls(self) -> bool:
        if self._session is None and not self._custom_auth:
            self._get_session()
        return self._is_mtls

    @property
    def predict_url(self) -> str:
        if self._predict_url_override is not None:
            return self._predict_url_override
        if not self._is_googleapis_host:
            return self._raw_predict_url
        return to_mtls_url(self._raw_predict_url, self.is_mtls)

    @predict_url.setter
    def predict_url(self, value: str) -> None:
        self._predict_url_override = value

    def _resolve_predict_url(self) -> str:
        """Resolve full target URL using base_url or Vertex endpoint resource."""
        if self.base_url:
            cleaned = self.base_url.strip().rstrip("/")
            if not cleaned.startswith(("http://", "https://")):
                cleaned = f"https://{cleaned}"
            if cleaned.endswith((":rawPredict", ":predict", "/predict")):
                return cleaned
            if self.endpoint_resource:
                endpoint_path = self.endpoint_resource.lstrip("/")
                return f"{cleaned}/v1/{endpoint_path}:rawPredict"
            return f"{cleaned}/predict"

        # Vertex AI endpoint format: projects/{project}/locations/{location}/endpoints/{endpoint_id}
        match = re.match(
            r"^projects/(?P<project>[^/]+)/locations/(?P<location>[^/]+)/endpoints/(?P<endpoint>[^/]+)$",
            self.endpoint_resource,
        )
        if match:
            location = match.group("location")
            return f"https://{location}-aiplatform.googleapis.com/v1/{self.endpoint_resource}:rawPredict"

        # Fallback if bare endpoint ID or relative path
        return f"https://us-central1-aiplatform.googleapis.com/v1/{self.endpoint_resource}:rawPredict"

    def _fetch_unbound_mds_token(self, session: requests.Session) -> tuple[str, float]:
        """Fetch an unbound OAuth2 access token from GCE metadata server via plain GET (REQ-AUTH-006.5)."""
        resp = session.get(
            METADATA_TOKEN_URL,
            headers={"Metadata-Flavor": "Google"},
            timeout=METADATA_TOKEN_TIMEOUT_S,
            allow_redirects=False,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError("Metadata server token endpoint returned non-JSON-object payload")
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Metadata server token response missing access_token")
        expires_in = float(data.get("expires_in", 3600))
        ttl_s = max(0.0, expires_in - UNBOUND_TOKEN_EXPIRY_SKEW_S)
        return access_token, self._clock() + ttl_s

    def _get_unbound_mds_token(self, session: requests.Session) -> str:
        """Return cached unbound metadata token or refresh thread-safely before expiry - 60 s."""
        now = self._clock()
        with self._token_lock:
            if self._unbound_token is not None and now < self._unbound_token_expiry:
                return self._unbound_token
            token, expiry = self._fetch_unbound_mds_token(session)
            self._unbound_token = token
            self._unbound_token_expiry = expiry
            return token

    def _invalidate_unbound_mds_token(self, failed_token: Optional[str] = None) -> None:
        """Invalidate cached unbound metadata token once on HTTP 401 (REQ-AUTH-006.5)."""
        with self._token_lock:
            if failed_token is None or self._unbound_token == failed_token:
                self._unbound_token = None
                self._unbound_token_expiry = 0.0

    def warm_connection(self) -> bool:
        """Pre-warm HTTP keep-alive connection pool to eliminate TLS handshake latency."""
        try:
            session = self._get_session()
            target_url = self.predict_url
            if "/v1/" in target_url:
                health_url = f"{target_url.split('/v1/')[0]}/healthz"
            elif target_url.endswith("/predict"):
                health_url = f"{target_url[:-8]}/healthz"
            else:
                health_url = target_url
            resp = session.get(health_url, timeout=0.5)
            return resp.status_code == 200
        except Exception:
            return False

    def close(self) -> None:
        """Close the underlying HTTP session connection pool."""
        if self._session is not None:
            self._session.close()

    def _build_headers(self, session: Any) -> tuple[Dict[str, str], Optional[str]]:
        """Construct request headers and return (headers, unbound_token_used_if_any)."""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ticket-router/router-s1-client",
        }
        if self._uses_unbound_mds_token:
            unbound_tok = self._get_unbound_mds_token(session)
            headers["Authorization"] = f"Bearer {unbound_tok}"
            return headers, unbound_tok
        if not self._uses_authorized_session:
            provider = self.token_provider or get_access_token
            token = provider()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers, None

    def _get_headers(self) -> Dict[str, str]:
        """Construct request headers, delegating Bearer auth to AuthorizedSession when active."""
        return self._build_headers(self._get_session())[0]

    def _execute_request(self, payload: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
        """Send HTTP POST request to endpoint via pooled session."""
        session = self._get_session()
        target_url = self.predict_url
        data_bytes = json.dumps(payload).encode("utf-8")
        headers, used_unbound_token = self._build_headers(session)

        resp = session.post(
            target_url,
            data=data_bytes,
            headers=headers,
            timeout=timeout_s,
        )
        if getattr(resp, "status_code", None) == 401:
            if used_unbound_token is not None:
                self._invalidate_unbound_mds_token(used_unbound_token)
            if self._is_mtls:
                target_host = urllib.parse.urlsplit(target_url).hostname or "unknown"
                auth_mode = "unbound_mds_token" if used_unbound_token is not None else "mtls_bound"
                logger.warning(
                    "router_s1_mtls_unauthorized: HTTP 401 from host=%s endpoint=%s "
                    "is_mtls=True auth_mode=%s",
                    target_host,
                    self.endpoint_resource,
                    auth_mode,
                    extra={
                        "event": "router_s1_mtls_unauthorized",
                        "status_code": 401,
                        "target_host": target_host,
                        "endpoint_resource": self.endpoint_resource,
                        "is_mtls": True,
                        "auth_mode": auth_mode,
                    },
                )
        resp.raise_for_status()
        return resp.json()

    def _post_http(self, payload: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
        """Execute raw HTTP POST, wrapped by CircuitBreaker."""
        return self.breaker.call(self._execute_request, payload, timeout_s)

    def route(self, text: str, timeout_s: Optional[float] = None) -> RouterPrediction:
        """Evaluate text via router-s1 encoder heads (REQ-AGENT-002)."""
        effective_timeout = timeout_s if timeout_s is not None else self.route_timeout_s
        payload = {
            "instances": [
                {
                    "task": "route",
                    "text": text,
                }
            ]
        }
        resp = self._post_http(payload, timeout_s=effective_timeout)
        return self._parse_route_response(resp)

    def rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        timeout_s: Optional[float] = None,
    ) -> List[CandidateScore]:
        """Score and rank candidates against query using choice reranker."""
        effective_timeout = timeout_s if timeout_s is not None else self.rerank_timeout_s
        payload = {
            "instances": [
                {
                    "task": "rerank",
                    "query": query,
                    "candidates": candidates,
                }
            ]
        }
        resp = self._post_http(payload, timeout_s=effective_timeout)
        return self._parse_rerank_response(resp)

    def _parse_route_response(self, resp: Dict[str, Any]) -> RouterPrediction:
        """Parse raw prediction JSON into a strongly typed RouterPrediction."""
        predictions = resp.get("predictions", [])
        if not predictions:
            if "escalate" in resp:
                pred = resp
            else:
                raise ValueError(f"Invalid route prediction response format: {resp}")
        else:
            pred = predictions[0]

        esc_head = pred.get("escalate", {})
        sent_head = pred.get("sentiment", {})
        urg_head = pred.get("urgency", {})
        intent_head = pred.get("intent", {})

        # Extract escalation decision
        esc_label = esc_head.get("label", False)
        if isinstance(esc_label, str):
            esc_bool = esc_label.lower() in ("true", "yes", "1", "escalate")
        else:
            esc_bool = bool(esc_label)

        esc_prob = float(esc_head.get("prob", 1.0 if esc_bool else 0.0))
        esc_probs = {str(k): float(v) for k, v in esc_head.get("probabilities", {}).items()}

        # Extract sentiment
        sent_label = str(sent_head.get("label", "neutral"))
        sent_prob = float(sent_head.get("prob", 0.0))
        sent_probs = {str(k): float(v) for k, v in sent_head.get("probabilities", {}).items()}

        # Extract urgency
        urg_label = str(urg_head.get("label", "normal"))
        urg_prob = float(urg_head.get("prob", 0.0))
        urg_probs = {str(k): float(v) for k, v in urg_head.get("probabilities", {}).items()}

        # Extract intent
        intent_label = str(intent_head.get("label", "other"))
        intent_prob = float(intent_head.get("prob", 0.0))
        intent_probs = {str(k): float(v) for k, v in intent_head.get("probabilities", {}).items()}

        latency_ms = float(pred.get("latency_ms", resp.get("latency_ms", 0.0)))

        all_probs = {
            "escalate": esc_probs,
            "sentiment": sent_probs,
            "urgency": urg_probs,
            "intent": intent_probs,
        }

        return RouterPrediction(
            escalate=esc_bool,
            escalate_prob=esc_prob,
            sentiment=sent_label,
            sentiment_prob=sent_prob,
            urgency=urg_label,
            urgency_prob=urg_prob,
            intent=intent_label,
            intent_prob=intent_prob,
            probabilities=all_probs,
            latency_ms=latency_ms,
            raw=pred,
        )

    def _parse_rerank_response(self, resp: Dict[str, Any]) -> List[CandidateScore]:
        """Parse raw rerank prediction JSON into sorted CandidateScore list."""
        predictions = resp.get("predictions", [])
        if not predictions:
            items = resp if isinstance(resp, list) else []
        else:
            items = predictions[0] if isinstance(predictions[0], list) else predictions

        scores: List[CandidateScore] = []
        for item in items:
            c_id = str(item.get("id", ""))
            score = float(item.get("score", 0.0))
            lat = float(item.get("latency_ms", 0.0))
            scores.append(CandidateScore(id=c_id, score=score, latency_ms=lat))

        # Ensure sorted descending by score
        scores.sort(key=lambda x: x.score, reverse=True)
        return scores
