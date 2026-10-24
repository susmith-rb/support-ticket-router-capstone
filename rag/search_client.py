#!/usr/bin/env python3
"""search_client.py — Unified Discovery Engine search client for Agent Search.

Builds and executes Agent Search requests against Discovery Engine data stores.
Supports:
- Location resolution (global vs regional/multi-regional)
- Status and effective-window filtering (REQ-RAG-RETR-009)
- Intent family filtering with automatic fallback (REQ-RAG-RETR-010)
- Relevance thresholds, ranking expressions, and query expansion
- Precise client-side HTTP round-trip latency measurements (TDD §6.4)
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Union
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from de_endpoint import api_base, data_store_name, search_url  # noqa: E402

DEFAULT_PROJECT = "susmith-fde-ticket-router"
DEFAULT_ENGINE = "support-kb-search"
DEFAULT_DATA_STORE = "faq-articles"
DEFAULT_LOCATION = "global"

DEFAULT_ALLOWED_STATUSES = ["active", "draft"]
VALID_INTENT_FAMILIES = [
    "account_payment",
    "order_status",
    "returns_refunds",
    "shipping_policy",
    "other",
]


def clean_query(text: str) -> str:
    """Strip @mentions, [URL]/[ORDER]/[EMAIL] tokens, and hashtag marks."""
    cleaned = re.sub(r"@\w+|\[(URL|ORDER|EMAIL|PHONE|CARD)\]", " ", text)
    return re.sub(r"\s+", " ", cleaned.replace("#", " ")).strip() or text


def build_filter(
    allowed_statuses: Optional[Union[str, Sequence[str]]] = None,
    intent_family: Optional[str] = None,
    effective_at: Optional[Union[str, datetime]] = None,
    custom_filter: Optional[str] = None,
) -> str:
    """Build a Discovery Engine filter expression string.

    Args:
        allowed_statuses: List or single status string (e.g. ['active', 'draft']).
                          Renders as: status: ANY("active", "draft").
        intent_family: Single intent family string (e.g. 'order_status').
                       Renders as: intent_family: ANY("order_status").
        effective_at: Optional datetime or ISO string for effective_from filtering.
                      Renders as: effective_from <= "<iso>".
        custom_filter: Any additional raw filter expression clause.

    Returns:
        A combined filter expression string joined with ' AND ', or empty string.
    """
    clauses = []

    if allowed_statuses:
        if isinstance(allowed_statuses, str):
            statuses = [allowed_statuses.strip()]
        else:
            statuses = [s.strip() for s in allowed_statuses if s.strip()]
        if statuses:
            formatted = ", ".join(f'"{s}"' for s in statuses)
            clauses.append(f"status: ANY({formatted})")

    if intent_family:
        family = intent_family.strip()
        if family:
            clauses.append(f'intent_family: ANY("{family}")')

    if effective_at:
        if isinstance(effective_at, datetime):
            iso_val = effective_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            iso_val = str(effective_at).strip()
        if iso_val:
            clauses.append(f'effective_from <= "{iso_val}"')

    if custom_filter:
        c = custom_filter.strip()
        if c:
            clauses.append(f"({c})")

    return " AND ".join(clauses)


@dataclass
class SearchResult:
    """Structured result returned by SearchClient."""
    document_ids: List[str]
    latency_ms: float
    total_size: int
    filter_applied: str
    fallback_triggered: bool = False
    raw_results: List[Dict[str, Any]] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)


class SearchClient:
    """Client for Discovery Engine Agent Search with filtering and fallback."""

    def __init__(
        self,
        project: str = DEFAULT_PROJECT,
        engine: str = DEFAULT_ENGINE,
        data_store: str = DEFAULT_DATA_STORE,
        location: str = DEFAULT_LOCATION,
        token: Optional[str] = None,
        default_allowed_statuses: Optional[Sequence[str]] = tuple(DEFAULT_ALLOWED_STATUSES),
        sleep_sec: float = 0.0,
    ):
        self.project = project
        self.engine = engine
        self.data_store = data_store
        self.location = location
        self._token = token
        self.default_allowed_statuses = list(default_allowed_statuses) if default_allowed_statuses else None
        self.sleep_sec = sleep_sec

        self.url = search_url(self.project, self.engine, location=self.location)
        self.ds_path = data_store_name(self.project, self.data_store, location=self.location)

    def get_token(self) -> str:
        if self._token:
            return self._token
        return subprocess.check_output(
            ["gcloud", "auth", "application-default", "print-access-token"], text=True
        ).strip()

    def _execute_http(self, body: Dict[str, Any], token: str, sleep_sec: float) -> tuple[Dict[str, Any], float]:
        """Execute a single HTTP search request and return (response_dict, latency_ms)."""
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "x-goog-user-project": self.project,
                "Content-Type": "application/json",
            },
        )
        err = "search failed"
        for attempt in range(8):
            try:
                t0 = time.perf_counter()
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read().decode("utf-8"))
                latency_ms = (time.perf_counter() - t0) * 1000.0
                if sleep_sec > 0:
                    time.sleep(sleep_sec)
                return data, latency_ms
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:400]}"
                if e.code == 429:
                    time.sleep(min(60, 5 * (attempt + 1)))
                    continue
                break
        raise RuntimeError(err)

    def search(
        self,
        query: str,
        page_size: int = 10,
        threshold: str = "",
        clean: bool = False,
        allowed_statuses: Any = "DEFAULT",
        intent_family: Optional[str] = None,
        fallback_min_results: int = 2,
        ranking_expression: str = "",
        query_expansion: bool = False,
        effective_at: Optional[Union[str, datetime]] = None,
        custom_filter: Optional[str] = None,
        extra_specs: Optional[Dict[str, Any]] = None,
        sleep_sec: Optional[float] = None,
        token: Optional[str] = None,
    ) -> SearchResult:
        """Perform search with status and intent filtering, plus fallback if results < fallback_min_results."""
        tok = token or self.get_token()
        throttle = self.sleep_sec if sleep_sec is None else sleep_sec

        statuses = self.default_allowed_statuses if allowed_statuses == "DEFAULT" else allowed_statuses
        q_text = clean_query(query) if clean else query

        # 1. Build initial filter
        primary_filter = build_filter(
            allowed_statuses=statuses,
            intent_family=intent_family,
            effective_at=effective_at,
            custom_filter=custom_filter,
        )

        body: Dict[str, Any] = {
            "query": q_text[:1000],
            "pageSize": page_size,
            "dataStoreSpecs": [{"dataStore": self.ds_path}],
        }
        if threshold:
            body["relevanceThreshold"] = threshold
        if primary_filter:
            body["filter"] = primary_filter
        if ranking_expression:
            body["rankingExpression"] = ranking_expression
        if query_expansion:
            body["queryExpansionSpec"] = {"condition": "AUTO"}
            body["spellCorrectionSpec"] = {"mode": "AUTO"}
        if extra_specs:
            body.update(extra_specs)

        # 2. Execute primary search
        data, latency_ms = self._execute_http(body, tok, throttle)
        raw_results = data.get("results", [])
        doc_ids = [r["document"]["id"] for r in raw_results if "document" in r and "id" in r["document"]]
        total_size = data.get("totalSize", len(doc_ids))
        summary = data.get("summary", {})

        # 3. Check fallback condition:
        # Fallback triggers if an intent_family filter was applied AND results < fallback_min_results
        fallback_triggered = False
        if intent_family and len(doc_ids) < fallback_min_results:
            fallback_triggered = True
            fallback_filter = build_filter(
                allowed_statuses=statuses,
                intent_family=None,
                effective_at=effective_at,
                custom_filter=custom_filter,
            )
            fallback_body = dict(body)
            if fallback_filter:
                fallback_body["filter"] = fallback_filter
            else:
                fallback_body.pop("filter", None)

            fallback_data, fb_latency_ms = self._execute_http(fallback_body, tok, throttle)
            latency_ms += fb_latency_ms  # Cumulative client latency for both rounds
            raw_results = fallback_data.get("results", [])
            doc_ids = [r["document"]["id"] for r in raw_results if "document" in r and "id" in r["document"]]
            total_size = fallback_data.get("totalSize", len(doc_ids))
            summary = fallback_data.get("summary", {})
            applied_filter = fallback_filter
        else:
            applied_filter = primary_filter

        return SearchResult(
            document_ids=doc_ids,
            latency_ms=round(latency_ms, 2),
            total_size=total_size,
            filter_applied=applied_filter,
            fallback_triggered=fallback_triggered,
            raw_results=raw_results,
            summary=summary,
        )
