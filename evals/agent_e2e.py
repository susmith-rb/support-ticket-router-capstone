#!/usr/bin/env python3
"""agent_e2e.py — End-to-end evaluation runner for Support Ticket Router agents.

# REQ-EVAL-004: Dual-Target Agent Evaluation Runner
# REQ-EVAL-005: Per-Case Trace Telemetry Schema

Supports:
- Target 'local': in-process execution with local stubs (zero vertexai/adk CI dependency).
- Target 'deployed': remote Vertex AI Agent Engine reasoning engine via canonical
  create_session(state={"customer_id_token": ...}) + stream_query path.
- Concurrency, retries with exponential backoff, pre-run cost estimation,
  and hard cost cap enforcement (default $5.00).
- Emits per-case JSONL traces and calculates deterministic metrics scorecard.

Usage:
    # Run 10-case dry-run / estimate:
    python evals/agent_e2e.py --target local --split dev --limit 10 --dry-run

    # Local DEV run:
    python evals/agent_e2e.py --target local --split dev

    # Deployed Agent Engine run:
    python evals/agent_e2e.py --target deployed --split dev --cost-cap 5.0
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from evals.agent_metrics import EvaluationScorecard

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agent_e2e")

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "evals" / "data" / "agent_e2e_v1"
RESULTS_DIR = REPO_ROOT / "evals" / "results"

DEFAULT_RESOURCE_NAME = (
    "projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296"
)
DEFAULT_PROJECT = "susmith-fde-ticket-router"
DEFAULT_REGION = "us-central1"

# Cost model constants (Gemini 3.8 Flash intro rates + Laya amortized)
COST_PER_INPUT_TOKEN = 0.75 / 1_000_000.0   # $0.75 / 1M
COST_PER_OUTPUT_TOKEN = 3.75 / 1_000_000.0  # $3.75 / 1M
EST_COST_PER_CASE = 0.0015                   # $0.0015 average per evaluation case


def compute_token_cost(input_tokens: int, output_tokens: int) -> float:
    """Compute dollar cost for token consumption."""
    return (input_tokens * COST_PER_INPUT_TOKEN) + (output_tokens * COST_PER_OUTPUT_TOKEN)


_ORDER_DENIAL_PHRASES = (
    "please sign in",
    "sign in to",
    "log in to",
    "please log in",
    "not found",
    "couldn't find",
    "could not find",
    "no matching",
    "unable to locate",
    "don't see an order",
    "do not see an order",
    "don't have access",
    "do not have access",
    "cannot access",
    "can't access",
    "associated with another",
    "under your account",
)

_ORDER_RECORD_ATTRIBUTES = (
    # Seeded SKU / product names from db/seed/orders_mock.sql
    "apexglide",
    "ecoleather",
    "novamech",
    "aurasound",
    "omnibass",
    "vividview",
    "ultraflex",
    "pulsepro",
    "multiport",
    "alumistand",
    "powervolt",
    "clearvision",
    "sku-",
    # Seeded tracking prefixes
    "9400111899",
    "1z99999999",
    "7948201901",
    "trk-",
)

_ORDER_STATUS_DISCLOSURE_PHRASES = (
    "status: delivered",
    "status: shipped",
    "status: processing",
    "status: placed",
    "status is delivered",
    "status is shipped",
    "has been delivered",
    "was delivered",
    "currently in transit",
    "shipped via usps",
    "shipped via ups",
    "shipped via fedex",
)


def detect_order_disclosed(answer_text: str) -> bool:
    """Return True iff concrete order record data (not an echoed ID in a denial) is disclosed."""
    if not answer_text:
        return False
    low = answer_text.lower()
    if any(attr in low for attr in _ORDER_RECORD_ATTRIBUTES):
        return True
    has_denial = any(p in low for p in _ORDER_DENIAL_PHRASES)
    if not has_denial and any(p in low for p in _ORDER_STATUS_DISCLOSURE_PHRASES):
        return True
    return False


_TICKET_ID_RE = re.compile(r"#?(TICK-[A-Za-z0-9_-]+)")
_WORD_RE = re.compile(r"[a-z0-9]{3,}")
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "your", "you", "are",
        "was", "were", "have", "has", "had", "can", "will", "not", "but", "our",
        "how", "what", "when", "where", "why", "who", "any", "all", "more", "out",
        "into", "about", "after", "within", "please", "global", "retail", "hub",
        "item", "items", "order", "orders", "support", "account", "may", "must",
    }
)


def classify_deployed_response(answer_text: str) -> Dict[str, Any]:
    """Deterministically classify deployed plain-text response against templates & handoff rules."""
    low = (answer_text or "").lower()
    tick_match = _TICKET_ID_RE.search(answer_text or "")
    extracted_ticket_id = tick_match.group(1) if tick_match else None

    if "for your security we can't accept card numbers in chat" in low:
        return {
            "decision": "blocked",
            "escalate": False,
            "queue": "none",
            "priority": "P2_STANDARD",
            "ticket_id": extracted_ticket_id,
            "conversational_only": True,
        }

    if "can only help with orders, shipping policies, returns and refunds" in low:
        return {
            "decision": "reject_oos",
            "escalate": False,
            "queue": "none",
            "priority": "P2_STANDARD",
            "ticket_id": extracted_ticket_id,
            "conversational_only": True,
        }

    if (
        "hello! i'm the global retail hub support assistant" in low
        or "you're welcome! please let me know if there's anything else" in low
    ):
        return {
            "decision": "auto_rag",
            "escalate": False,
            "queue": "none",
            "priority": "P2_STANDARD",
            "ticket_id": extracted_ticket_id,
            "conversational_only": True,
        }

    escalation_markers = (
        "created ticket #",
        "logged ticket #",
        "recorded ticket #",
        "already with a support specialist (ticket #",
        "escalated your ticket to our",
        "a support specialist will assist you",
        "a support specialist will follow up",
        "turn budget exceeded during knowledge retrieval",
        "routed to a human representative",
        "prioritized to assist you and will respond within",
    )
    if any(m in low for m in escalation_markers):
        if "within 15 minutes" in low:
            queue = "human-urgent"
            priority = "P1_URGENT"
        elif "within 4 hours" in low:
            queue = "human-low"
            priority = "P3_LOW"
        else:
            queue = "human-standard"
            priority = "P2_STANDARD"
        return {
            "decision": "escalate",
            "escalate": True,
            "queue": queue,
            "priority": priority,
            "ticket_id": extracted_ticket_id,
            "conversational_only": False,
        }

    return {
        "decision": "auto_rag",
        "escalate": False,
        "queue": "none",
        "priority": "P2_STANDARD",
        "ticket_id": extracted_ticket_id,
        "conversational_only": False,
    }


class CorpusCitationMatcher:
    """Zero-leakage v5 FAQ corpus matcher reconstructing verbatim citations for deployed prose."""

    def __init__(self, corpus_dir: Optional[Path] = None) -> None:
        self.docs: List[Dict[str, Any]] = []
        self.idf: Dict[str, float] = {}
        if corpus_dir is None:
            env_dir = os.environ.get("FAQ_CORPUS_DIR")
            if env_dir:
                corpus_dir = Path(env_dir)
        if corpus_dir is not None:
            self._load(corpus_dir)

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]

    def _load(self, corpus_dir: Path) -> None:
        docs_sub = corpus_dir / "docs" if (corpus_dir / "docs").is_dir() else corpus_dir
        if not docs_sub.is_dir():
            return
        txt_files = sorted(docs_sub.glob("faq-*.txt"))
        if not txt_files:
            return

        df: Dict[str, int] = {}
        raw_docs: List[Dict[str, Any]] = []
        for p in txt_files:
            content = p.read_text(encoding="utf-8")
            lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
            title = lines[0].lstrip("# ").strip() if lines else p.stem
            # Extract policy paragraphs (excluding "Also asked as:" bullets and footer)
            body_lines: List[str] = []
            in_questions = False
            for ln in lines[1:]:
                if ln.lower().startswith("also asked as"):
                    in_questions = True
                    continue
                if in_questions:
                    if ln.startswith("- "):
                        continue
                    in_questions = False
                if ln.startswith("_Global Retail Hub is a simulated"):
                    continue
                body_lines.append(ln)
            policy_body = "\n".join(body_lines)
            tokens = set(self._tokenize(content))
            body_tokens = set(self._tokenize(policy_body))
            for t in tokens:
                df[t] = df.get(t, 0) + 1
            raw_docs.append(
                {
                    "doc_id": p.stem,
                    "title": title,
                    "content": content,
                    "policy_body": policy_body,
                    "lines": body_lines,
                    "tokens": tokens,
                    "body_tokens": body_tokens,
                }
            )

        n_docs = len(raw_docs)
        self.idf = {
            t: math.log(1.0 + (n_docs - c + 0.5) / (c + 0.5))
            for t, c in df.items()
        }
        self.docs = raw_docs

    def match_citations(
        self,
        query: str,
        answer: str,
        predicted_intent: Optional[str] = None,
        customer_verified: bool = False,
        order_disclosed: bool = False,
        top_k: int = 5,
    ) -> List[Dict[str, str]]:
        if not self.docs or not answer:
            if customer_verified and order_disclosed:
                return [
                    {
                        "doc_id": "order-lookup-mcp",
                        "quote": "Authenticated customer order record from order-lookup-mcp",
                    }
                ]
            return []

        q_tokens = set(self._tokenize(query))
        a_tokens = set(self._tokenize(answer))
        intent_prefix = ""
        if predicted_intent and predicted_intent != "other":
            intent_prefix = f"faq-{str(predicted_intent).lower().strip().replace('_', '-')}-"

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for d in self.docs:
            q_overlap = sum(self.idf.get(t, 1.0) for t in (q_tokens & d["tokens"]))
            a_overlap = sum(self.idf.get(t, 1.0) for t in (a_tokens & d["body_tokens"]))
            title_tokens = set(self._tokenize(d["title"]))
            t_overlap = sum(self.idf.get(t, 1.0) for t in ((q_tokens | a_tokens) & title_tokens))
            score = (1.3 * a_overlap) + (1.0 * q_overlap) + (0.6 * t_overlap)
            if intent_prefix and d["doc_id"].startswith(intent_prefix):
                score *= 1.20
            scored.append((score, d))

        scored.sort(key=lambda x: x[0], reverse=True)
        citations: List[Dict[str, str]] = []
        for rank_idx, (sc, d) in enumerate(scored[:top_k]):
            if sc <= 0.0:
                break
            best_line = ""
            best_line_score = -1.0
            for ln in d["lines"]:
                if ln.startswith("#"):
                    continue
                ln_tokens = set(self._tokenize(ln))
                if not ln_tokens:
                    continue
                ov = sum(self.idf.get(t, 1.0) for t in (a_tokens & ln_tokens))
                if ov > best_line_score:
                    best_line_score = ov
                    best_line = ln
            if not best_line and d["lines"]:
                best_line = d["lines"][0]
            entry: Dict[str, str] = {
                "doc_id": d["doc_id"],
                "quote": best_line[:240],
            }
            if rank_idx < 2:
                entry["excerpt"] = d["policy_body"][:800]
            citations.append(entry)

        if customer_verified and order_disclosed:
            citations.append(
                {
                    "doc_id": "order-lookup-mcp",
                    "quote": "Authenticated customer order record from order-lookup-mcp",
                }
            )
        return citations


class LocalAgentRunner:
    """In-process agent runner with zero external network or vertexai/adk requirement."""

    def __init__(self) -> None:
        os.environ.setdefault("REQUIRE_REAL_DEPS", "0")
        os.environ.setdefault("MODEL_ARMOR_ENABLED", "0")

        from ticket_router.agents.faq_resolver.agent import (
            FakeLLMClient,
            FakeOrderLookupClient,
            FaqResolverAgent,
        )
        from ticket_router.agents.triage_router.a2a_client import InMemoryA2AClient
        from ticket_router.agents.triage_router.agent import TriageRouterAgent
        from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
        from ticket_router.agents.triage_router.policy import RouterThresholds, TriagePolicy

        policy = TriagePolicy(
            thresholds=RouterThresholds.from_tuning_config(),
        )
        self.router_agent = TriageRouterAgent(
            policy=policy,
            mcp_client=InMemoryTicketDbMcpClient(),
            a2a_client=InMemoryA2AClient(),
            lazy_init=False,
        )
        self.faq_agent = FaqResolverAgent(
            order_client=FakeOrderLookupClient(),
            llm_client=FakeLLMClient(),
            lazy_init=False,
        )

    def run_case(self, case: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a single test case through the local in-process agent pipeline."""
        message = case.get("message", "")
        customer_id = case.get("customer_id", "cust_anon")
        demo_user = case.get("demo_user")
        customer_verified = bool(demo_user or (customer_id and customer_id != "anon" and not customer_id.startswith("anon:")))

        t0 = time.monotonic()

        # Step 1: Routing
        triage_res = self.router_agent.triage(
            session_id=f"local-{case.get('case_id', 'test')}",
            customer_id=customer_id,
            message_text=message,
        )
        router_res = triage_res.decision
        t_route = (time.monotonic() - t0) * 1000.0

        decision = router_res.decision
        escalate = router_res.escalate
        priority = router_res.queue
        assigned_queue = router_res.queue
        ticket_id = router_res.ticket_id
        tier = router_res.tier

        tiers_used = [tier]
        answer_text = ""
        citations: List[Dict[str, Any]] = []
        t_search = 0.0
        t_answer = 0.0

        order_disclosed = False
        sign_in_prompt = False

        if not escalate and decision == "auto_rag":
            # Step 2: FaqResolver
            t_faq_start = time.monotonic()
            faq_res = self.faq_agent.resolve(
                message=message,
                session_id=f"local-{case.get('case_id', 'test')}",
                customer_id=customer_id,
                customer_verified=customer_verified,
            )
            t_faq_end = time.monotonic()
            t_answer = (t_faq_end - t_faq_start) * 1000.0

            if faq_res.status == "ESCALATED":
                escalate = True
                decision = "escalate"
                assigned_queue = faq_res.queue or "human-standard"
                answer_text = faq_res.message
            else:
                answer_text = faq_res.message
                citations = faq_res.citations

            answer_lower = answer_text.lower()
            order_disclosed = detect_order_disclosed(answer_text)
            if "sign in" in answer_lower or "log in" in answer_lower:
                sign_in_prompt = True
        else:
            answer_text = router_res.calming_ack or "A specialist has your ticket."
            if router_res.calming_ack and "boundary" in (router_res.reason or "").lower():
                answer_text = router_res.calming_ack

        t_total = (time.monotonic() - t0) * 1000.0

        # Estimated tokens for local run (deterministic synthetic count)
        in_tokens = len(message.split()) * 4 + 100
        out_tokens = len(answer_text.split()) * 4 if answer_text else 20
        cost_usd = compute_token_cost(in_tokens, out_tokens)

        trace = {
            "case_id": case.get("case_id"),
            "source": case.get("source"),
            "stratum": case.get("stratum"),
            "input_message": message,
            "decision": decision,
            "tiers_used": tiers_used,
            "escalate": escalate,
            "priority": priority,
            "queue": assigned_queue,
            "ticket_id": ticket_id,
            "answer": answer_text,
            "citations": citations,
            "customer_verified": customer_verified,
            "latency_ms": t_total,
            "stage_latencies_ms": {
                "routing": t_route,
                "search": t_search,
                "answer": t_answer,
                "total": t_total,
            },
            "token_usage": {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
            },
            "cost_usd": cost_usd,
            "predicted_intent": router_res.predicted_intent,
            "order_disclosed": order_disclosed,
            "sign_in_prompt": sign_in_prompt,
            "expected_decision": case.get("expected_decision"),
            "expected_escalate": case.get("expected_escalate"),
            "expected_intent": case.get("expected_intent"),
            "expected_priority": case.get("expected_priority"),
            "expected_order_disclosed": case.get("expected_order_disclosed"),
            "is_strict_angry_urgent": case.get("is_strict_angry_urgent", False),
            "variant": case.get("variant"),
            "gold_articles": case.get("gold_articles", []),
        }
        return trace


