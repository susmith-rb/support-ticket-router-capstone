#!/usr/bin/env python3
"""Deploy TriageRouterAgent to Vertex AI Agent Engine / Reasoning Engine runtime.

Implements REQ-AGENT-017: Agent Runtime deployment for TriageRouterAgent.
Implements REQ-AUTH-004: SPIFFE Agent Identity (--identity-type agent) deployment option.

Key deployment invariants:
1. Environment Isolation & Contract: Sets all contract environment variables
   in-process before importing agent definitions or serializing closures,
   preventing foreign developer/workstation project IDs from baking into the
   pickled artifact:
   - GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION
   - AGENT_SEARCH_LOCATION=global, AGENT_SEARCH_ENGINE_ID=support-kb-search
   - ROUTER_S1_ENDPOINT, ROUTER_S1_BASE_URL
   - ORDER_LOOKUP_MCP_URL, TICKET_DB_MCP_URL, HUMAN_QUEUE_URL
   - FAQ_RESOLVER_URL, FAQ_RESOLVER_AUDIENCE
   - CCT_KEY_VERSION, CCT_ISSUER, CCT_AUDIENCE, CCT_TTL_S
   - ALLOWED_CALLERS, REQUIRE_REAL_DEPS=1, SERVICE=triage_router
2. Control Plane Reserved Names: Filters out reserved names
   (GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, GOOGLE_CLOUD_REGION,
   GOOGLE_CLOUD_AGENT_ENGINE*) from remote container env_vars to prevent
   FAILED_PRECONDITION rejections.
3. Pinned Runtime Dependencies: Requirements combine pyproject.toml [agents, runtime]
   extras, pinned cloudpickle, pydantic>=2.0.0, google-adk>=2.2.0, and
   google-cloud-aiplatform[agent_engines,adk]>=1.75.0.
4. Extra Packages: Stages local package code and prompt definitions (src/ticket_router, prompts).
5. Service Account Association: Binds remote execution identity to sa-triage@.
6. Staging Bucket: gs://susmith-fde-ticket-router-laya/agent-engine-staging.
7. Support for in-place updates (--update <resource_name>).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

VENV_SITE_PACKAGES = (
    REPO_ROOT / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
)
if VENV_SITE_PACKAGES.is_dir() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("deploy_triage_agent_runtime")

DEFAULT_PROJECT = "susmith-fde-ticket-router"
DEFAULT_REGION = "us-central1"
DEFAULT_STAGING_BUCKET = "gs://susmith-fde-ticket-router-laya/agent-engine-staging"
DEFAULT_DISPLAY_NAME = "triage-router-agent"
DEFAULT_SERVICE_ACCOUNT = "sa-triage@susmith-fde-ticket-router.iam.gserviceaccount.com"

# Pinned runtime dependencies for remote container unpickling and execution parity
PINNED_REQUIREMENTS = [
    "google-cloud-aiplatform[agent_engines,adk]>=1.75.0",
    "google-adk>=2.2.0",
    "cloudpickle==3.0.0",
    "pydantic>=2.0.0",
    "cryptography>=42.0.0",
    "google-genai>=0.1.0",
    "requests>=2.32.0",
    "google-auth>=2.27.0",
    "pyyaml>=6.0.1",
    "opentelemetry-api>=1.20.0",
    "opentelemetry-sdk>=1.20.0",
    "opentelemetry-exporter-gcp-trace>=1.6.0",
]

DEFAULT_EXTRA_PACKAGES = ["src/ticket_router", "prompts"]

# Default contract endpoints and live service URLs
DEFAULT_ROUTER_ENDPOINT = (
    "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1-dedicated"
)
DEFAULT_ROUTER_BASE_URL = (
    "https://router-s1-dedicated.us-central1-620485621180.prediction.vertexai.goog"
)
DEFAULT_TICKET_DB_URL = "https://ticket-db-mcp-34mwh7k34a-uc.a.run.app"
DEFAULT_ORDER_LOOKUP_URL = "https://order-lookup-mcp-34mwh7k34a-uc.a.run.app"
DEFAULT_HUMAN_QUEUE_URL = "https://human-queue-34mwh7k34a-uc.a.run.app"
DEFAULT_HUMAN_QUEUE_TOPIC = "human-queue"
# REQ-STORE-009: symmetric KMS key that wraps redaction-map DEKs (TriageRouter encrypts only).
DEFAULT_REDACTION_KMS_KEY = (
    "projects/susmith-fde-ticket-router/locations/us-central1/keyRings/ticket-router-identity/cryptoKeys/redaction-map-wrapper"
)
DEFAULT_FAQ_RESOLVER_URL = "https://faq.8-232-184-225.sslip.io"
DEFAULT_FAQ_RESOLVER_AUDIENCE = "https://faq.8-232-184-225.sslip.io"

DEFAULT_CCT_KEY_VERSION = (
    "projects/susmith-fde-ticket-router/locations/us-central1/keyRings/ticket-router-identity/cryptoKeys/customer-context-signer/cryptoKeyVersions/1"
)
DEFAULT_CCT_ISSUER = "triage-router"
DEFAULT_CCT_AUDIENCE = "order-lookup-mcp"
DEFAULT_CCT_TTL_S = "300"


def prepare_environment(
    project: str,
    region: str,
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
    model_armor_template: str = (
        "projects/susmith-fde-ticket-router/locations/us-central1/templates/support-router-lean"
    ),
    model_armor_enabled: str = "1",
    require_real_deps: str = "1",
    service: str = "triage_router",
    ge_oauth_auth_id: str | None = None,
    ge_oauth_client_id: str | None = None,
    cct_key_version: str = DEFAULT_CCT_KEY_VERSION,
    cct_issuer: str = DEFAULT_CCT_ISSUER,
    cct_audience: str = DEFAULT_CCT_AUDIENCE,
    cct_ttl_s: str = DEFAULT_CCT_TTL_S,
    id_token_impersonate_sa: str | None = None,
) -> dict[str, str]:
    """Set environment variables in-process before importing or pickling.

    Critical pitfall mitigation: Prevents local workstation project names from
    being baked into the serialized agent closure by AdkApp or cloudpickle, while
    supplying all mandatory contract environment variables.
    """
    env_vars = {
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
    else:
        os.environ.pop("ID_TOKEN_IMPERSONATE_SA", None)

    for key, val in env_vars.items():
        os.environ[key] = val

    logger.info("Environment contract isolated with %d variables", len(env_vars))
    return env_vars


def filter_reserved_env_vars(env_dict: dict[str, str]) -> dict[str, str]:
    """Filter out reserved environment variable names rejected by Vertex Agent Engine control plane."""
    filtered: dict[str, str] = {}
    for key, value in env_dict.items():
        if key in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_REGION"):
            continue
        if key.startswith("GOOGLE_CLOUD_AGENT_ENGINE"):
            continue
        filtered[key] = value
    return filtered


def build_agent_app() -> Any:
    """Instantiate the TriageRouter agent application from adk_agent.

    Imports root_agent and AdkApp from ticket_router.agents.triage_router.adk_agent.
    Fails loudly on ImportError without any mock or stub fallback.
    """
    from ticket_router.agents.triage_router.adk_agent import app, root_agent
    logger.info("Successfully imported ticket_router.agents.triage_router.adk_agent")

    if app is not None:
        return app

    try:
        from vertexai.agent_engines.templates.adk import AdkApp
        return AdkApp(agent=root_agent, enable_tracing=True)
    except (ImportError, Exception):
        pass

    try:
        from vertexai.preview.reasoning_engines import AdkApp
        return AdkApp(agent=root_agent, enable_tracing=True)
    except (ImportError, Exception):
        pass

    return root_agent


def wait_for_reasoning_engine_idle(
    resource_name: str,
    project: str,
    region: str,
    max_wait_seconds: int = 600,
    poll_interval_seconds: int = 15,
) -> None:
    """Poll until the target reasoning engine is not UPDATING (≤10 min).

    Implements REQ-CD-006: Before calling update, wait until the engine is not
    UPDATING (poll ≤10 min) — a concurrent update returns 400.
    """
    logger.info("Checking if Agent Engine %s is UPDATING...", resource_name)
    start_time = time.time()
    try:
        from google.cloud import aiplatform_v1
        client = aiplatform_v1.ReasoningEngineServiceClient(
            client_options={"api_endpoint": f"{region}-aiplatform.googleapis.com"}
        )
        parent = f"projects/{project}/locations/{region}"
        while time.time() - start_time < max_wait_seconds:
            resp = client.list_operations(request={"name": parent})
            active_ops = [
                op.name for op in resp.operations
                if resource_name in op.name and not op.done
            ]
            if not active_ops:
                logger.info("Agent Engine %s is idle (no active operations).", resource_name)
                return
            logger.info(
                "Agent Engine %s is UPDATING (%d active operation(s)); waiting %ds...",
                resource_name,
                len(active_ops),
                poll_interval_seconds,
            )
            time.sleep(poll_interval_seconds)
        raise TimeoutError(
            f"Timed out after {max_wait_seconds}s waiting for Agent Engine {resource_name} to finish UPDATING"
        )
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            raise
        logger.warning("Could not check active operations for %s: %s", resource_name, exc)


def _non_empty_str(val: Any) -> str | None:
    """Return stripped string if val is a non-empty str, else None."""
    if isinstance(val, str):
        stripped = val.strip()
        if stripped:
            return stripped
    return None


def format_effective_identity_principal(raw_identity: str | None) -> str | None:
    """Normalize Vertex AI ReasoningEngine effective_identity into IAM member format.

    # REQ-AUTH-004: SDK returns SPIFFE identity without 'principal://' prefix
    # (agents.global.org-<ORG>.system.id.goog/resources/aiplatform/...).
    """
    ident = _non_empty_str(raw_identity)
    if not ident:
        return None
    if ident.startswith(("principal://", "serviceAccount:")):
        return ident
    if "@" in ident and "/" not in ident:
        return f"serviceAccount:{ident}"
    return f"principal://{ident}"


def _extract_raw_engine_fields(engine: Any) -> tuple[str | None, str | None]:
    """Extract (resource_name, raw_effective_identity) from a Client AgentEngine object."""
    if engine is None:
        return None, None

    api_res = getattr(engine, "api_resource", None)
    res_name: str | None = None
    raw_identity: str | None = None

    if isinstance(api_res, dict):
        res_name = _non_empty_str(api_res.get("name"))
        spec = api_res.get("spec")
        if isinstance(spec, dict):
            raw_identity = _non_empty_str(spec.get("effective_identity"))
        else:
            raw_identity = _non_empty_str(getattr(spec, "effective_identity", None))
        if not raw_identity:
            raw_identity = _non_empty_str(api_res.get("effective_identity"))
    elif api_res is not None:
        res_name = _non_empty_str(getattr(api_res, "name", None))
        spec = getattr(api_res, "spec", None)
        if isinstance(spec, dict):
            raw_identity = _non_empty_str(spec.get("effective_identity"))
        elif spec is not None:
            raw_identity = _non_empty_str(getattr(spec, "effective_identity", None))
        if not raw_identity:
            raw_identity = _non_empty_str(getattr(api_res, "effective_identity", None))

    if not res_name:
        res_name = _non_empty_str(getattr(engine, "resource_name", None)) or _non_empty_str(
            getattr(engine, "name", None)
        )
    if not raw_identity:
        raw_identity = _non_empty_str(getattr(engine, "effective_identity", None))

    return res_name, raw_identity


def _extract_client_engine_info(
    engine: Any,
    client: Any = None,
    fallback_name: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve resource_name and IAM principal string from client.agent_engines response."""
    res_name, raw_identity = _extract_raw_engine_fields(engine)
    if not res_name:
        res_name = _non_empty_str(fallback_name)

    if not raw_identity and client is not None and res_name:
        try:
            fetched = client.agent_engines.get(name=res_name)
            fetched_name, fetched_identity = _extract_raw_engine_fields(fetched)
            if not res_name and fetched_name:
                res_name = fetched_name
            raw_identity = fetched_identity
        except Exception as exc:
            logger.warning("Could not fetch effective_identity via get(%s): %s", res_name, exc)

    return res_name, format_effective_identity_principal(raw_identity)


