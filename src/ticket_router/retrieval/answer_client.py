"""Answer client for FaqResolver with call-time 429 fault-injection hook.

# REQ-CHAOS-001: Chaos Engineering Fault Injection, Probing, and Safe Restoration
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from ticket_router.models.tier2_gemini import check_gemini_429_hook


class GeminiAnswerClient:
    """Answer client wrapper with call-time 429 fault-injection hook."""

    def __init__(
        self,
        project: str,
        location: str,
        model_role: str = "answer",
    ) -> None:
        from ticket_router.agents.faq_resolver.agent import (
            GeminiAnswerClient as _RealAnswerClient,
        )

        self._impl = _RealAnswerClient(
            project=project,
            location=location,
            model_role=model_role,
        )

    def generate_answer(
        self,
        query: str,
        retrieved_docs: Sequence[Dict[str, Any]],
        order_info: Optional[Dict[str, Any]] = None,
        model_id: Optional[str] = None,
        system_instruction: Optional[str] = None,
        feedback: Optional[str] = None,
        deadline_s: Optional[float] = None,
        hedge_after_s: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Synthesize answer with mandatory citations, checking 429 hook before network call."""
        check_gemini_429_hook()
        return self._impl.generate_answer(
            query=query,
            retrieved_docs=retrieved_docs,
            order_info=order_info,
            model_id=model_id,
            system_instruction=system_instruction,
            feedback=feedback,
            deadline_s=deadline_s,
            hedge_after_s=hedge_after_s,
            **kwargs,
        )
