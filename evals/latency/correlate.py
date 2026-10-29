"""Correlation engine for Cloud Logging decision logs and Cloud Trace spans.

# REQ-EVAL-011: Live Per-Path Latency Benchmark with Trace-Level Step Attribution
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

DEFAULT_PROJECT_ID = "susmith-fde-ticket-router"


def parse_rfc3339_ms(ts_str: str) -> float:
    """Parse RFC3339 timestamp with nanosecond precision into Unix epoch milliseconds."""
    clean = ts_str.rstrip("Z")
    if "." in clean:
        base, frac = clean.split(".", 1)
        dt = datetime.fromisoformat(base).replace(tzinfo=timezone.utc)
        sec = dt.timestamp() + float("0." + frac)
        return sec * 1000.0
    dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def parse_decision_log(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a TriageRouter structured decision log from Cloud Logging entry.

    Accepts entries with embedded JSON inside textPayload or direct jsonPayload.
    Filters strictly for logger == "triage_router".
    """
    raw_payload = entry.get("jsonPayload")
    if not isinstance(raw_payload, dict):
        text = entry.get("textPayload") or ""
        # Find first '{' and matching last '}'
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                raw_payload = json.loads(text[start : end + 1])
            except Exception:
                return None
        else:
            return None

    if not isinstance(raw_payload, dict):
        return None

    if raw_payload.get("logger") != "triage_router":
        return None

    session_id = raw_payload.get("session_id")
    if not session_id:
        return None

    traceparent = str(raw_payload.get("traceparent") or "")
    trace_id: str | None = None
    if traceparent:
        parts = traceparent.split("-")
        if len(parts) >= 2 and parts[1]:
            trace_id = parts[1]

    raw_lat = raw_payload.get("latency_ms")
    server_lat = float(raw_lat) if raw_lat is not None else None

    return {
        "session_id": str(session_id),
        "traceparent": traceparent,
        "trace_id": trace_id,
        "tier": raw_payload.get("tier"),
        "decision": raw_payload.get("decision"),
        "predicted_intent": raw_payload.get("predicted_intent"),
        "server_latency_ms": round(server_lat, 2) if server_lat is not None else None,
        "ticket_id": raw_payload.get("ticket_id"),
        "queue": raw_payload.get("queue"),
        "degraded": bool(raw_payload.get("degraded", False)),
        "timestamp": raw_payload.get("timestamp"),
    }


def fetch_decision_logs(
    http: Any,
    token: str,
    session_ids: Sequence[str],
    since_iso: str,
    project_id: str = DEFAULT_PROJECT_ID,
    sleep: Callable[[float], None] = lambda _: None,
) -> dict[str, dict[str, Any]]:
    """Fetch structured decision logs from Cloud Logging for a list of session IDs."""
    unique_sids = sorted({s for s in session_ids if s})
    if not unique_sids:
        return {}

    url = "https://logging.googleapis.com/v2/entries:list"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    results: dict[str, dict[str, Any]] = {}
    chunk_size = 20

    for i in range(0, len(unique_sids), chunk_size):
        chunk = unique_sids[i : i + chunk_size]
        sids_clause = " OR ".join(f'"{s}"' for s in chunk)
        filter_str = (
            'resource.type="aiplatform.googleapis.com/ReasoningEngine" '
            f'AND timestamp>="{since_iso}" '
            f"AND ({sids_clause})"
        )

        page_token: str | None = None
        while True:
            body: dict[str, Any] = {
                "resourceNames": [f"projects/{project_id}"],
                "filter": filter_str,
                "pageSize": 1000,
                "orderBy": "timestamp asc",
            }
            if page_token:
                body["pageToken"] = page_token

            # Retry with exponential backoff on 429
            resp = None
            delay = 1.0
            for _ in range(5):
                resp = http.post(url, headers=headers, json=body, timeout=30.0)
                status = resp.status_code
                if status == 429:
                    sleep(delay)
                    delay *= 2.0
                    continue
                break

            if resp is None or resp.status_code != 200:
                break

            data = resp.json()
            entries = data.get("entries") or []
            for entry in entries:
                parsed = parse_decision_log(entry)
                if parsed:
                    results[parsed["session_id"]] = parsed

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    return results


