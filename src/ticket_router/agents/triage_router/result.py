"""result.py — TriageResult dataclass for TriageRouterAgent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ticket_router.agents.triage_router.policy import RouterDecision


@dataclass(frozen=True)
class TriageResult:
    """End-to-end outcome of a triage routing turn."""

    ticket_id: str
    session_id: str
    decision: RouterDecision
    calming_ack: Optional[str] = None
    handoff_response: Optional[Dict[str, Any]] = None
    idempotency_key: str = ""
    latency_ms: float = 0.0
    status_code: int = 200
    error: Optional[str] = None

    @property
    def reply_text(self) -> str:
        """Customer-facing reply text, falling back across calming_ack and handoff answer."""
        if self.calming_ack:
            return self.calming_ack
        if self.handoff_response:
            ans = self.handoff_response.get("answer") or self.handoff_response.get("reply_text")
            if ans:
                return str(ans)
        return ""

    def __getitem__(self, item: str) -> Any:
        return self.to_dict()[item]

    def __contains__(self, item: object) -> bool:
        return item in self.to_dict()

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        """Convert result to response dictionary."""
        d: Dict[str, Any] = {
            "ticket_id": self.ticket_id,
            "session_id": self.session_id,
            "decision": self.decision.decision,
            "queue": self.decision.queue,
            "calming_ack": self.calming_ack,
            "intent": self.decision.predicted_intent,
            "latency_ms": self.latency_ms,
            "status_code": self.status_code,
        }
        if self.error:
            d["error"] = self.error
        if self.calming_ack:
            d["answer"] = self.calming_ack
        elif self.handoff_response and self.handoff_response.get("answer"):
            d["answer"] = self.handoff_response.get("answer")
        if self.handoff_response:
            d["handoff_response"] = self.handoff_response
        return d
