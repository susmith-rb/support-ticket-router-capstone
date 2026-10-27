"""TriageRouterAgent module.

# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
"""

from ticket_router.agents.triage_router.policy import (
    QueuePriority,
    RouterDecision,
    RouterThresholds,
    Tier0Result,
    TriagePolicy,
    VerifiedFacts,
    evaluate_gaming_resistant_priority,
    evaluate_tier0_rules,
)
from ticket_router.agents.triage_router.templates import (
    CALMING_ACK_TEMPLATES,
    render_calming_acknowledgement,
)

__all__ = [
    "CALMING_ACK_TEMPLATES",
    "QueuePriority",
    "RouterDecision",
    "RouterThresholds",
    "Tier0Result",
    "TriagePolicy",
    "VerifiedFacts",
    "evaluate_gaming_resistant_priority",
    "evaluate_tier0_rules",
    "render_calming_acknowledgement",
]
