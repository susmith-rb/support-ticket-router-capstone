"""Cloud Run Fallback Agent Host for TriageRouter.

# REQ-AGENT-035: Cloud Run Warm Fallback Agent Host (triage-fallback)
"""

from ticket_router.agents.fallback_host.app import create_app
from ticket_router.agents.fallback_host.env_contract import (
    TRIAGE_ENV_KEYS,
    filter_reserved_env_vars,
    get_triage_env_contract,
)
from ticket_router.agents.fallback_host.sessions import (
    InMemorySessionStore,
)

__all__ = [
    "TRIAGE_ENV_KEYS",
    "InMemorySessionStore",
    "create_app",
    "filter_reserved_env_vars",
    "get_triage_env_contract",
]
