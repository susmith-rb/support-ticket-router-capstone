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
from ticket_router.agents.triage_router.tier3 import (
    Tier3Config,
    Tier3GeminiInvoker,
    create_tier3_invoker,
    default_tier3_invoker,
    invoke_tier3_gemini,
    is_tier3_trigger,
)

__all__ = [
    "CALMING_ACK_TEMPLATES",
    "HISTORY_MEMORY_TIMEOUT_S",
    "QueuePriority",
    "RouterDecision",
    "RouterThresholds",
    "Tier3Config",
    "Tier3GeminiInvoker",
    "TriagePolicy",
    "VerifiedFacts",
    "create_tier3_invoker",
    "default_tier3_invoker",
    "evaluate_gaming_resistant_priority",
    "invoke_tier3_gemini",
    "is_tier3_trigger",
    "render_calming_acknowledgement",
]
