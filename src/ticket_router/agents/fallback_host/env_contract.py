"""Canonical environment variable contract for TriageRouterAgent runtime hosts.

# REQ-AGENT-035: Cloud Run Warm Fallback Agent Host (triage-fallback)
# REQ-AGENT-017: Agent Runtime & Vertex AI Agent Engine Deployment

Shared contract definition between:
1. Vertex AI Agent Engine deploy script (scripts/deploy_agents/deploy_triage_agent_runtime.py)
2. Cloud Run fallback service (ticket_router.agents.fallback_host)
3. Terraform infrastructure declarations (deploy/terraform/single-project/modules/cloud-run-services)

Ensures that the environment contract is defined once and never forked.
"""

from __future__ import annotations

import os
from typing import Dict, List

from ticket_router.common.deploy_defaults import get_deploy_default

DEFAULT_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT") or get_deploy_default("project")
DEFAULT_REGION = os.environ.get("GOOGLE_CLOUD_LOCATION") or get_deploy_default("region", "us-central1")
DEFAULT_SERVICE_ACCOUNT = os.environ.get("SERVICE_ACCOUNT") or get_deploy_default("service_accounts.triage")

# Default endpoints and service URLs derived from environment or deploy defaults
DEFAULT_ROUTER_ENDPOINT = os.environ.get("ROUTER_S1_ENDPOINT") or get_deploy_default("endpoints.router_s1_endpoint")
DEFAULT_ROUTER_BASE_URL = os.environ.get("ROUTER_S1_BASE_URL") or get_deploy_default("endpoints.router_s1_base_url")
DEFAULT_TICKET_DB_URL = os.environ.get("TICKET_DB_MCP_URL") or get_deploy_default("services.ticket_db_url")
DEFAULT_ORDER_LOOKUP_URL = os.environ.get("ORDER_LOOKUP_MCP_URL") or get_deploy_default("services.order_lookup_url")
DEFAULT_HUMAN_QUEUE_URL = os.environ.get("HUMAN_QUEUE_URL") or get_deploy_default("services.human_queue_url")
DEFAULT_HUMAN_QUEUE_TOPIC = os.environ.get("HUMAN_QUEUE_TOPIC") or get_deploy_default("services.human_queue_topic", "human-queue")
DEFAULT_REDACTION_KMS_KEY = os.environ.get("REDACTION_KMS_KEY") or get_deploy_default("security.redaction_kms_key")
DEFAULT_FAQ_RESOLVER_URL = os.environ.get("FAQ_RESOLVER_URL") or get_deploy_default("services.faq_resolver_url")
DEFAULT_FAQ_RESOLVER_AUDIENCE = os.environ.get("FAQ_RESOLVER_AUDIENCE") or get_deploy_default("services.faq_resolver_audience")

DEFAULT_CCT_KEY_VERSION = os.environ.get("CCT_KEY_VERSION") or get_deploy_default("security.cct_key_version")
DEFAULT_CCT_ISSUER = os.environ.get("CCT_ISSUER") or get_deploy_default("security.cct_issuer", "triage-router")
DEFAULT_CCT_AUDIENCE = os.environ.get("CCT_AUDIENCE") or get_deploy_default("security.cct_audience", "order-lookup-mcp")
DEFAULT_CCT_TTL_S = os.environ.get("CCT_TTL_S") or get_deploy_default("security.cct_ttl_s", "300")
DEFAULT_MODEL_ARMOR_TEMPLATE = os.environ.get("MODEL_ARMOR_TEMPLATE") or get_deploy_default("security.model_armor_template")

# Canonical list of contract environment variable keys
TRIAGE_ENV_KEYS: List[str] = [
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "CLOUD_ML_PROJECT_ID",
    "AGENT_SEARCH_LOCATION",
    "AGENT_SEARCH_ENGINE_ID",
    "ROUTER_S1_ENDPOINT",
    "ROUTER_S1_BASE_URL",
    "ORDER_LOOKUP_MCP_URL",
    "TICKET_DB_MCP_URL",
    "HUMAN_QUEUE_URL",
    "HUMAN_QUEUE_TOPIC",
    "REDACTION_KMS_KEY",
    "FAQ_RESOLVER_URL",
    "FAQ_RESOLVER_AUDIENCE",
    "ALLOWED_CALLERS",
    "MODEL_ARMOR_TEMPLATE",
    "MODEL_ARMOR_ENABLED",
    "CCT_KEY_VERSION",
    "CCT_ISSUER",
    "CCT_AUDIENCE",
    "CCT_TTL_S",
    "ENABLE_CLOUD_TRACE",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
    "REQUIRE_REAL_DEPS",
    "SERVICE",
    "GE_OAUTH_AUTH_ID",
    "GE_OAUTH_CLIENT_ID",
    "PYTHONPATH",
]


