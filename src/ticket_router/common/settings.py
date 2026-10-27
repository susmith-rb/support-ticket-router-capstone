"""Runtime configuration and environment settings for deployed agents.

# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-013: Centralized Model Configuration Resolution
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Dict, List, Optional, Tuple

from ticket_router.config.models import get_serving_config


@dataclass(frozen=True)
class Settings:
    """Frozen runtime configuration loaded from environment variables and config defaults."""

    project: str
    location: str
    agent_search_location: str
    agent_search_engine_id: str
    router_s1_endpoint: str
    router_s1_base_url: Optional[str]
    order_lookup_mcp_url: Optional[str]
    ticket_db_mcp_url: Optional[str]
    human_queue_url: Optional[str]
    faq_resolver_url: Optional[str]
    faq_resolver_audience: Optional[str]
    allowed_callers: Tuple[str, ...]
    require_real_deps: bool

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> Settings:
        """Construct Settings from environment dictionary (or os.environ)."""
        environ = os.environ if env is None else env

        # Defaults from models.yaml serving section
        serving_cfg = get_serving_config("router_s1")
        default_s1_endpoint = serving_cfg.get(
            "default_endpoint",
            "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1",
        )
        default_s1_base_url = serving_cfg.get("dedicated_base_url")

        project = environ.get("GOOGLE_CLOUD_PROJECT", "susmith-fde-ticket-router").strip()
        location = environ.get("GOOGLE_CLOUD_LOCATION", "us-central1").strip()
        agent_search_location = environ.get("AGENT_SEARCH_LOCATION", "global").strip()
        agent_search_engine_id = environ.get("AGENT_SEARCH_ENGINE_ID", "support-kb-search").strip()

        router_s1_endpoint = environ.get("ROUTER_S1_ENDPOINT", default_s1_endpoint).strip()
        router_s1_base_url = environ.get("ROUTER_S1_BASE_URL", default_s1_base_url)
        if router_s1_base_url is not None:
            router_s1_base_url = router_s1_base_url.strip() or None

        order_lookup_mcp_url = environ.get("ORDER_LOOKUP_MCP_URL")
        if order_lookup_mcp_url:
            order_lookup_mcp_url = order_lookup_mcp_url.strip() or None

        ticket_db_mcp_url = environ.get("TICKET_DB_MCP_URL")
        if ticket_db_mcp_url:
            ticket_db_mcp_url = ticket_db_mcp_url.strip() or None

        human_queue_url = environ.get("HUMAN_QUEUE_URL")
        if human_queue_url:
            human_queue_url = human_queue_url.strip() or None

        faq_resolver_url = environ.get("FAQ_RESOLVER_URL")
        if faq_resolver_url:
            faq_resolver_url = faq_resolver_url.strip() or None

        faq_resolver_audience = environ.get("FAQ_RESOLVER_AUDIENCE")
        if faq_resolver_audience:
            faq_resolver_audience = faq_resolver_audience.strip() or None
        else:
            faq_resolver_audience = faq_resolver_url

        allowed_callers_raw = environ.get("ALLOWED_CALLERS", "")
        callers_list: List[str] = []
        for caller in allowed_callers_raw.split(","):
            cleaned = caller.strip()
            if cleaned:
                callers_list.append(cleaned)
        allowed_callers = tuple(callers_list)

        # REQUIRE_REAL_DEPS: default is "1" in containers: fail fast at startup if any required var missing; tests set 0.
        raw_require = environ.get("REQUIRE_REAL_DEPS")
        if raw_require is not None:
            require_real_deps = raw_require.strip().lower() in ("1", "true", "yes", "on")
        elif "PYTEST_CURRENT_TEST" in os.environ:
            require_real_deps = False
        else:
            require_real_deps = True

        return cls(
            project=project,
            location=location,
            agent_search_location=agent_search_location,
            agent_search_engine_id=agent_search_engine_id,
            router_s1_endpoint=router_s1_endpoint,
            router_s1_base_url=router_s1_base_url,
            order_lookup_mcp_url=order_lookup_mcp_url,
            ticket_db_mcp_url=ticket_db_mcp_url,
            human_queue_url=human_queue_url,
            faq_resolver_url=faq_resolver_url,
            faq_resolver_audience=faq_resolver_audience,
            allowed_callers=allowed_callers,
            require_real_deps=require_real_deps,
        )

    def validate_for_faq(self) -> None:
        """Validate settings required for FaqResolverAgent role."""
        if not self.require_real_deps:
            return
        missing: List[str] = []
        if not self.project:
            missing.append("GOOGLE_CLOUD_PROJECT")
        if not self.location:
            missing.append("GOOGLE_CLOUD_LOCATION")
        if not self.agent_search_engine_id:
            missing.append("AGENT_SEARCH_ENGINE_ID")
        if not self.router_s1_endpoint:
            missing.append("ROUTER_S1_ENDPOINT")
        if not self.order_lookup_mcp_url:
            missing.append("ORDER_LOOKUP_MCP_URL")
        if not self.allowed_callers:
            missing.append("ALLOWED_CALLERS")
        if missing:
            raise ValueError(
                f"Missing required environment variable(s) for FaqResolver: {', '.join(missing)}"
            )

    def validate_for_triage(self) -> None:
        """Validate settings required for TriageRouterAgent role."""
        if not self.require_real_deps:
            return
        missing: List[str] = []
        if not self.project:
            missing.append("GOOGLE_CLOUD_PROJECT")
        if not self.location:
            missing.append("GOOGLE_CLOUD_LOCATION")
        if not self.router_s1_endpoint:
            missing.append("ROUTER_S1_ENDPOINT")
        if not self.ticket_db_mcp_url:
            missing.append("TICKET_DB_MCP_URL")
        if not self.faq_resolver_url:
            missing.append("FAQ_RESOLVER_URL")
        if missing:
            raise ValueError(
                f"Missing required environment variable(s) for TriageRouter: {', '.join(missing)}"
            )

    def validate(self, role: str) -> None:
        """Validate settings according to agent role ('faq' or 'triage')."""
        norm_role = role.lower().strip()
        if norm_role in ("faq", "faq_resolver"):
            self.validate_for_faq()
        elif norm_role in ("triage", "triage_router"):
            self.validate_for_triage()
        else:
            raise ValueError(f"Unknown agent role '{role}' for settings validation.")