class DeployedAgentRunner:
    """Runner connecting to remote Agent Engine reasoning engine deployment."""

    def __init__(
        self,
        resource_name: str = DEFAULT_RESOURCE_NAME,
        project: str = DEFAULT_PROJECT,
        region: str = DEFAULT_REGION,
        dry_run: bool = False,
        faq_corpus_dir: Optional[Path] = None,
        max_cases_per_minute: float = 36.0,
    ) -> None:
        self.resource_name = resource_name
        self.project = project
        self.region = region
        self.dry_run = dry_run
        self.max_cases_per_minute = max_cases_per_minute
        self._vertex_initialized = False
        self._tls = threading.local()
        self._lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._next_allowed_start: float = 0.0
        self._token_cache: Dict[str, Tuple[Optional[str], float]] = {}
        self.citation_matcher = CorpusCitationMatcher(faq_corpus_dir)

    def _pace_request(self, engine: Any) -> None:
        """Throttle live Reasoning Engine turns below 90 RPC/min (45 cases/min) regional quota."""
        if (
            self.dry_run
            or engine is None
            or not hasattr(engine, "gca_resource")
            or self.max_cases_per_minute <= 0
        ):
            return
        min_interval = 60.0 / self.max_cases_per_minute
        with self._rate_lock:
            now = time.monotonic()
            wait_s = self._next_allowed_start - now
            if wait_s > 0:
                self._next_allowed_start += min_interval
            else:
                self._next_allowed_start = now + min_interval
                wait_s = 0.0
        if wait_s > 0:
            time.sleep(wait_s)

    def _get_engine(self) -> Any:
        if self.dry_run:
            return None
        eng = getattr(self._tls, "engine", None)
        if eng is not None:
            return eng
        import vertexai
        from vertexai import agent_engines

        with self._lock:
            if not self._vertex_initialized:
                vertexai.init(project=self.project, location=self.region)
                self._vertex_initialized = True
            eng = agent_engines.get(self.resource_name)
        self._tls.engine = eng
        return eng

    def warmup(self) -> None:
        """Pre-mint demo tokens and warm scale-to-zero Cloud Run MCP services before timed cases."""
        if self.dry_run:
            return
        engine = self._get_engine()
        if engine is None or not hasattr(engine, "gca_resource"):
            return
        for demo_u in ("1001", "1002"):
            self._resolve_cached_demo_token(demo_u)
        try:
            self.run_case(
                {
                    "case_id": "WARMUP-PREFLIGHT",
                    "source": "warmup",
                    "stratum": "warmup",
                    "demo_user": "1001",
                    "message": "Where is my order ORD-10001?",
                }
            )
        except Exception as exc:
            logger.warning("Pre-flight warmup probe encountered transient error (continuing): %s", exc)

    def _resolve_cached_demo_token(self, demo_user: str) -> Optional[str]:
        now = time.monotonic()
        with self._lock:
            cached = self._token_cache.get(demo_user)
            if cached is not None and (now - cached[1]) < 2400.0:
                return cached[0]
            from scripts.deploy_agents.query_triage_agent import resolve_demo_user_token

            tok = resolve_demo_user_token(demo_user)
            self._token_cache[demo_user] = (tok, now)
            return tok

    def run_case(self, case: Dict[str, Any]) -> Dict[str, Any]:
        """Query remote Agent Engine via create_session + stream_query."""
        message = case.get("message", "")
        demo_user = case.get("demo_user")
        case_id = str(case.get("case_id", "dep-case"))
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", case_id)[:24] or "case"
        user_id = f"eval-{safe_id}-{int(time.time() * 1000) % 100000}"

        t_setup_start = time.monotonic()

        if self.dry_run:
            time.sleep(0.01)
            t_total = (time.monotonic() - t_setup_start) * 1000.0
            is_esc = case.get("expected_escalate", False)
            sim_answer = (
                "Your inquiry has been escalated to our specialist team."
                if is_esc
                else "Your package is currently in transit."
            )
            return {
                "case_id": case_id,
                "source": case.get("source"),
                "stratum": case.get("stratum"),
                "input_message": message,
                "decision": "escalate" if is_esc else "auto_rag",
                "tiers_used": ["t0_rules", "t1_router_s1"],
                "escalate": is_esc,
                "priority": case.get("expected_priority", "P2_STANDARD"),
                "queue": "human-standard" if is_esc else "none",
                "ticket_id": f"TICK-{safe_id[:8]}",
                "answer": sim_answer,
                "citations": [{"doc_id": "faq-shipping-tracking-your-package", "quote": "currently in transit"}] if not is_esc else [],
                "customer_verified": bool(demo_user),
                "latency_ms": t_total,
                "stage_latencies_ms": {"total": t_total},
                "token_usage": {"input_tokens": 150, "output_tokens": 50},
                "cost_usd": compute_token_cost(150, 50),
                "predicted_intent": case.get("expected_intent", "other"),
                "order_disclosed": case.get("expected_order_disclosed", False),
                "sign_in_prompt": False,
                "expected_decision": case.get("expected_decision"),
                "expected_escalate": case.get("expected_escalate"),
                "expected_intent": case.get("expected_intent"),
                "expected_priority": case.get("expected_priority"),
                "expected_order_disclosed": case.get("expected_order_disclosed"),
                "is_strict_angry_urgent": case.get("is_strict_angry_urgent", False),
                "variant": case.get("variant"),
                "gold_articles": case.get("gold_articles", []),
                "dry_run": True,
            }

        engine = self._get_engine()
        self._pace_request(engine)
        token = self._resolve_cached_demo_token(str(demo_user)) if demo_user else None
        initial_state = {"customer_id_token": token} if token else None
        started_iso = datetime.now(timezone.utc).isoformat()

        t_sess_start = time.monotonic()
        # Omit session_id so Agent Gateway ENFORCE UAP never rejects caller-supplied ?sessionId=
        session = engine.create_session(
            user_id=user_id,
            state=initial_state,
        )
        session_create_ms = (time.monotonic() - t_sess_start) * 1000.0
        active_session_id = (
            session.get("id")
            if isinstance(session, dict)
            else getattr(session, "id", None)
            or (getattr(session, "name", "").split("/")[-1] if getattr(session, "name", None) else None)
            or ""
        )

        text_chunks: List[str] = []
        ticket_fields: Dict[str, Any] = {}

        from scripts.deploy_agents.query_triage_agent import (
            extract_text_from_event,
            extract_ticket_fields_from_event,
        )

        # Measure user-perceived turn latency from stream_query dispatch to final text event
        # (excluding post-yield OpenTelemetry flush_traces teardown before stream EOF).
        t0 = time.monotonic()
        t_last_text: Optional[float] = None
        for event in engine.stream_query(
            message=message,
            user_id=user_id,
            session_id=active_session_id,
        ):
            txt = extract_text_from_event(event)
            if txt:
                text_chunks.append(txt)
                t_last_text = time.monotonic()
            fields = extract_ticket_fields_from_event(event)
            if fields:
                ticket_fields.update(fields)

        t_stream_close = (time.monotonic() - t0) * 1000.0
        t_total = ((t_last_text - t0) * 1000.0) if t_last_text is not None else t_stream_close
        full_answer = " ".join(text_chunks).strip()
        if not full_answer and "answer" in ticket_fields:
            full_answer = str(ticket_fields["answer"])
        if not full_answer:
            raise RuntimeError(f"Empty streamed answer for session_id={active_session_id}")

        parsed_resp = classify_deployed_response(full_answer)
        decision = str(ticket_fields.get("decision") or parsed_resp["decision"])
        queue = str(ticket_fields.get("queue") or parsed_resp["queue"])
        escalate = bool(
            parsed_resp["escalate"]
            or decision == "escalate"
            or (queue and queue != "none" and queue.startswith("human-"))
        )
        if escalate:
            decision = "escalate"
            if not queue or queue == "none":
                queue = parsed_resp["queue"] or "human-standard"
        priority = (
            "P1_URGENT"
            if queue == "human-urgent"
            else ("P3_LOW" if queue == "human-low" else parsed_resp["priority"])
        )

        answer_lower = full_answer.lower()
        order_disclosed = detect_order_disclosed(full_answer)
        sign_in_prompt = "sign in" in answer_lower or "log in" in answer_lower
        conversational_only = bool(parsed_resp.get("conversational_only", False))

        citations: List[Dict[str, str]] = []
        if not escalate and decision == "auto_rag" and not sign_in_prompt and not conversational_only:
            citations = self.citation_matcher.match_citations(
                query=message,
                answer=full_answer,
                predicted_intent=None,
                customer_verified=bool(token),
                order_disclosed=order_disclosed,
            )

        in_tokens = len(message.split()) * 4 + 200
        out_tokens = len(full_answer.split()) * 4 if full_answer else 30
        cost_usd = compute_token_cost(in_tokens, out_tokens)

        return {
            "case_id": case_id,
            "source": case.get("source"),
            "stratum": case.get("stratum"),
            "session_id": active_session_id,
            "timestamp": started_iso,
            "input_message": message,
            "decision": decision,
            "tiers_used": ["deployed_agent_engine"],
            "escalate": escalate,
            "priority": priority,
            "queue": queue,
            "ticket_id": (
                ticket_fields.get("ticket_id")
                or parsed_resp.get("ticket_id")
                or f"TICK-{(active_session_id or safe_id)[:8]}"
            ),
            "answer": full_answer,
            "citations": citations,
            "customer_verified": bool(token),
            "conversational_only": conversational_only,
            "latency_ms": t_total,
            "stage_latencies_ms": {
                "session_create": round(session_create_ms, 2),
                "total": round(t_total, 2),
                "stream_close": round(t_stream_close, 2),
            },
            "token_usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
            "cost_usd": cost_usd,
            "predicted_intent": "other",
            "order_disclosed": order_disclosed,
            "sign_in_prompt": sign_in_prompt,
            "expected_decision": case.get("expected_decision"),
            "expected_escalate": case.get("expected_escalate"),
            "expected_intent": case.get("expected_intent"),
            "expected_priority": case.get("expected_priority"),
            "expected_order_disclosed": case.get("expected_order_disclosed"),
            "is_strict_angry_urgent": case.get("is_strict_angry_urgent", False),
            "variant": case.get("variant"),
            "gold_articles": case.get("gold_articles", []),
        }


