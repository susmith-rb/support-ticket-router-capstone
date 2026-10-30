"""Runtime configuration and environment settings for deployed agents.

# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-013: Centralized Model Configuration Resolution
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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
    model_armor_template: str
    model_armor_enabled: bool
    ge_oauth_auth_id: str = "support-customer-oauth"
    ge_oauth_client_id: Optional[str] = None
    cct_mode: str = "accept"
    cct_issuer: str = "triage-router"
    cct_audience: str = "order-lookup-mcp"
    cct_ttl_s: int = 300
    cct_key_version: str = ""
    cct_key_prefix: str = (
        "projects/susmith-fde-ticket-router/locations/us-central1/keyRings/ticket-router-identity/cryptoKeys/customer-context-signer/"
    )
    cct_allowed_issuers: Tuple[str, ...] = ("triage-router",)

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> Settings:
        """Construct Settings from environment dictionary (or os.environ)."""
        environ = os.environ if env is None else env

        # Defaults from models.yaml serving section
        serving_cfg = get_serving_config("router_s1")
        default_s1_endpoint = serving_cfg.get(
            "default_endpoint",
            "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1-dedicated",
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

        # Model Armor settings
        default_ma_template = f"projects/{project}/locations/us-central1/templates/support-router-lean"
        model_armor_template = environ.get("MODEL_ARMOR_TEMPLATE", default_ma_template).strip()

        raw_ma_enabled = environ.get("MODEL_ARMOR_ENABLED")
        if raw_ma_enabled is not None:
            model_armor_enabled = raw_ma_enabled.strip().lower() in ("1", "true", "yes", "on")
        elif "PYTEST_CURRENT_TEST" in os.environ:
            model_armor_enabled = False
        else:
            model_armor_enabled = True

        ge_oauth_auth_id = (
            environ.get("GE_OAUTH_AUTH_ID", "support-customer-oauth").strip()
            or "support-customer-oauth"
        )
        ge_oauth_client_id = (
            environ.get("GE_OAUTH_CLIENT_ID", "").strip() or None
        )

        raw_cct_mode = environ.get("CCT_MODE", "accept").strip().lower() or "accept"
        if raw_cct_mode not in ("off", "accept", "require"):
            raise ValueError(
                f"Invalid CCT_MODE: '{raw_cct_mode}' (must be one of 'off', 'accept', 'require')"
            )
        cct_mode = raw_cct_mode

        cct_issuer = environ.get("CCT_ISSUER", "triage-router").strip() or "triage-router"
        cct_audience = environ.get("CCT_AUDIENCE", "order-lookup-mcp").strip() or "order-lookup-mcp"

        raw_cct_ttl_s = environ.get("CCT_TTL_S")
        if raw_cct_ttl_s is not None and raw_cct_ttl_s.strip():
            cct_ttl_s = int(raw_cct_ttl_s.strip())
        else:
            cct_ttl_s = 300

        cct_key_version = environ.get("CCT_KEY_VERSION", "").strip()
        default_cct_prefix = (
            "projects/susmith-fde-ticket-router/locations/us-central1/keyRings/ticket-router-identity/cryptoKeys/customer-context-signer/"
        )
        cct_key_prefix = environ.get("CCT_KEY_PREFIX", default_cct_prefix).strip() or default_cct_prefix

        raw_cct_issuers = environ.get("CCT_ALLOWED_ISSUERS")
        if raw_cct_issuers is not None:
            issuers_list: List[str] = []
            for iss in raw_cct_issuers.split(","):
                cleaned = iss.strip()
                if cleaned:
                    issuers_list.append(cleaned)
            cct_allowed_issuers = tuple(issuers_list)
        else:
            cct_allowed_issuers = ("triage-router",)

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
            model_armor_template=model_armor_template,
            model_armor_enabled=model_armor_enabled,
            ge_oauth_auth_id=ge_oauth_auth_id,
            ge_oauth_client_id=ge_oauth_client_id,
            cct_mode=cct_mode,
            cct_issuer=cct_issuer,
            cct_audience=cct_audience,
            cct_ttl_s=cct_ttl_s,
            cct_key_version=cct_key_version,
            cct_key_prefix=cct_key_prefix,
            cct_allowed_issuers=cct_allowed_issuers,
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
