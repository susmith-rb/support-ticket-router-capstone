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

import json
import pathlib
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Union

from ticket_router.common.auth import get_access_token
from ticket_router.config.models import model_id
from ticket_router.retrieval.de_endpoint import api_base, data_store_name, search_url

DEFAULT_PROJECT = "susmith-fde-ticket-router"
DEFAULT_ENGINE = "support-kb-search"
DEFAULT_DATA_STORE = "faq-articles"
DEFAULT_LOCATION = "global"

DEFAULT_ALLOWED_STATUSES = ["active", "draft"]
DEFAULT_FAQ_DOCS_DIR = pathlib.Path(
    "/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag/faq_v2p/docs"
)
VALID_INTENT_FAMILIES = [
    "account_payment",
    "order_status",
    "returns_refunds",
    "shipping_policy",
    "other",
]


def load_rich_faq_records(
    docs_dir: Optional[Union[str, pathlib.Path]] = None,
) -> Dict[str, Dict[str, str]]:
    """Load rich FAQ article representations for Discovery Engine Ranking API.

    Each document in docs_dir is parsed into:
    - id: document stem (e.g. faq-order-status-late-or-not-arrived-order)
    - title: markdown heading (# Title)
    - content: "Also asked as:\n<bullets>\n\n<body_first_600>"
    """
    dir_path = pathlib.Path(docs_dir) if docs_dir else DEFAULT_FAQ_DOCS_DIR
    if not dir_path.exists():
        # Fallback to local corpus_v2 if scratch path is not present
        alt_path = pathlib.Path(__file__).parent / "corpus_v2" / "docs"
        if alt_path.exists():
            dir_path = alt_path
        else:
            return {}

    records: Dict[str, Dict[str, str]] = {}
    for f in sorted(dir_path.glob("*.txt")):
        text = f.read_text(encoding="utf-8")
        lines = text.splitlines()

        title = ""
        for line in lines:
            if line.startswith("# "):
                title = line[2:].strip()
                break

        questions: List[str] = []
        in_also = False
        body_lines: List[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped == "Also asked as:":
                in_also = True
                continue
            if in_also:
                if stripped.startswith("- "):
                    questions.append(stripped[2:].strip())
                elif stripped == "":
                    continue
                else:
                    in_also = False
                    body_lines.append(line)
            else:
                if not line.startswith("# "):
                    body_lines.append(line)

        body_text = "\n".join(body_lines).strip()
        body_snippet = body_text[:600]

        content_parts = []
        if questions:
            content_parts.append("Also asked as:\n" + "\n".join(f"- {q}" for q in questions))
        if body_snippet:
            content_parts.append(body_snippet)

        full_content = "\n\n".join(content_parts)
        records[f.stem] = {
            "id": f.stem,
            "title": title or f.stem,
            "content": full_content,
        }
    return records


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
    rerank_applied: bool = False
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
        # An explicitly injected token is used as-is (tests, short-lived scripts).
        # Otherwise ask the shared credential cache every call: get_access_token()
        # refreshes when the ~60 min access token expires. Caching the string here
        # made every search 401 an hour after pod start (search_failure escalations).
        if self._token:
            return self._token
        return get_access_token()

    def _execute_http(
        self,
        body: Dict[str, Any],
        token: str,
        sleep_sec: float,
        url: Optional[str] = None,
    ) -> tuple[Dict[str, Any], float]:
        """Execute a single HTTP request and return (response_dict, latency_ms)."""
        target_url = url or self.url
        req = urllib.request.Request(
            target_url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "x-goog-user-project": self.project,
                "Content-Type": "application/json",
            },
        )
        err = "request failed"
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

    def rank(
        self,
        query: str,
        records: List[Dict[str, Any]],
        top_n: int = 5,
        model: Optional[str] = None,
        sleep_sec: Optional[float] = None,
        token: Optional[str] = None,
    ) -> tuple[List[Dict[str, Any]], float]:
        """Call Discovery Engine Ranking API to rerank records given a query.

        Returns:
            (reranked_records, latency_ms)
        """
        tok = token or self.get_token()
        throttle = self.sleep_sec if sleep_sec is None else sleep_sec
        rank_url = f"{api_base(self.location)}projects/{self.project}/locations/{self.location}/rankingConfigs/default_ranking_config:rank"
        resolved_model = model or model_id("ranking_api_model")

        body = {
            "rankingConfig": f"projects/{self.project}/locations/{self.location}/rankingConfigs/default_ranking_config",
            "model": resolved_model,
            "query": query,
            "records": records,
            "topN": top_n,
        }

        data, latency_ms = self._execute_http(body, tok, throttle, url=rank_url)
        return data.get("records", []), latency_ms

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
        boost_spec: Optional[Dict[str, Any]] = None,
        intent_boost: Optional[float] = None,
        rerank: bool = False,
        rerank_model: Optional[str] = None,
        rerank_top_n: int = 5,
        rerank_margin: Optional[float] = None,
        blend_margin: Optional[float] = None,
        rich_records: Optional[Dict[str, Dict[str, str]]] = None,
        extra_specs: Optional[Dict[str, Any]] = None,
        sleep_sec: Optional[float] = None,
        token: Optional[str] = None,
    ) -> SearchResult:
        """Perform search with status/intent filtering, boost, and optional ranking fallback/reranking."""
        tok = token or self.get_token()
        throttle = self.sleep_sec if sleep_sec is None else sleep_sec

        statuses = self.default_allowed_statuses if allowed_statuses == "DEFAULT" else allowed_statuses
        q_text = clean_query(query) if clean else query

        # Boost spec construction:
        # If intent_boost is provided with intent_family, construct soft boost condition
        final_boost_spec = dict(boost_spec) if boost_spec else None
        if intent_family and (intent_boost is not None):
            intent_clause = {
                "condition": f'intent_family: ANY("{intent_family}")',
                "boost": float(intent_boost),
            }
            if final_boost_spec is None:
                final_boost_spec = {"conditionBoostSpecs": [intent_clause]}
            else:
                c_specs = list(final_boost_spec.get("conditionBoostSpecs", []))
                c_specs.append(intent_clause)
                final_boost_spec["conditionBoostSpecs"] = c_specs
            # Soft boost: do NOT apply intent_family as hard filter in build_filter
            filter_intent = None
        else:
            filter_intent = intent_family

        # 1. Build initial filter
        primary_filter = build_filter(
            allowed_statuses=statuses,
            intent_family=filter_intent,
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
        if final_boost_spec:
            body["boostSpec"] = final_boost_spec
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
        # Fallback triggers if an intent_family hard filter was applied AND results < fallback_min_results
        fallback_triggered = False
        if filter_intent and len(doc_ids) < fallback_min_results:
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

        # 4. Optional managed reranking via Discovery Engine Ranking API
        rerank_applied = False
        if rerank and doc_ids:
            records = []
            for r in raw_results:
                doc = r.get("document", {})
                d_id = doc.get("id")
                if not d_id:
                    continue
                if rich_records and d_id in rich_records:
                    rec = rich_records[d_id]
                    records.append({
                        "id": d_id,
                        "title": rec.get("title", d_id),
                        "content": rec.get("content", ""),
                    })
                else:
                    derived = doc.get("derivedStructData", {})
                    struct = doc.get("structData", {})
                    title = derived.get("title") or struct.get("title") or d_id
                    snippets = derived.get("snippets", [])
                    snippet_text = snippets[0].get("snippet", "") if snippets else ""
                    content = snippet_text or title
                    records.append({
                        "id": d_id,
                        "title": title,
                        "content": content,
                    })
            if records:
                try:
                    reranked_records, r_lat = self.rank(
                        query=q_text,
                        records=records,
                        top_n=rerank_top_n,
                        model=rerank_model,
                        sleep_sec=throttle,
                        token=tok,
                    )
                    latency_ms += r_lat
                    reranked_ids = [rec["id"] for rec in reranked_records if "id" in rec]
                    margin = blend_margin if blend_margin is not None else rerank_margin
                    if margin is not None and doc_ids:
                        original_top1 = doc_ids[0]
                        rerank_top1 = reranked_ids[0] if reranked_ids else original_top1
                        scores = {rec["id"]: rec.get("score", 0.0) for rec in reranked_records if "id" in rec}
                        best_score = scores.get(rerank_top1, 0.0)
                        orig_score = scores.get(original_top1, 0.0)
                        if (best_score - orig_score) > float(margin):
                            doc_ids = reranked_ids
                        else:
                            doc_ids = [original_top1] + [rid for rid in reranked_ids if rid != original_top1]
                    else:
                        doc_ids = reranked_ids
                    rerank_applied = True
                except Exception:
                    # In case reranking fails, keep un-reranked doc_ids
                    pass

        return SearchResult(
            document_ids=doc_ids,
            latency_ms=round(latency_ms, 2),
            total_size=total_size,
            filter_applied=applied_filter,
            fallback_triggered=fallback_triggered,
            rerank_applied=rerank_applied,
            raw_results=raw_results,
            summary=summary,
        )
