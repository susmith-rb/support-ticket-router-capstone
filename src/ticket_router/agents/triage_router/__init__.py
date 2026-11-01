"""TriageRouterAgent module.

# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
"""

from ticket_router.agents.triage_router.history import (
    HISTORY_MEMORY_TIMEOUT_S,
)
from ticket_router.agents.triage_router.policy import (
    QueuePriority,
    RouterDecision,
    RouterThresholds,
    TriagePolicy,
    VerifiedFacts,
    evaluate_gaming_resistant_priority,
)
from ticket_router.agents.triage_router.templates import (
    CALMING_ACK_TEMPLATES,
    render_calming_acknowledgement,
)

__all__ = [
    "CALMING_ACK_TEMPLATES",
    "HISTORY_MEMORY_TIMEOUT_S",
    "QueuePriority",
    "RouterDecision",
    "RouterThresholds",
    "TriagePolicy",
    "VerifiedFacts",
    "evaluate_gaming_resistant_priority",
    "render_calming_acknowledgement",
]
