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

DEFAULT_PROJECT = "susmith-fde-ticket-router"
DEFAULT_REGION = "us-central1"
DEFAULT_SERVICE_ACCOUNT = "sa-triage@susmith-fde-ticket-router.iam.gserviceaccount.com"

# Default endpoints and service URLs
DEFAULT_ROUTER_ENDPOINT = "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1-dedicated"
DEFAULT_ROUTER_BASE_URL = "https://router-s1-dedicated.us-central1-620485621180.prediction.vertexai.goog"
DEFAULT_TICKET_DB_URL = "https://ticket-db-mcp-34mwh7k34a-uc.a.run.app"
DEFAULT_ORDER_LOOKUP_URL = "https://order-lookup-mcp-34mwh7k34a-uc.a.run.app"
DEFAULT_HUMAN_QUEUE_URL = "https://human-queue-34mwh7k34a-uc.a.run.app"
DEFAULT_HUMAN_QUEUE_TOPIC = "human-queue"
DEFAULT_REDACTION_KMS_KEY = "projects/susmith-fde-ticket-router/locations/us-central1/keyRings/ticket-router-identity/cryptoKeys/redaction-map-wrapper"
DEFAULT_FAQ_RESOLVER_URL = "https://faq.8-232-184-225.sslip.io"
DEFAULT_FAQ_RESOLVER_AUDIENCE = "https://faq.8-232-184-225.sslip.io"

DEFAULT_CCT_KEY_VERSION = "projects/susmith-fde-ticket-router/locations/us-central1/keyRings/ticket-router-identity/cryptoKeys/customer-context-signer/cryptoKeyVersions/1"
DEFAULT_CCT_ISSUER = "triage-router"
DEFAULT_CCT_AUDIENCE = "order-lookup-mcp"
DEFAULT_CCT_TTL_S = "300"
DEFAULT_MODEL_ARMOR_TEMPLATE = "projects/susmith-fde-ticket-router/locations/us-central1/templates/support-router-lean"

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
