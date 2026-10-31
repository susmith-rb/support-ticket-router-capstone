"""Human review web UI behind Cloud Run Identity-Aware Proxy (IAP).

# REQ-AGENT-033: Human Review UI with Cloud Run IAP and Verified Rep Identity
# REQ-AUTH-010: Human Review UI Rep Identity Assertion and sa-review-ui Least Privilege
"""

from __future__ import annotations

from ticket_router.agents.review_ui.auth import verify_iap_jwt
from ticket_router.agents.review_ui.client import HumanQueueClient
from ticket_router.agents.review_ui.server import create_app

__all__ = [
    "HumanQueueClient",
    "create_app",
    "verify_iap_jwt",
]