def fetch_trace(
    http: Any,
    token: str,
    trace_id: str,
    project_id: str = DEFAULT_PROJECT_ID,
    sleep: Callable[[float], None] = lambda _: None,
    max_retries: int = 6,
) -> dict[str, Any] | None:
    """Fetch Cloud Trace details by trace ID with exponential backoff on 429/5xx."""
    url = f"https://cloudtrace.googleapis.com/v1/projects/{project_id}/traces/{trace_id}"
    headers = {
        "Authorization": f"Bearer {token}",
    }

    delay = 0.5
    for attempt in range(max_retries):
        resp = http.get(url, headers=headers, timeout=30.0)
        status = resp.status_code

        if status == 200:
            return resp.json()
        if status in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
            sleep(delay)
            delay *= 2.0
            continue
        break

    return None


def _find_span(parsed_spans: Sequence[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for ps in parsed_spans:
        if ps["name"] == name:
            return ps
    return None


def _find_all_spans(parsed_spans: Sequence[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [ps for ps in parsed_spans if ps["name"] == name]


def aggregate_spans(spans: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate and attribute step durations from deduplicated Cloud Trace spans."""
    # Deduplicate spans on (name, startTime)
    deduped: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()

    for s in spans:
        name = str(s.get("name") or "")
        start_ts = str(s.get("startTime") or "")
        key = (name, start_ts)
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(s)

    # Index spans with parsed times
    parsed_spans: list[dict[str, Any]] = []
    for s in deduped:
        name = str(s.get("name") or "")
        st_str = s.get("startTime")
        et_str = s.get("endTime")
        if not st_str or not et_str:
            continue
        try:
            st = parse_rfc3339_ms(st_str)
            et = parse_rfc3339_ms(et_str)
            dur = max(0.0, et - st)
            parsed_spans.append(
                {
                    "name": name,
                    "start": st,
                    "end": et,
                    "dur": dur,
                    "labels": s.get("labels") or {},
                }
            )
        except Exception:
            continue

    turn_span = _find_span(parsed_spans, "triage_router_turn")
    res_cust_span = _find_span(parsed_spans, "mcp.ticket_db.resolve_customer")
    upsert_span = _find_span(parsed_spans, "mcp.ticket_db.upsert_ticket")
    handoff_span = _find_span(parsed_spans, "a2a.handoff.faq_resolver")
    faq_resolve_span = _find_span(parsed_spans, "faq_resolver.resolve")
    search_span = _find_span(parsed_spans, "faq_resolver.search")
    rerank_span = _find_span(parsed_spans, "faq_resolver.rerank")
    gen_answer_spans = _find_all_spans(parsed_spans, "faq_resolver.generate_answer")

    list_orders_span = _find_span(parsed_spans, "faq_resolver.list_orders") or _find_span(parsed_spans, "mcp.order_lookup.list_orders")
    get_order_span = _find_span(parsed_spans, "mcp.order_lookup.get_order")

    model_armor_spans = [ps for ps in parsed_spans if "model_armor" in ps["name"].lower()]

    # Routing math: upsert_ticket.start - turn.start - resolve_customer
    routing_ms: float | None = None
    if turn_span and upsert_span:
        t_start = turn_span["start"]
        u_start = upsert_span["start"]
        res_time = 0.0
        if res_cust_span:
            r_start = res_cust_span["start"]
            if t_start <= r_start < u_start:
                res_time = res_cust_span["dur"]
        routing_ms = max(0.0, (u_start - t_start) - res_time)

    # A2A overhead math: a2a_handoff - faq_resolve
    a2a_overhead_ms: float | None = None
    if handoff_span and faq_resolve_span:
        a2a_overhead_ms = max(0.0, handoff_span["dur"] - faq_resolve_span["dur"])

    gen_answer_ms: float | None = None
    if gen_answer_spans:
        gen_answer_ms = sum(s["dur"] for s in gen_answer_spans)

    model_armor_ms: float | None = None
    if model_armor_spans:
        model_armor_ms = sum(s["dur"] for s in model_armor_spans)

    return {
        "turn_ms": round(turn_span["dur"], 2) if turn_span else None,
        "resolve_customer_ms": round(res_cust_span["dur"], 2) if res_cust_span else None,
        "upsert_ticket_ms": round(upsert_span["dur"], 2) if upsert_span else None,
        "routing_ms": round(routing_ms, 2) if routing_ms is not None else None,
        "a2a_handoff_ms": round(handoff_span["dur"], 2) if handoff_span else None,
        "faq_resolve_ms": round(faq_resolve_span["dur"], 2) if faq_resolve_span else None,
        "a2a_overhead_ms": round(a2a_overhead_ms, 2) if a2a_overhead_ms is not None else None,
        "list_orders_ms": round(list_orders_span["dur"], 2) if list_orders_span else None,
        "get_order_ms": round(get_order_span["dur"], 2) if get_order_span else None,
        "search_ms": round(search_span["dur"], 2) if search_span else None,
        "rerank_ms": round(rerank_span["dur"], 2) if rerank_span else None,
        "generate_answer_ms": round(gen_answer_ms, 2) if gen_answer_ms is not None else None,
        "generate_answer_count": len(gen_answer_spans),
        "model_armor_ms": round(model_armor_ms, 2) if model_armor_ms is not None else None,
    }


def correlate_records(
    records: Sequence[dict[str, Any]],
    token: str,
    http: Any,
    project_id: str = DEFAULT_PROJECT_ID,
    since_iso: str | None = None,
    settle_seconds: float = 30.0,
    re_poll_attempts: int = 3,
    sleep: Callable[[float], None] = lambda _: None,
) -> list[dict[str, Any]]:
    """Enrich benchmark request records with Cloud Logging decisions and Cloud Trace spans."""
    enriched: list[dict[str, Any]] = [dict(r) for r in records]

    if settle_seconds > 0:
        sleep(settle_seconds)

    if since_iso is None:
        # Determine earliest timestamp minus 2 minutes
        earliest = datetime.now(timezone.utc)
        for r in enriched:
            ts_str = r.get("timestamp")
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if dt < earliest:
                        earliest = dt
                except Exception:
                    pass
        since_iso = (earliest - timedelta(minutes=2)).isoformat()

    # Step 1: Poll logs for missing trace information
    all_sids = [r["session_id"] for r in enriched if r.get("session_id")]
    log_map: dict[str, dict[str, Any]] = {}

    for _ in range(re_poll_attempts):
        missing_sids = [
            sid for sid in all_sids
            if sid not in log_map
        ]
        if not missing_sids:
            break
        fetched = fetch_decision_logs(http, token, missing_sids, since_iso, project_id, sleep=sleep)
        log_map.update(fetched)
        if len(log_map) == len(all_sids):
            break
        sleep(2.0)

    # Step 2: Attach log data to records
    for r in enriched:
        sid = r.get("session_id")
        log_data = log_map.get(sid) if sid else None
        if log_data:
            r["traceparent"] = log_data.get("traceparent")
            r["trace_id"] = log_data.get("trace_id")
            r["tier"] = log_data.get("tier")
            r["decision"] = log_data.get("decision")
            r["predicted_intent"] = log_data.get("predicted_intent")
            r["server_latency_ms"] = log_data.get("server_latency_ms")
            r["ticket_id"] = log_data.get("ticket_id")
            r["queue"] = log_data.get("queue")
            r["degraded"] = log_data.get("degraded")
        else:
            for k in (
                "traceparent", "trace_id", "tier", "decision",
                "predicted_intent", "server_latency_ms", "ticket_id", "queue", "degraded"
            ):
                if k not in r:
                    r[k] = None

    # Step 3: Fetch traces and aggregate spans
    trace_map: dict[str, dict[str, Any]] = {}
    for r in enriched:
        tid = r.get("trace_id")
        if tid and tid not in trace_map:
            t_json = fetch_trace(http, token, tid, project_id, sleep=sleep)
            if t_json and "spans" in t_json:
                trace_map[tid] = aggregate_spans(t_json["spans"])

    # Step 4: Merge span fields and compute overhead
    for r in enriched:
        tid = r.get("trace_id")
        span_metrics = trace_map.get(tid) if tid else None
        if span_metrics:
            r.update(span_metrics)
        else:
            for k in (
                "turn_ms", "resolve_customer_ms", "upsert_ticket_ms",
                "routing_ms", "a2a_handoff_ms", "faq_resolve_ms",
                "a2a_overhead_ms", "list_orders_ms", "get_order_ms",
                "search_ms", "rerank_ms", "generate_answer_ms",
                "generate_answer_count", "model_armor_ms"
            ):
                if k not in r:
                    r[k] = None

        # Compute agent_engine_overhead_ms = client_total_ms - turn_ms
        c_tot = r.get("client_total_ms")
        t_ms = r.get("turn_ms")
        if c_tot is not None and t_ms is not None:
            r["agent_engine_overhead_ms"] = round(c_tot - t_ms, 2)
        else:
            r["agent_engine_overhead_ms"] = None

    return enriched