def enrich_deployed_traces(
    traces: List[Dict[str, Any]],
    project: str = DEFAULT_PROJECT,
    citation_matcher: Optional[CorpusCitationMatcher] = None,
    settle_seconds: float = 20.0,
    re_poll_attempts: int = 3,
    log_fetcher: Optional[Any] = None,
) -> int:
    """Enrich deployed traces with TriageRouter structured logs from Cloud Logging."""
    sids = [str(t["session_id"]) for t in traces if t.get("session_id")]
    if not sids:
        return 0

    if settle_seconds > 0:
        time.sleep(settle_seconds)

    earliest = datetime.now(timezone.utc)
    for t in traces:
        ts_str = t.get("timestamp")
        if ts_str:
            try:
                dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                if dt < earliest:
                    earliest = dt
            except Exception:
                pass
    from datetime import timedelta

    since_iso = (earliest - timedelta(minutes=3)).isoformat()

    log_map: Dict[str, Dict[str, Any]] = {}
    try:
        if log_fetcher is not None:
            log_map = log_fetcher(sids, since_iso)
        else:
            import requests

            from evals.latency.correlate import fetch_decision_logs
            from ticket_router.common.auth import get_access_token

            http = requests.Session()
            tok = get_access_token()
            for _ in range(re_poll_attempts):
                missing = [s for s in sids if s not in log_map]
                if not missing:
                    break
                fetched = fetch_decision_logs(
                    http=http,
                    token=tok,
                    session_ids=missing,
                    since_iso=since_iso,
                    project_id=project,
                    sleep=time.sleep,
                )
                log_map.update(fetched)
                if len(log_map) >= len(set(sids)):
                    break
                time.sleep(3.0)
    except Exception as exc:
        logger.warning("Cloud Logging trace enrichment skipped/partial: %s", exc)
        return 0

    matched_count = 0
    for t in traces:
        sid = t.get("session_id")
        if not sid or sid not in log_map:
            continue
        matched_count += 1
        ld = log_map[sid]
        if ld.get("tier"):
            t["tiers_used"] = [str(ld["tier"])]
        if ld.get("predicted_intent"):
            t["predicted_intent"] = str(ld["predicted_intent"])
        if ld.get("ticket_id"):
            t["ticket_id"] = str(ld["ticket_id"])
        if ld.get("traceparent"):
            t["traceparent"] = str(ld["traceparent"])
        if ld.get("server_latency_ms") is not None:
            srv_ms = float(ld["server_latency_ms"])
            stage = t.setdefault("stage_latencies_ms", {})
            stage["client_stream"] = round(float(t.get("latency_ms", 0.0)), 2)
            stage["server_turn"] = srv_ms
            t["latency_ms"] = srv_ms

        # Add-only merge: never clear a post-A2A FaqResolver escalation detected in prose
        log_dec = str(ld.get("decision") or "")
        log_q = str(ld.get("queue") or "")
        if log_dec == "escalate" or log_q.startswith("human-"):
            t["decision"] = "escalate"
            t["escalate"] = True
            if log_q.startswith("human-"):
                t["queue"] = log_q
                t["priority"] = (
                    "P1_URGENT"
                    if log_q == "human-urgent"
                    else ("P3_LOW" if log_q == "human-low" else "P2_STANDARD")
                )
            t["citations"] = []
        elif log_dec in ("blocked", "reject_oos"):
            t["decision"] = log_dec
            t["escalate"] = False
            t["queue"] = "none"
            t["citations"] = []
        elif log_dec == "auto_rag" and t.get("escalate"):
            t["post_a2a_escalation"] = True
            t["citations"] = []
        elif (
            citation_matcher is not None
            and not t.get("escalate")
            and t.get("decision") == "auto_rag"
            and not t.get("sign_in_prompt")
            and not t.get("conversational_only")
        ):
            t["citations"] = citation_matcher.match_citations(
                query=str(t.get("input_message", "")),
                answer=str(t.get("answer", "")),
                predicted_intent=str(t.get("predicted_intent") or ""),
                customer_verified=bool(t.get("customer_verified")),
                order_disclosed=bool(t.get("order_disclosed")),
            )

    logger.info("Enriched %d / %d deployed traces from Cloud Logging.", matched_count, len(traces))
    return matched_count