def get_triage_env_contract(
    project: str = DEFAULT_PROJECT,
    region: str = DEFAULT_REGION,
    ticket_db_mcp_url: str = DEFAULT_TICKET_DB_URL,
    order_lookup_mcp_url: str = DEFAULT_ORDER_LOOKUP_URL,
    human_queue_url: str = DEFAULT_HUMAN_QUEUE_URL,
    human_queue_topic: str = DEFAULT_HUMAN_QUEUE_TOPIC,
    redaction_kms_key: str = DEFAULT_REDACTION_KMS_KEY,
    faq_resolver_url: str = DEFAULT_FAQ_RESOLVER_URL,
    faq_resolver_audience: str = DEFAULT_FAQ_RESOLVER_AUDIENCE,
    router_s1_endpoint: str = DEFAULT_ROUTER_ENDPOINT,
    router_s1_base_url: str = DEFAULT_ROUTER_BASE_URL,
    agent_search_location: str = "global",
    agent_search_engine_id: str = "support-kb-search",
    allowed_callers: str = DEFAULT_SERVICE_ACCOUNT,
    model_armor_template: str = DEFAULT_MODEL_ARMOR_TEMPLATE,
    model_armor_enabled: str = "1",
    require_real_deps: str = "1",
    service: str = "triage_fallback",
    ge_oauth_auth_id: str | None = None,
    ge_oauth_client_id: str | None = None,
    cct_key_version: str = DEFAULT_CCT_KEY_VERSION,
    cct_issuer: str = DEFAULT_CCT_ISSUER,
    cct_audience: str = DEFAULT_CCT_AUDIENCE,
    cct_ttl_s: str = DEFAULT_CCT_TTL_S,
    id_token_impersonate_sa: str | None = None,
) -> Dict[str, str]:
    """Construct canonical dictionary of environment variables mirroring Agent Engine."""
    env_vars: Dict[str, str] = {
        "GOOGLE_CLOUD_PROJECT": project,
        "GOOGLE_CLOUD_LOCATION": region,
        "CLOUD_ML_PROJECT_ID": project,
        "AGENT_SEARCH_LOCATION": agent_search_location,
        "AGENT_SEARCH_ENGINE_ID": agent_search_engine_id,
        "ROUTER_S1_ENDPOINT": router_s1_endpoint,
        "ROUTER_S1_BASE_URL": router_s1_base_url,
        "ORDER_LOOKUP_MCP_URL": order_lookup_mcp_url,
        "TICKET_DB_MCP_URL": ticket_db_mcp_url,
        "HUMAN_QUEUE_URL": human_queue_url,
        "HUMAN_QUEUE_TOPIC": human_queue_topic,
        "REDACTION_KMS_KEY": redaction_kms_key,
        "FAQ_RESOLVER_URL": faq_resolver_url,
        "FAQ_RESOLVER_AUDIENCE": faq_resolver_audience,
        "ALLOWED_CALLERS": allowed_callers,
        "MODEL_ARMOR_TEMPLATE": model_armor_template,
        "MODEL_ARMOR_ENABLED": model_armor_enabled,
        "CCT_KEY_VERSION": cct_key_version,
        "CCT_ISSUER": cct_issuer,
        "CCT_AUDIENCE": cct_audience,
        "CCT_TTL_S": cct_ttl_s,
        "ENABLE_CLOUD_TRACE": "1",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT",
        "REQUIRE_REAL_DEPS": require_real_deps,
        "SERVICE": service,
        "PYTHONPATH": "/code/src:src:.",
    }

    oauth_auth_id = ge_oauth_auth_id or os.environ.get("GE_OAUTH_AUTH_ID", "support-customer-oauth")
    if oauth_auth_id:
        env_vars["GE_OAUTH_AUTH_ID"] = oauth_auth_id

    oauth_client_id = ge_oauth_client_id or os.environ.get("GE_OAUTH_CLIENT_ID")
    if oauth_client_id:
        env_vars["GE_OAUTH_CLIENT_ID"] = oauth_client_id

    if id_token_impersonate_sa:
        env_vars["ID_TOKEN_IMPERSONATE_SA"] = id_token_impersonate_sa

    return env_vars


def filter_reserved_env_vars(env_dict: Dict[str, str]) -> Dict[str, str]:
    """Filter out reserved environment variable names rejected by Vertex Agent Engine control plane."""
    filtered: Dict[str, str] = {}
    for key, value in env_dict.items():
        if key in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_REGION"):
            continue
        if key.startswith("GOOGLE_CLOUD_AGENT_ENGINE"):
            continue
        filtered[key] = value
    return filtered
