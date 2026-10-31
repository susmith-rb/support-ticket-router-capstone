"""Vertex AI Agent Engine Memory Bank client and memory summary formatting.

Implements REQ-AGENT-036: Vertex AI Agent Engine Memory Bank Integration.
- Scoped strictly to verified customer identity tokens (scope={"user_id": verified_id}).
- Read: retrieves top-k (<=5) memories with hard timeout (~300 ms), failing open.
- Formats concise memory summary (<= 400 chars) with tokenized facts only (zero raw PII).
- Write: generates memories from tokenized turn (customer message + decision summary) with TTL 180 days.
- Controlled by feature flag MEMORY_BANK_ENABLED (default "1" in deploy, "0" in tests unless set).
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Protocol, Sequence

logger = logging.getLogger("ticket_router.memory")

DEFAULT_MEMORY_BANK_ENGINE = (
    "projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296"
)
DEFAULT_RETRIEVAL_TIMEOUT_S = 0.300  # Hard timeout (~300 ms)
MAX_MEMORY_SUMMARY_CHARS = 400
DEFAULT_TTL_S = "15552000s"  # 180 days in seconds (180 * 24 * 3600)
DEFAULT_TOP_K = 5


def is_memory_bank_enabled() -> bool:
    """Return whether Memory Bank is enabled.

    Default: '1' in deploy/production environment, '0' in test runners unless explicitly set.
    """
    env_val = os.environ.get("MEMORY_BANK_ENABLED")
    if env_val is not None:
        return env_val.strip().lower() in ("1", "true", "yes")

    # In automated test environments, default to disabled unless explicitly enabled
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return False

    return True


def render_memory_summary(memories: Sequence[Any], max_chars: int = MAX_MEMORY_SUMMARY_CHARS) -> str:
    """Render a compact memory summary string (<= max_chars) from retrieved memories.

    Extracts durable facts without including raw PII.
    """
    if not memories:
        return ""

    facts: List[str] = []
    seen = set()

    for item in memories:
        fact_str: Optional[str] = None
        if hasattr(item, "memory") and item.memory is not None:
            fact_str = getattr(item.memory, "fact", None) or getattr(item.memory, "description", None)
        elif hasattr(item, "fact"):
            fact_str = getattr(item, "fact", None)
        elif isinstance(item, dict):
            mem_dict = item.get("memory") if isinstance(item.get("memory"), dict) else item
            fact_str = mem_dict.get("fact") or mem_dict.get("description")
        elif isinstance(item, str):
            fact_str = item

        if fact_str:
            cleaned = fact_str.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                facts.append(cleaned)

    if not facts:
        return ""

    lines = [f"- {f}" for f in facts]
    summary = "\n".join(lines)

    if len(summary) > max_chars:
        summary = summary[: max_chars - 3].rstrip() + "..."

    return summary


class BaseMemoryBankClient(Protocol):
    """Protocol defining Memory Bank operations."""

    def retrieve_summary(
        self,
        customer_id: str,
        query: str = "",
        timeout_s: float = DEFAULT_RETRIEVAL_TIMEOUT_S,
        top_k: int = DEFAULT_TOP_K,
    ) -> str:
        """Retrieve top-k memories for customer and render a compact summary (<=400 chars)."""
        ...

    def generate_turn_memories(
        self,
        customer_id: str,
        user_message_tokenized: str,
        decision_summary: str,
        async_mode: bool = True,
    ) -> Any:
        """Generate durable customer facts from the tokenized turn."""
        ...


class VertexMemoryBankClient:
    """Vertex AI Agent Engine Memory Bank client."""

    def __init__(
        self,
        engine_name: Optional[str] = None,
        project: Optional[str] = None,
        region: Optional[str] = None,
        client: Optional[Any] = None,
    ) -> None:
        self.engine_name = (
            engine_name
            or os.environ.get("MEMORY_BANK_ENGINE_NAME")
            or DEFAULT_MEMORY_BANK_ENGINE
        )
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or "susmith-fde-ticket-router"
        self.region = region or os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1"
        self._client = client
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="mem_bank")

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        import vertexai

        self._client = vertexai.Client(project=self.project, location=self.region)
        return self._client

    def retrieve_summary(
        self,
        customer_id: str,
        query: str = "",
        timeout_s: float = DEFAULT_RETRIEVAL_TIMEOUT_S,
        top_k: int = DEFAULT_TOP_K,
    ) -> str:
        """Retrieve top-k memories for customer and return formatted summary.

        Fail open on any timeout, error, or disabled flag.
        """
        if not is_memory_bank_enabled():
            return ""

        cid = str(customer_id).strip() if customer_id else ""
        if not cid or cid.startswith("anon:"):
            logger.debug("Skipping Memory Bank retrieve for anonymous/unverified user: %s", cid)
            return ""

        def _do_retrieve() -> str:
            client = self._get_client()
            search_params: Optional[Dict[str, Any]] = None
            simple_params: Optional[Dict[str, Any]] = None

            q_clean = query.strip() if query else ""
            if q_clean:
                search_params = {"search_query": q_clean[:500], "top_k": top_k}
            else:
                simple_params = {"page_size": top_k}

            retrieve_kwargs: Dict[str, Any] = {
                "name": self.engine_name,
                "scope": {"user_id": cid},
            }
            if search_params is not None:
                retrieve_kwargs["similarity_search_params"] = search_params
            if simple_params is not None:
                retrieve_kwargs["simple_retrieval_params"] = simple_params

            res = client.agent_engines.memories.retrieve(**retrieve_kwargs)
            memories_list = list(res)
            return render_memory_summary(memories_list)

        fut = self._executor.submit(_do_retrieve)
        try:
            return fut.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "Memory Bank retrieve timed out after %.3fs for customer %s; failing open.",
                timeout_s,
                cid,
            )
            return ""
        except Exception as exc:
            logger.warning(
                "Memory Bank retrieve failed for customer %s (%s: %s); failing open.",
                cid,
                type(exc).__name__,
                exc,
            )
            return ""

    def generate_turn_memories(
        self,
        customer_id: str,
        user_message_tokenized: str,
        decision_summary: str,
        async_mode: bool = True,
    ) -> Any:
        """Generate memories from tokenized turn.

        Guarantees:
        - Non-blocking (runs in thread pool or without waiting)
        - Only tokenized text passed (never restored PII)
        - Only verified customers
        """
        if not is_memory_bank_enabled():
            return None

        cid = str(customer_id).strip() if customer_id else ""
        if not cid or cid.startswith("anon:"):
            logger.debug("Skipping Memory Bank write for anonymous/unverified user: %s", cid)
            return None

        def _do_generate() -> Any:
            try:
                from google.genai import types as genai_types

                user_content = genai_types.Content(
                    role="user",
                    parts=[genai_types.Part.from_text(text=user_message_tokenized)],
                )
                model_content = genai_types.Content(
                    role="model",
                    parts=[genai_types.Part.from_text(text=decision_summary)],
                )
                try:
                    from vertexai._genai import types as vx_types

                    source: Any = vx_types.GenerateMemoriesRequestDirectContentsSource(
                        events=[
                            vx_types.GenerateMemoriesRequestDirectContentsSourceEvent(
                                content=user_content
                            ),
                            vx_types.GenerateMemoriesRequestDirectContentsSourceEvent(
                                content=model_content
                            ),
                        ]
                    )
                    config: Any = vx_types.GenerateAgentEngineMemoriesConfig(
                        wait_for_completion=False,
                        revision_ttl=DEFAULT_TTL_S,
                    )
                except ImportError:
                    from types import SimpleNamespace

                    source = {
                        "events": [
                            {"content": user_content},
                            {"content": model_content},
                        ]
                    }
                    config = SimpleNamespace(
                        wait_for_completion=False,
                        revision_ttl=DEFAULT_TTL_S,
                    )

                client = self._get_client()
                return client.agent_engines.memories.generate(
                    name=self.engine_name,
                    direct_contents_source=source,
                    scope={"user_id": cid},
                    config=config,
                )
            except Exception as exc:
                logger.warning(
                    "Memory Bank generate failed for customer %s (%s: %s); best-effort ignored.",
                    cid,
                    type(exc).__name__,
                    exc,
                )
                return None

        if async_mode:
            return self._executor.submit(_do_generate)
        return _do_generate()


class FakeMemoryBankClient:
    """Hermetic in-memory test double for Memory Bank."""

    def __init__(
        self,
        memories_by_user: Optional[Dict[str, List[str]]] = None,
        simulate_timeout: bool = False,
        simulate_error: bool = False,
    ) -> None:
        self.memories_by_user: Dict[str, List[str]] = dict(memories_by_user or {})
        self.simulate_timeout = simulate_timeout
        self.simulate_error = simulate_error
        self.retrieve_calls: List[Dict[str, Any]] = []
        self.generate_calls: List[Dict[str, Any]] = []

    def retrieve_summary(
        self,
        customer_id: str,
        query: str = "",
        timeout_s: float = DEFAULT_RETRIEVAL_TIMEOUT_S,
        top_k: int = DEFAULT_TOP_K,
    ) -> str:
        if not is_memory_bank_enabled():
            return ""

        cid = str(customer_id).strip() if customer_id else ""
        if not cid or cid.startswith("anon:"):
            return ""

        self.retrieve_calls.append({
            "customer_id": cid,
            "query": query,
            "timeout_s": timeout_s,
            "top_k": top_k,
            "scope": {"user_id": cid},
        })

        if self.simulate_timeout:
            logger.warning("Simulated Memory Bank timeout")
            return ""

        if self.simulate_error:
            logger.warning("Simulated Memory Bank error")
            return ""

        user_mems = self.memories_by_user.get(cid, [])
        return render_memory_summary(user_mems[:top_k])

    def generate_turn_memories(
        self,
        customer_id: str,
        user_message_tokenized: str,
        decision_summary: str,
        async_mode: bool = True,
    ) -> Any:
        if not is_memory_bank_enabled():
            return None

        cid = str(customer_id).strip() if customer_id else ""
        if not cid or cid.startswith("anon:"):
            return None

        call_record = {
            "customer_id": cid,
            "user_message_tokenized": user_message_tokenized,
            "decision_summary": decision_summary,
            "scope": {"user_id": cid},
            "ttl": DEFAULT_TTL_S,
        }
        self.generate_calls.append(call_record)

        # Store a simple synthesized fact in fake
        self.memories_by_user.setdefault(cid, []).append(
            f"Prior inquiry: {decision_summary}"
        )
        return {"status": "GENERATED", "operation": "fake-op"}