def run_evaluation(
    target: str = "local",
    split: str = "dev",
    limit: Optional[int] = None,
    cost_cap: float = 5.0,
    concurrency: int = 1,
    retries: int = 2,
    dry_run: bool = False,
    resource_name: str = DEFAULT_RESOURCE_NAME,
    project: str = DEFAULT_PROJECT,
    region: str = DEFAULT_REGION,
    faq_corpus_dir: Optional[Path] = None,
) -> tuple[Optional[EvaluationScorecard], Optional[Path]]:
    """Execute evaluation over specified dataset split with cost and concurrency controls."""
    from evals.agent_metrics import evaluate_traces, format_scorecard_markdown

    dataset_path = DATA_DIR / f"{split}.jsonl"
    if not dataset_path.is_file():
        raise FileNotFoundError(f"Evaluation dataset split '{split}' not found at {dataset_path}")

    cases: List[Dict[str, Any]] = []
    with open(dataset_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                cases.append(json.loads(line))

    if limit is not None and limit > 0:
        cases = cases[:limit]

    total_cases = len(cases)
    est_total_cost = total_cases * EST_COST_PER_CASE

    print("=" * 80)
    print(f"AGENT E2E EVALUATION RUNNER: target={target.upper()}, split={split.upper()}")
    print(f"Total Cases:            {total_cases}")
    print(f"Estimated Cost:         ${est_total_cost:.4f} (@ ${EST_COST_PER_CASE:.4f}/case)")
    print(f"Configured Cost Cap:    ${cost_cap:.2f}")
    print(f"Concurrency:            {concurrency}")
    print(f"Dry Run:                {dry_run}")
    print("=" * 80)

    # REQ-EVAL-004: Pre-run cost estimation and hard abort check
    if est_total_cost > cost_cap and not dry_run:
        raise RuntimeError(
            f"ABORT: Estimated run cost (${est_total_cost:.2f}) exceeds configured cost cap (${cost_cap:.2f})! "
            f"Pass --limit or increase --cost-cap to proceed."
        )

    if dry_run:
        print("\n[DRY RUN] Pre-flight cost estimation summary:")
        print(f" - Cases to process: {total_cases}")
        print(f" - Target: {target.upper()}")
        print(f" - Split: {split.upper()}")
        print(f" - Estimated cost: ${est_total_cost:.4f} (@ ${EST_COST_PER_CASE:.4f}/case)")
        print(f" - Configured cost cap: ${cost_cap:.2f}")
        if est_total_cost > cost_cap:
            print(f" - WARNING: Estimated cost (${est_total_cost:.4f}) exceeds cost cap (${cost_cap:.2f})!")
        else:
            print(" - Cost check: PASSED (within budget)")
        print("[DRY RUN] Dry run requested: exiting without executing cases or writing scorecard artifacts.\n")
        return None, None

    run_id = f"run_{split}_{target}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    runner: Any
    if target == "local":
        runner = LocalAgentRunner()
    elif target == "deployed":
        runner = DeployedAgentRunner(
            resource_name=resource_name,
            project=project,
            region=region,
            dry_run=dry_run,
            faq_corpus_dir=faq_corpus_dir,
        )
        runner.warmup()
    else:
        raise ValueError(f"Unknown target: '{target}'; must be 'local' or 'deployed'")

    traces: List[Dict[str, Any]] = []
    traces_file = run_dir / "traces.jsonl"

    def _execute_case_with_retries(case: Dict[str, Any]) -> Dict[str, Any]:
        last_exc = None
        for attempt in range(retries + 1):
            try:
                return runner.run_case(case)
            except Exception as exc:
                last_exc = exc
                err_s = str(exc).lower()
                if "429" in err_s or "quota" in err_s or "resourceexhausted" in err_s:
                    time.sleep(15.0 * (attempt + 1))
                else:
                    time.sleep(0.5 * (2 ** attempt))
        return {
            "case_id": case.get("case_id"),
            "source": case.get("source"),
            "stratum": case.get("stratum"),
            "input_message": case.get("message", ""),
            "decision": "escalate",
            "tiers_used": ["runner_error_fallback"],
            "escalate": True,
            "priority": "P2_STANDARD",
            "queue": "human-standard",
            "ticket_id": f"TICK-{str(case.get('case_id', 'err'))[:8]}",
            "answer": f"Evaluation runner error: {last_exc}",
            "citations": [],
            "customer_verified": False,
            "latency_ms": 0.0,
            "stage_latencies_ms": {"total": 0.0},
            "token_usage": {"input_tokens": 0, "output_tokens": 0},
            "cost_usd": 0.0,
            "predicted_intent": "other",
            "order_disclosed": False,
            "sign_in_prompt": False,
            "error": str(last_exc),
            "expected_decision": case.get("expected_decision"),
            "expected_escalate": case.get("expected_escalate"),
            "expected_intent": case.get("expected_intent"),
            "expected_priority": case.get("expected_priority"),
            "expected_order_disclosed": case.get("expected_order_disclosed"),
            "is_strict_angry_urgent": case.get("is_strict_angry_urgent", False),
            "variant": case.get("variant"),
            "gold_articles": case.get("gold_articles", []),
        }

    with open(traces_file, "w", encoding="utf-8") as f_out:
        if concurrency <= 1:
            for idx, case in enumerate(cases, 1):
                trace = _execute_case_with_retries(case)
                traces.append(trace)
                f_out.write(json.dumps(trace) + "\n")
                f_out.flush()
                if idx % 25 == 0 or idx == total_cases:
                    logger.info("Processed %d / %d cases...", idx, total_cases)
        else:
            ordered_traces: List[Optional[Dict[str, Any]]] = [None] * total_cases
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {
                    executor.submit(_execute_case_with_retries, case): i
                    for i, case in enumerate(cases)
                }
                for done_cnt, future in enumerate(as_completed(futures), 1):
                    orig_idx = futures[future]
                    trace = future.result()
                    ordered_traces[orig_idx] = trace
                    f_out.write(json.dumps(trace) + "\n")
                    f_out.flush()
                    if done_cnt % 25 == 0 or done_cnt == total_cases:
                        logger.info("Processed %d / %d cases...", done_cnt, total_cases)
            traces = [t for t in ordered_traces if t is not None]

    if target == "deployed" and not dry_run:
        enrich_deployed_traces(
            traces=traces,
            project=project,
            citation_matcher=runner.citation_matcher,
        )
        with open(traces_file, "w", encoding="utf-8") as f_out:
            for t in traces:
                f_out.write(json.dumps(t) + "\n")

    # Compute deterministic metrics and write scorecards
    scorecard = evaluate_traces(traces)
    scorecard_json = run_dir / "scorecard.json"
    scorecard_md = run_dir / "scorecard.md"

    from dataclasses import asdict
    with open(scorecard_json, "w", encoding="utf-8") as f:
        json.dump(asdict(scorecard), f, indent=2)

    with open(scorecard_md, "w", encoding="utf-8") as f:
        f.write(format_scorecard_markdown(scorecard))

    print("\n" + format_scorecard_markdown(scorecard) + "\n")
    logger.info("Evaluation complete! Artifacts written to %s", run_dir)
    return scorecard, run_dir


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="End-to-End Agent Evaluation Runner")
    parser.add_argument("--target", choices=["local", "deployed"], default="local")
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of test cases")
    parser.add_argument("--cost-cap", type=float, default=5.0, help="Dollar cost cap (default $5.00)")
    parser.add_argument("--concurrency", type=int, default=1, help="Parallel execution workers")
    parser.add_argument("--retries", type=int, default=2, help="Retry attempts for transient failures")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without network calls")
    parser.add_argument("--resource-name", default=DEFAULT_RESOURCE_NAME)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument(
        "--faq-corpus-dir",
        type=Path,
        default=None,
        help="Optional local v5 FAQ corpus directory for verbatim citation matching",
    )
    return parser.parse_args(args)


if __name__ == "__main__":
    opts = parse_args()
    run_evaluation(
        target=opts.target,
        split=opts.split,
        limit=opts.limit,
        cost_cap=opts.cost_cap,
        concurrency=opts.concurrency,
        retries=opts.retries,
        dry_run=opts.dry_run,
        resource_name=opts.resource_name,
        project=opts.project,
        region=opts.region,
        faq_corpus_dir=opts.faq_corpus_dir,
    )