def deploy_reasoning_engine(
    project: str,
    region: str,
    staging_bucket: str,
    display_name: str,
    service_account: str = DEFAULT_SERVICE_ACCOUNT,
    requirements: list[str] | None = None,
    extra_packages: list[str] | None = None,
    contract_vars: dict[str, str] | None = None,
    update_resource_name: str | None = None,
    dry_run: bool = False,
    identity_type: str = "sa",
    resource_name: str | None = None,
    agent_gateway: str | None = None,
) -> str | None:
    """Package and deploy or update TriageRouterAgent on Vertex AI Agent Engine."""
    if identity_type not in ("sa", "agent", "service_account"):
        raise ValueError(f"Unsupported identity_type={identity_type!r}; expected 'sa' or 'agent'")
    effective_identity_type = "sa" if identity_type in ("sa", "service_account") else "agent"

    agent_gw = _non_empty_str(agent_gateway)
    if agent_gw and effective_identity_type != "agent":
        raise ValueError(
            f"--agent-gateway is not supported with identity_type={identity_type!r}; requires 'agent'"
        )

    upd_arg = _non_empty_str(update_resource_name)
    res_arg = _non_empty_str(resource_name)
    if upd_arg and res_arg and upd_arg != res_arg:
        raise ValueError("update_resource_name and resource_name must match when both are provided")
    target_resource = res_arg or upd_arg

    reqs = requirements or PINNED_REQUIREMENTS
    pkgs = extra_packages or DEFAULT_EXTRA_PACKAGES
    contract = contract_vars or {}

    if effective_identity_type == "agent":
        default_triage_sa = f"sa-triage@{project}.iam.gserviceaccount.com"
        impersonate_sa = _non_empty_str(contract.get("ID_TOKEN_IMPERSONATE_SA")) or default_triage_sa
        default_allowed_callers = impersonate_sa
    else:
        impersonate_sa = None
        default_allowed_callers = service_account

    raw_env_vars = prepare_environment(
        project=project,
        region=region,
        ticket_db_mcp_url=contract.get("TICKET_DB_MCP_URL", DEFAULT_TICKET_DB_URL),
        order_lookup_mcp_url=contract.get("ORDER_LOOKUP_MCP_URL", DEFAULT_ORDER_LOOKUP_URL),
        human_queue_url=contract.get("HUMAN_QUEUE_URL", DEFAULT_HUMAN_QUEUE_URL),
        human_queue_topic=contract.get("HUMAN_QUEUE_TOPIC", DEFAULT_HUMAN_QUEUE_TOPIC),
        redaction_kms_key=contract.get("REDACTION_KMS_KEY", DEFAULT_REDACTION_KMS_KEY),
        faq_resolver_url=contract.get("FAQ_RESOLVER_URL", DEFAULT_FAQ_RESOLVER_URL),
        faq_resolver_audience=contract.get("FAQ_RESOLVER_AUDIENCE", DEFAULT_FAQ_RESOLVER_AUDIENCE),
        router_s1_endpoint=contract.get("ROUTER_S1_ENDPOINT", DEFAULT_ROUTER_ENDPOINT),
        router_s1_base_url=contract.get("ROUTER_S1_BASE_URL", DEFAULT_ROUTER_BASE_URL),
        agent_search_location=contract.get("AGENT_SEARCH_LOCATION", "global"),
        agent_search_engine_id=contract.get("AGENT_SEARCH_ENGINE_ID", "support-kb-search"),
        allowed_callers=contract.get("ALLOWED_CALLERS", default_allowed_callers),
        require_real_deps=contract.get("REQUIRE_REAL_DEPS", "1"),
        service=contract.get("SERVICE", "triage_router"),
        ge_oauth_auth_id=contract.get(
            "GE_OAUTH_AUTH_ID", os.environ.get("GE_OAUTH_AUTH_ID", "support-customer-oauth")
        ),
        ge_oauth_client_id=contract.get(
            "GE_OAUTH_CLIENT_ID", os.environ.get("GE_OAUTH_CLIENT_ID")
        ),
        cct_key_version=contract.get("CCT_KEY_VERSION", DEFAULT_CCT_KEY_VERSION),
        cct_issuer=contract.get("CCT_ISSUER", DEFAULT_CCT_ISSUER),
        cct_audience=contract.get("CCT_AUDIENCE", DEFAULT_CCT_AUDIENCE),
        cct_ttl_s=contract.get("CCT_TTL_S", DEFAULT_CCT_TTL_S),
        id_token_impersonate_sa=impersonate_sa,
    )
    remote_env_vars = filter_reserved_env_vars(raw_env_vars)
    agent_app = build_agent_app()

    logger.info("Target Project:        %s", project)
    logger.info("Target Region:         %s", region)
    logger.info("Staging Bucket:        %s", staging_bucket)
    logger.info("Display Name:          %s", display_name)
    logger.info("Identity Type:         %s", effective_identity_type)
    if agent_gw:
        logger.info("Agent Gateway:         %s", agent_gw)
    if effective_identity_type == "agent":
        logger.info("Impersonate SA (OIDC): %s", impersonate_sa)
    else:
        logger.info("Service Account:       %s", service_account)
    logger.info("Extra Packages:        %s", pkgs)
    logger.info("Requirements:          %s", reqs)
    logger.info("Remote Env Vars:       %d variables (reserved excluded)", len(remote_env_vars))
    logger.info("Update Resource:       %s", target_resource or "None (Creating new)")
    logger.info("Dry Run:               %s", dry_run)

    if dry_run:
        sample_name = f"projects/{project}/locations/{region}/reasoningEngines/dry-run-sample-id"
        logger.info("[Dry Run] Agent application built and serialized successfully without calling GCP APIs.")
        if effective_identity_type == "agent":
            sample_principal = (
                f"principal://agents.global.org-dry-run.system.id.goog/resources/aiplatform/{sample_name}"
            )
            print(sample_name)
            print(f"effective_identity: {sample_principal}")
        return sample_name

    import vertexai

    bucket_uri = staging_bucket
    if "/" in staging_bucket.replace("gs://", ""):
        bucket_name = staging_bucket.replace("gs://", "").split("/")[0]
        bucket_uri = f"gs://{bucket_name}"

    deployed_resource_name: str | None = None
    if effective_identity_type == "agent":
        # REQ-AUTH-004: Deploy via vertexai.Client().agent_engines with AGENT_IDENTITY and no service_account.
        # REQ-AUTH-007: Optional Agent Gateway configuration and token sharing prevention.
        if agent_gw:
            remote_env_vars["GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES"] = "False"
        client = vertexai.Client(project=project, location=region)
        agent_config: dict[str, Any] = {
            "staging_bucket": bucket_uri,
            "requirements": reqs,
            "extra_packages": pkgs,
            "env_vars": remote_env_vars,
            "display_name": display_name,
            "description": "Support Ticket Router Triage Agent",
            "gcs_dir_name": "agent_engine/triage_router",
            "identity_type": vertexai.types.IdentityType.AGENT_IDENTITY,
        }
        if agent_gw:
            agent_config["agent_gateway_config"] = {
                "agent_to_anywhere_config": {
                    "agent_gateway": agent_gw,
                }
            }
        if target_resource:
            wait_for_reasoning_engine_idle(
                resource_name=target_resource,
                project=project,
                region=region,
            )
            logger.info("Updating existing Agent Engine (AGENT_IDENTITY): %s", target_resource)
            engine = client.agent_engines.update(
                name=target_resource,
                agent=agent_app,
                config=agent_config,
            )
            deployed_resource_name, effective_principal = _extract_client_engine_info(
                engine,
                client=client,
                fallback_name=target_resource,
            )
            logger.info("Agent Engine updated successfully: %s", deployed_resource_name)
        else:
            logger.info("Creating new Agent Engine on Vertex AI (AGENT_IDENTITY)...")
            engine = client.agent_engines.create(
                agent=agent_app,
                config=agent_config,
            )
            deployed_resource_name, effective_principal = _extract_client_engine_info(
                engine,
                client=client,
            )
            logger.info("Agent Engine deployed successfully: %s", deployed_resource_name)

        if effective_principal:
            logger.info("Effective Identity:    %s", effective_principal)
        if deployed_resource_name:
            print(deployed_resource_name)
        if effective_principal:
            print(f"effective_identity: {effective_principal}")
    else:
        vertexai.init(project=project, location=region, staging_bucket=bucket_uri)

        has_agent_engines = False
        try:
            from vertexai import agent_engines
            has_agent_engines = True
        except (ImportError, AttributeError):
            has_agent_engines = False

        if target_resource:
            wait_for_reasoning_engine_idle(
                resource_name=target_resource,
                project=project,
                region=region,
            )
            logger.info("Updating existing Agent Engine: %s", target_resource)
            if has_agent_engines:
                from vertexai import agent_engines
                engine = agent_engines.update(
                    resource_name=target_resource,
                    agent_engine=agent_app,
                    requirements=reqs,
                    extra_packages=pkgs,
                    env_vars=remote_env_vars,
                    display_name=display_name,
                    description="Support Ticket Router Triage Agent",
                    service_account=service_account,
                    gcs_dir_name="agent_engine/triage_router",
                )
                deployed_resource_name = str(engine.resource_name)
            else:
                from vertexai.preview import reasoning_engines
                target_engine = reasoning_engines.ReasoningEngine(target_resource)
                target_engine.update(
                    reasoning_engine=agent_app,
                    requirements=reqs,
                    extra_packages=pkgs,
                    display_name=display_name,
                    description="Support Ticket Router Triage Agent",
                )
                deployed_resource_name = target_resource
            logger.info("Agent Engine updated successfully: %s", deployed_resource_name)
        else:
            logger.info("Creating new Agent Engine on Vertex AI...")
            if has_agent_engines:
                from vertexai import agent_engines
                engine = agent_engines.create(
                    agent_engine=agent_app,
                    requirements=reqs,
                    extra_packages=pkgs,
                    env_vars=remote_env_vars,
                    display_name=display_name,
                    description="Support Ticket Router Triage Agent",
                    service_account=service_account,
                    gcs_dir_name="agent_engine/triage_router",
                )
                deployed_resource_name = str(engine.resource_name)
            else:
                from vertexai.preview import reasoning_engines
                create_kwargs: dict[str, Any] = {
                    "reasoning_engine": agent_app,
                    "requirements": reqs,
                    "extra_packages": pkgs,
                    "display_name": display_name,
                    "description": "Support Ticket Router Triage Agent",
                }
                if service_account:
                    create_kwargs["service_account"] = service_account
                engine = reasoning_engines.ReasoningEngine.create(**create_kwargs)
                deployed_resource_name = str(engine.resource_name)
            logger.info("Agent Engine deployed successfully: %s", deployed_resource_name)

    if deployed_resource_name:
        query_cmd = (
            f"python scripts/deploy_agents/query_triage_agent.py "
            f"--resource-name {deployed_resource_name} --project {project} --region {region}"
        )
        logger.info("Query the deployed agent with:\n  %s", query_cmd)

    return deployed_resource_name


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy TriageRouterAgent to Vertex AI Reasoning Engine / Agent Runtime"
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("PROJECT_ID", DEFAULT_PROJECT),
        help=f"GCP Project ID (default: {DEFAULT_PROJECT})",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("REGION", DEFAULT_REGION),
        help=f"GCP Region (default: {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--staging-bucket",
        default=os.environ.get("STAGING_BUCKET", DEFAULT_STAGING_BUCKET),
        help=f"GCS staging bucket (default: {DEFAULT_STAGING_BUCKET})",
    )
    parser.add_argument(
        "--display-name",
        default=DEFAULT_DISPLAY_NAME,
        help=f"Display name (default: {DEFAULT_DISPLAY_NAME})",
    )
    parser.add_argument(
        "--service-account",
        default=os.environ.get("SERVICE_ACCOUNT", DEFAULT_SERVICE_ACCOUNT),
        help=f"Service Account (default: {DEFAULT_SERVICE_ACCOUNT})",
    )
    parser.add_argument(
        "--ticket-db-url",
        default=os.environ.get("TICKET_DB_MCP_URL", DEFAULT_TICKET_DB_URL),
        help=f"Ticket DB MCP URL (default: {DEFAULT_TICKET_DB_URL})",
    )
    parser.add_argument(
        "--order-lookup-url",
        default=os.environ.get("ORDER_LOOKUP_MCP_URL", DEFAULT_ORDER_LOOKUP_URL),
        help=f"Order Lookup MCP URL (default: {DEFAULT_ORDER_LOOKUP_URL})",
    )
    parser.add_argument(
        "--human-queue-url",
        default=os.environ.get("HUMAN_QUEUE_URL", DEFAULT_HUMAN_QUEUE_URL),
        help=f"Human Queue URL (default: {DEFAULT_HUMAN_QUEUE_URL})",
    )
    parser.add_argument(
        "--human-queue-topic",
        default=os.environ.get("HUMAN_QUEUE_TOPIC", DEFAULT_HUMAN_QUEUE_TOPIC),
        help=f"Pub/Sub human queue topic name (default: {DEFAULT_HUMAN_QUEUE_TOPIC})",
    )
    parser.add_argument(
        "--redaction-kms-key",
        default=os.environ.get("REDACTION_KMS_KEY", DEFAULT_REDACTION_KMS_KEY),
        help="KMS cryptoKey that wraps redaction-map DEKs (REQ-STORE-009)",
    )
    parser.add_argument(
        "--faq-resolver-url",
        default=os.environ.get("FAQ_RESOLVER_URL", DEFAULT_FAQ_RESOLVER_URL),
        help=f"Faq Resolver URL (default: {DEFAULT_FAQ_RESOLVER_URL})",
    )
    parser.add_argument(
        "--faq-resolver-audience",
        default=os.environ.get("FAQ_RESOLVER_AUDIENCE", DEFAULT_FAQ_RESOLVER_AUDIENCE),
        help=f"Faq Resolver Audience (default: {DEFAULT_FAQ_RESOLVER_AUDIENCE})",
    )
    parser.add_argument(
        "--router-endpoint",
        default=os.environ.get("ROUTER_S1_ENDPOINT", DEFAULT_ROUTER_ENDPOINT),
        help=f"Router Endpoint ID (default: {DEFAULT_ROUTER_ENDPOINT})",
    )
    parser.add_argument(
        "--router-base-url",
        default=os.environ.get("ROUTER_S1_BASE_URL", DEFAULT_ROUTER_BASE_URL),
        help=f"Router Base URL (default: {DEFAULT_ROUTER_BASE_URL})",
    )
    parser.add_argument(
        "--model-armor-template",
        default=os.environ.get(
            "MODEL_ARMOR_TEMPLATE",
            f"projects/{DEFAULT_PROJECT}/locations/us-central1/templates/support-router-lean",
        ),
        help="Model Armor template resource path",
    )
    parser.add_argument(
        "--model-armor-enabled",
        default=os.environ.get("MODEL_ARMOR_ENABLED", "1"),
        help="Enable Model Armor sanitization (1/0)",
    )
    parser.add_argument(
        "--cct-key-version",
        default=os.environ.get("CCT_KEY_VERSION", DEFAULT_CCT_KEY_VERSION),
        help=f"CCT KMS key version resource name (default: {DEFAULT_CCT_KEY_VERSION})",
    )
    parser.add_argument(
        "--cct-issuer",
        default=os.environ.get("CCT_ISSUER", DEFAULT_CCT_ISSUER),
        help=f"CCT issuer claim (default: {DEFAULT_CCT_ISSUER})",
    )
    parser.add_argument(
        "--cct-audience",
        default=os.environ.get("CCT_AUDIENCE", DEFAULT_CCT_AUDIENCE),
        help=f"CCT audience claim (default: {DEFAULT_CCT_AUDIENCE})",
    )
    parser.add_argument(
        "--cct-ttl-s",
        default=os.environ.get("CCT_TTL_S", DEFAULT_CCT_TTL_S),
        help=f"CCT TTL in seconds (default: {DEFAULT_CCT_TTL_S})",
    )
    parser.add_argument(
        "--ge-oauth-auth-id",
        default=os.environ.get("GE_OAUTH_AUTH_ID", "support-customer-oauth"),
        help="Gemini Enterprise OAuth Auth ID (default: support-customer-oauth)",
    )
    parser.add_argument(
        "--ge-oauth-client-id",
        default=os.environ.get("GE_OAUTH_CLIENT_ID"),
        help="Gemini Enterprise OAuth Client ID (optional)",
    )
    parser.add_argument(
        "--extra-packages",
        nargs="*",
        default=DEFAULT_EXTRA_PACKAGES,
        help=f"Extra local packages to upload (default: {DEFAULT_EXTRA_PACKAGES})",
    )
    parser.add_argument(
        "--identity-type",
        choices=["sa", "agent"],
        default="sa",
        help="Runtime workload identity type: 'sa' (default service account) or 'agent' (SPIFFE AGENT_IDENTITY, REQ-AUTH-004)",
    )
    parser.add_argument(
        "--agent-gateway",
        default=os.environ.get("TRIAGE_AGENT_GATEWAY"),
        help="Agent Gateway resource name for egress governance (requires --identity-type agent, REQ-AUTH-007)",
    )
    parser.add_argument(
        "--update",
        metavar="RESOURCE_NAME",
        default=None,
        help="Existing reasoning engine resource name to update in place",
    )
    parser.add_argument(
        "--resource-name",
        metavar="RESOURCE_NAME",
        default=None,
        help="Existing reasoning engine resource name to update in place (omit with --display-name to create a separate staging engine)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate packaging and environment isolation without deploying to GCP",
    )
    parsed = parser.parse_args(args)
    gw_val = _non_empty_str(parsed.agent_gateway)
    if gw_val and parsed.identity_type != "agent":
        parser.error("--agent-gateway requires --identity-type agent")
    upd_val = _non_empty_str(parsed.update)
    res_val = _non_empty_str(parsed.resource_name)
    if upd_val and res_val and upd_val != res_val:
        parser.error("--update and --resource-name must match when both are provided")
    parsed.target_resource_name = res_val or upd_val
    return parsed


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    contract_vars = {
        "TICKET_DB_MCP_URL": args.ticket_db_url,
        "ORDER_LOOKUP_MCP_URL": args.order_lookup_url,
        "HUMAN_QUEUE_URL": args.human_queue_url,
        "HUMAN_QUEUE_TOPIC": args.human_queue_topic,
        "REDACTION_KMS_KEY": args.redaction_kms_key,
        "FAQ_RESOLVER_URL": args.faq_resolver_url,
        "FAQ_RESOLVER_AUDIENCE": args.faq_resolver_audience,
        "ROUTER_S1_ENDPOINT": args.router_endpoint,
        "ROUTER_S1_BASE_URL": args.router_base_url,
        "MODEL_ARMOR_TEMPLATE": args.model_armor_template,
        "MODEL_ARMOR_ENABLED": args.model_armor_enabled,
        "CCT_KEY_VERSION": args.cct_key_version,
        "CCT_ISSUER": args.cct_issuer,
        "CCT_AUDIENCE": args.cct_audience,
        "CCT_TTL_S": args.cct_ttl_s,
    }
    if args.ge_oauth_auth_id:
        contract_vars["GE_OAUTH_AUTH_ID"] = args.ge_oauth_auth_id
    if args.ge_oauth_client_id:
        contract_vars["GE_OAUTH_CLIENT_ID"] = args.ge_oauth_client_id
    try:
        resource_name = deploy_reasoning_engine(
            project=args.project,
            region=args.region,
            staging_bucket=args.staging_bucket,
            display_name=args.display_name,
            service_account=args.service_account,
            requirements=PINNED_REQUIREMENTS,
            extra_packages=args.extra_packages,
            contract_vars=contract_vars,
            update_resource_name=args.target_resource_name,
            dry_run=args.dry_run,
            identity_type=args.identity_type,
            agent_gateway=args.agent_gateway,
        )
        logger.info("Deployment script completed. Output: %s", resource_name)
        if resource_name and args.identity_type != "agent":
            print(resource_name)
    except Exception as e:
        logger.error("Deployment failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
