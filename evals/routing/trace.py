"""Trace record schema and serialization for routing evaluation runs.

# REQ-REVAL-011: Per-case trace record contract.
WP-D consumes this trace record schema exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PredictedDecision:
    """Summary of the final predicted routing decision."""

    intent: str
    decision: str
    queue: str
    escalate: bool
    sentiment: Optional[str] = None
    urgency: Optional[str] = None
    tier: str = ""
    degraded: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "decision": self.decision,
            "queue": self.queue,
            "escalate": self.escalate,
            "sentiment": self.sentiment,
            "urgency": self.urgency,
            "tier": self.tier,
            "degraded": self.degraded,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PredictedDecision:
        return cls(
            intent=str(data.get("intent", "other")),
            decision=str(data.get("decision", "escalate")),
            queue=str(data.get("queue", "human-standard")),
            escalate=bool(data.get("escalate", True)),
            sentiment=data.get("sentiment"),
            urgency=data.get("urgency"),
            tier=str(data.get("tier", "")),
            degraded=bool(data.get("degraded", False)),
            reason=str(data.get("reason", "")),
        )


@dataclass
class TierCallRecord:
    """Record of an individual tier execution (live or cached)."""

    tier: str
    model_role: str
    model_id: str
    prompt_version: Optional[str] = None
    pass_index: int = 0
    cache_hit: bool = False
    latency_ms: float = 0.0
    usage: Optional[Dict[str, int]] = None
    cost_usd: float = 0.0
    error: Optional[str] = None
    prediction: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "model_role": self.model_role,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "pass_index": self.pass_index,
            "cache_hit": self.cache_hit,
            "latency_ms": self.latency_ms,
            "usage": self.usage,
            "cost_usd": self.cost_usd,
            "error": self.error,
            "prediction": self.prediction,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TierCallRecord:
        return cls(
            tier=str(data.get("tier", "")),
            model_role=str(data.get("model_role", "")),
            model_id=str(data.get("model_id", "")),
            prompt_version=data.get("prompt_version"),
            pass_index=int(data.get("pass_index", 0)),
            cache_hit=bool(data.get("cache_hit", False)),
            latency_ms=float(data.get("latency_ms", 0.0)),
            usage=data.get("usage"),
            cost_usd=float(data.get("cost_usd", 0.0)),
            error=data.get("error"),
            prediction=data.get("prediction"),
        )


@dataclass
class ReliabilityPass:
    """A single stochastic tier pass for reliability evaluation."""

    intent: str
    escalate: bool
    decision: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "escalate": self.escalate,
            "decision": self.decision,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ReliabilityPass:
        return cls(
            intent=str(data.get("intent", "other")),
            escalate=bool(data.get("escalate", True)),
            decision=str(data.get("decision", "escalate")),
        )


@dataclass
class ReliabilityRecord:
    """Collection of stochastic passes for k-pass reliability analysis."""

    passes: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"passes": list(self.passes)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ReliabilityRecord:
        raw_passes = data.get("passes", [])
        passes = []
        for p in raw_passes:
            if isinstance(p, dict):
                passes.append(p)
            elif hasattr(p, "to_dict"):
                passes.append(p.to_dict())
        return cls(passes=passes)


@dataclass
class TraceRecord:
    """Canonical trace record representing evaluation execution for a single ticket case.

    # REQ-REVAL-011: Strict schema contract across all runner targets.
    """

    ticket_id: str
    split: str
    target: str
    run_id: str
    config_hash: str
    candidate: str
    predicted: Dict[str, Any]
    decision_raw: Dict[str, Any]
    tier_calls: List[Dict[str, Any]] = field(default_factory=list)
    reliability: Optional[Dict[str, Any]] = None
    latency_ms_total: float = 0.0
    cost_usd_total: float = 0.0
    error: Optional[str] = None
    is_dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert trace record to canonical dictionary matching WP-D contract."""
        pred_dict = (
            self.predicted.to_dict()
            if hasattr(self.predicted, "to_dict")
            else dict(self.predicted)
        )
        calls_list = [
            c.to_dict() if hasattr(c, "to_dict") else dict(c) for c in self.tier_calls
        ]
        rel_dict = None
        if self.reliability is not None:
            rel_dict = (
                self.reliability.to_dict()
                if hasattr(self.reliability, "to_dict")
                else dict(self.reliability)
            )

        return {
            "ticket_id": self.ticket_id,
            "split": self.split,
            "target": self.target,
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "candidate": self.candidate,
            "predicted": pred_dict,
            "decision_raw": self.decision_raw,
            "tier_calls": calls_list,
            "reliability": rel_dict,
            "latency_ms_total": self.latency_ms_total,
            "cost_usd_total": self.cost_usd_total,
            "error": self.error,
            "is_dry_run": self.is_dry_run,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TraceRecord:
        """Construct TraceRecord from dictionary."""
        return cls(
            ticket_id=str(data["ticket_id"]),
            split=str(data["split"]),
            target=str(data["target"]),
            run_id=str(data["run_id"]),
            config_hash=str(data["config_hash"]),
            candidate=str(data["candidate"]),
            predicted=dict(data.get("predicted", {})),
            decision_raw=dict(data.get("decision_raw", {})),
            tier_calls=list(data.get("tier_calls", [])),
            reliability=dict(data["reliability"]) if data.get("reliability") is not None else None,
            latency_ms_total=float(data.get("latency_ms_total", 0.0)),
            cost_usd_total=float(data.get("cost_usd_total", 0.0)),
            error=data.get("error"),
            is_dry_run=bool(data.get("is_dry_run", False)),
        )
