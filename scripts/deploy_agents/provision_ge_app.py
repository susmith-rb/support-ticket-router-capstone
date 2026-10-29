#!/usr/bin/env python3
"""Provision and verify Gemini Enterprise Web App front-door and ADK agent registration.

# REQ-AGENT-023: Gemini Enterprise Web App Front-Door and ADK Agent Registration.
Automates Discovery Engine v1alpha surfaces absent from hashicorp/google v6.50.0:
- Location aclConfig (idpType = "GSUITE")
- APP_TYPE_INTRANET search engine (support-assistant-app)
- ADK reasoning engine registration (TriageRouterAgent)
- widgetConfigs/default_search_widget_config resolution for direct chat URL

Zero gcloud subprocess calls; uses google.auth ADC or GOOGLE_OAUTH_ACCESS_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import requests

logger = logging.getLogger("provision_ge_app")

DEFAULT_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "susmith-fde-ticket-router")
DEFAULT_PROJECT_NUMBER = os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER", "620485621180")
DEFAULT_LOCATION = "global"
DEFAULT_DATA_STORE_ID = "support-ge-default-ds"
DEFAULT_ENGINE_ID = "support-assistant-app"
DEFAULT_KB_ENGINE_ID = "support-kb-search"
DEFAULT_REASONING_ENGINE = (
    "projects/620485621180/locations/us-central1/reasoningEngines/8384262420809383936"
)
DEFAULT_DISPLAY_NAME = "Global Retail Hub Support Assistant"
DEFAULT_COMPANY_NAME = "Global Retail Hub"
DEFAULT_AGENT_DISPLAY_NAME = "Support Ticket Router & Assistant"
DEFAULT_LICENSE_CONFIG_ID = "free_trial_gemini"
DEFAULT_USER_PRINCIPAL = os.environ.get("GE_USER_PRINCIPAL", "susmith@gcp.altostrat.com")


def resolve_token(
    token: Optional[str] = None,
    token_provider: Optional[Callable[[], str]] = None,
) -> str:
    """Resolve a bearer access token without running gcloud subprocesses."""
    if token:
        return token
    if token_provider:
        return token_provider()
    env_token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
    if env_token:
        return env_token
    from ticket_router.common.auth import get_access_token

    return get_access_token()


def get_base_url(location: str) -> str:
    """Return the Discovery Engine v1alpha base URL."""
    host = (
        "discoveryengine.googleapis.com"
        if location == "global"
        else f"{location}-discoveryengine.googleapis.com"
    )
    return f"https://{host}/v1alpha"


def get_headers(project: str, token: str) -> Dict[str, str]:
    """Return mandatory request headers for Discovery Engine API calls."""
    return {
        "Authorization": f"Bearer {token}",
        "X-Goog-User-Project": project,
        "Content-Type": "application/json",
    }


def ensure_acl_config(
    project: str,
    location: str,
    idp_type: str = "GSUITE",
    session: Optional[requests.Session] = None,
    token: Optional[str] = None,
    token_provider: Optional[Callable[[], str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Ensure location aclConfig is configured with idpType.

    # REQ-AGENT-023: Location aclConfig automation.
    """
    base = get_base_url(location)
    acl_url = f"{base}/projects/{project}/locations/{location}/aclConfig"

    if dry_run:
        return {
            "status": "dry_run",
            "action": "ensure_acl_config",
            "project": project,
            "location": location,
            "idpType": idp_type,
            "planned_request": f"GET/PATCH {acl_url}",
        }

    token_val = resolve_token(token, token_provider)
    headers = get_headers(project, token_val)
    http = session or requests.Session()

    resp = http.get(acl_url, headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        if data.get("idpConfig", {}).get("idpType") == idp_type:
            return {"status": "already_configured", "aclConfig": data}

    patch_payload = {
        "name": f"projects/{project}/locations/{location}/aclConfig",
        "idpConfig": {"idpType": idp_type},
    }
    patch_resp = http.patch(acl_url, headers=headers, json=patch_payload)
    patch_resp.raise_for_status()
    return {"status": "configured", "aclConfig": patch_resp.json()}


def ensure_user_license(
    project: str,
    location: str,
    user_principal: str = DEFAULT_USER_PRINCIPAL,
    license_config_id: str = DEFAULT_LICENSE_CONFIG_ID,
    project_number: str = DEFAULT_PROJECT_NUMBER,
    session: Optional[requests.Session] = None,
    token: Optional[str] = None,
    token_provider: Optional[Callable[[], str]] = None,
    dry_run: bool = False,
    poll_interval_s: float = 2.0,
    timeout_s: float = 120.0,
) -> Dict[str, Any]:
    """Ensure active Gemini Enterprise LicenseConfig, UserStore auto-register, and user seat assignment.

    # REQ-AGENT-023: Prevent FAILED_PRECONDITION ("active Gemini Enterprise license is not available")
    # during AgentService.CreateAgent and first browser login (NO_LICENSE_ATTEMPTED_LOGIN).
    """
    base = get_base_url(location)
    lic_list_url = f"{base}/projects/{project}/locations/{location}/licenseConfigs"
    user_store_url = f"{base}/projects/{project}/locations/{location}/userStores/default_user_store"

    if dry_run:
        return {
            "status": "dry_run",
            "action": "ensure_user_license",
            "project": project,
            "location": location,
            "user_principal": user_principal,
            "license_config_id": license_config_id,
            "planned_requests": [
                f"GET/POST {lic_list_url}",
                f"PATCH {user_store_url}",
                f"POST {user_store_url}:batchUpdateUserLicenses",
            ],
        }

    token_val = resolve_token(token, token_provider)
    headers = get_headers(project, token_val)
    http = session or requests.Session()

    # 1. Inspect UserStore to discover canonical project number
    us_resp = http.get(user_store_url, headers=headers)
    us_resp.raise_for_status()
    us_data = us_resp.json()
    us_name = us_data.get("name", "")
    # projects/<PROJECT_NUMBER>/locations/<loc>/userStores/default_user_store
    parts = us_name.split("/")
    resolved_proj_num = parts[1] if len(parts) >= 2 and parts[0] == "projects" and parts[1].isdigit() else project_number

    # 2. Ensure active LicenseConfig exists
    lic_resp = http.get(lic_list_url, headers=headers)
    lic_resp.raise_for_status()
    lic_items = lic_resp.json().get("licenseConfigs", [])

    active_lic_name: Optional[str] = None
    for item in lic_items:
        state = item.get("state", "ACTIVE")
        if state in ("ACTIVE", "STATE_UNSPECIFIED", ""):
            active_lic_name = item.get("name")
            break

    lic_status = "exists"
    if not active_lic_name:
        now = datetime.now(timezone.utc)
        create_lic_url = f"{lic_list_url}?licenseConfigId={license_config_id}"
        lic_payload = {
            "licenseCount": 50,
            "subscriptionTier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT",
            "subscriptionTerm": "SUBSCRIPTION_TERM_ONE_MONTH",
            "freeTrial": True,
            "geminiBundle": True,
            "startDate": {"year": now.year, "month": now.month, "day": now.day},
        }
        post_lic = http.post(create_lic_url, headers=headers, json=lic_payload)
        post_lic.raise_for_status()
        created_lic = post_lic.json()
        if "operations/" in created_lic.get("name", "") and not created_lic.get("done", True):
            op_name = created_lic["name"]
            op_url = op_name if op_name.startswith("http") else f"{base}/{op_name}"
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                time.sleep(poll_interval_s)
                op_r = http.get(op_url, headers=headers)
                op_r.raise_for_status()
                op_d = op_r.json()
                if op_d.get("done", False):
                    if "error" in op_d:
                        raise RuntimeError(f"CreateLicenseConfig LRO failed: {op_d['error']}")
                    created_lic = op_d.get("response", created_lic)
                    break
        active_lic_name = (
            created_lic.get("name")
            if created_lic.get("name", "").endswith(f"/licenseConfigs/{license_config_id}")
            else f"projects/{resolved_proj_num}/locations/{location}/licenseConfigs/{license_config_id}"
        )
        lic_status = "created"

    # Normalize licenseConfig resource name to project number (required by batchUpdateUserLicenses)
    if active_lic_name and f"projects/{project}/" in active_lic_name:
        active_lic_name = active_lic_name.replace(f"projects/{project}/", f"projects/{resolved_proj_num}/", 1)

    # 3. Enable auto-license registration on default_user_store so org evaluators auto-receive a seat on login
    if not us_data.get("enableLicenseAutoRegister") or us_data.get("defaultLicenseConfig") != active_lic_name:
        patch_us_url = (
            f"{user_store_url}?updateMask=defaultLicenseConfig,enableLicenseAutoRegister,enableExpiredLicenseAutoUpdate"
        )
        patch_us_payload = {
            "name": us_name or f"projects/{resolved_proj_num}/locations/{location}/userStores/default_user_store",
            "defaultLicenseConfig": active_lic_name,
            "enableLicenseAutoRegister": True,
            "enableExpiredLicenseAutoUpdate": True,
        }
        patch_us_resp = http.patch(patch_us_url, headers=headers, json=patch_us_payload)
        if not patch_us_resp.ok:
            logger.warning("UserStore auto-register patch returned %s: %s", patch_us_resp.status_code, patch_us_resp.text)

    # 4. Ensure operator user_principal has an ASSIGNED seat
    ul_list_url = f"{user_store_url}/userLicenses"
    ul_resp = http.get(ul_list_url, headers=headers)
    ul_resp.raise_for_status()
    for ul in ul_resp.json().get("userLicenses", []):
        if ul.get("userPrincipal", "").lower() == user_principal.lower() and ul.get("licenseAssignmentState") == "ASSIGNED":
            return {
                "status": "already_assigned",
                "license_status": lic_status,
                "license_config": active_lic_name,
                "user_principal": user_principal,
            }

    batch_url = f"{user_store_url}:batchUpdateUserLicenses"
    batch_payload = {
        "inlineSource": {
            "userLicenses": [
                {
                    "userPrincipal": user_principal,
                    "licenseConfig": active_lic_name,
                }
            ]
        },
        "deleteUnassignedUserLicenses": False,
    }
    batch_resp = http.post(batch_url, headers=headers, json=batch_payload)
    batch_resp.raise_for_status()
    batch_data = batch_resp.json()
    if not batch_data.get("done", True) and "name" in batch_data:
        op_name = batch_data["name"]
        op_url = op_name if op_name.startswith("http") else f"{base}/{op_name}"
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(poll_interval_s)
            op_r = http.get(op_url, headers=headers)
            op_r.raise_for_status()
            op_d = op_r.json()
            if op_d.get("done", False):
                if "error" in op_d:
                    raise RuntimeError(f"batchUpdateUserLicenses LRO failed: {op_d['error']}")
                break

    return {
        "status": "assigned",
        "license_status": lic_status,
        "license_config": active_lic_name,
        "user_principal": user_principal,
    }


def ensure_ge_engine(
    project: str,
    location: str,
    engine_id: str = DEFAULT_ENGINE_ID,
    data_store_id: str = DEFAULT_DATA_STORE_ID,
    display_name: str = DEFAULT_DISPLAY_NAME,
    company_name: str = DEFAULT_COMPANY_NAME,
    session: Optional[requests.Session] = None,
    token: Optional[str] = None,
    token_provider: Optional[Callable[[], str]] = None,
    dry_run: bool = False,
    poll_interval_s: float = 3.0,
    timeout_s: float = 180.0,
) -> Dict[str, Any]:
    """Ensure Gemini Enterprise search engine exists with APP_TYPE_INTRANET.

    # REQ-AGENT-023: Provision APP_TYPE_INTRANET engine with enterprise tier.
    """
    base = get_base_url(location)
    engine_url = (
        f"{base}/projects/{project}/locations/{location}/collections/default_collection/engines/{engine_id}"
    )

    if dry_run:
        return {
            "status": "dry_run",
            "action": "ensure_ge_engine",
            "engine_id": engine_id,
            "location": location,
            "data_store_id": data_store_id,
            "appType": "APP_TYPE_INTRANET",
            "searchTier": "SEARCH_TIER_ENTERPRISE",
            "searchAddOns": ["SEARCH_ADD_ON_LLM"],
            "planned_request": f"GET {engine_url} (or POST if 404)",
        }

    token_val = resolve_token(token, token_provider)
    headers = get_headers(project, token_val)
    http = session or requests.Session()

    resp = http.get(engine_url, headers=headers)
    if resp.status_code == 200:
        engine_data = resp.json()
        actual_app_type = engine_data.get("appType")
        if actual_app_type != "APP_TYPE_INTRANET":
            raise ValueError(
                f"Engine {engine_id} exists but has unexpected appType: {actual_app_type!r}, "
                "expected 'APP_TYPE_INTRANET'"
            )
        return {"status": "exists", "engine": engine_data}

    if resp.status_code != 404:
        resp.raise_for_status()

    post_url = (
        f"{base}/projects/{project}/locations/{location}/collections/default_collection/engines?engineId={engine_id}"
    )
    payload = {
        "displayName": display_name,
        "solutionType": "SOLUTION_TYPE_SEARCH",
        "industryVertical": "GENERIC",
        "appType": "APP_TYPE_INTRANET",
        "dataStoreIds": [data_store_id],
        "commonConfig": {"companyName": company_name},
        "searchEngineConfig": {
            "searchTier": "SEARCH_TIER_ENTERPRISE",
            "searchAddOns": ["SEARCH_ADD_ON_LLM"],
        },
    }
    post_resp = http.post(post_url, headers=headers, json=payload)
    post_resp.raise_for_status()
    lro_data = post_resp.json()

    if not lro_data.get("done", False) and "name" in lro_data:
        op_name = lro_data["name"]
        op_url = op_name if op_name.startswith("http") else f"{base}/{op_name}"
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(poll_interval_s)
            op_resp = http.get(op_url, headers=headers)
            op_resp.raise_for_status()
            current_lro = op_resp.json()
            if current_lro.get("done", False):
                if "error" in current_lro:
                    raise RuntimeError(f"Engine creation LRO failed: {current_lro['error']}")
                break
        else:
            raise TimeoutError(f"Engine creation timed out after {timeout_s}s: {op_name}")

    created_resp = http.get(engine_url, headers=headers)
    created_resp.raise_for_status()
    return {"status": "created", "engine": created_resp.json()}


def register_adk_agent(
    project: str,
    location: str,
    engine_id: str = DEFAULT_ENGINE_ID,
    reasoning_engine: str = DEFAULT_REASONING_ENGINE,
    display_name: str = DEFAULT_AGENT_DISPLAY_NAME,
    session: Optional[requests.Session] = None,
    token: Optional[str] = None,
    token_provider: Optional[Callable[[], str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Register or update TriageRouter ADK reasoning engine in Gemini Enterprise.

    # REQ-AGENT-023: ADK agent registration with starter prompts and org sharing.
    """
    base = get_base_url(location)
    agents_url = (
        f"{base}/projects/{project}/locations/{location}/collections/default_collection/engines/{engine_id}/"
        "assistants/default_assistant/agents"
    )

    if dry_run:
        return {
            "status": "dry_run",
            "action": "register_adk_agent",
            "engine_id": engine_id,
            "reasoning_engine": reasoning_engine,
            "display_name": display_name,
            "agent_id": "dry-run-agent-id",
            "planned_request": f"GET {agents_url} and POST/PATCH",
        }

    token_val = resolve_token(token, token_provider)
    headers = get_headers(project, token_val)
    http = session or requests.Session()

    resp = http.get(agents_url, headers=headers)
    resp.raise_for_status()
    agents_list = resp.json().get("agents", [])

    desired_payload = {
        "displayName": display_name,
        "description": (
            "Tiered AI customer support router and grounded FAQ assistant for Global Retail Hub "
            "(handles shipping, returns/refunds, account/payment FAQs, order lookups, and urgent human escalations)."
        ),
        "adkAgentDefinition": {
            "toolSettings": {
                "toolDescription": (
                    "Route and resolve Global Retail Hub customer support inquiries, FAQ questions, "
                    "order status checks, and escalations."
                )
            },
            "provisionedReasoningEngine": {
                "reasoningEngine": reasoning_engine,
            },
        },
        "starterPrompts": [
            {"text": "What is your standard return and refund policy?"},
            {"text": "How long does standard vs express shipping take?"},
            {"text": "Someone hacked my account and placed an unauthorized charge — I need immediate help!"},
            {"text": "Where is my order ORD-10001?"},
        ],
        "sharingConfig": {"scope": "ALL_USERS"},
    }

    matched_agent = None
    for agent in agents_list:
        current_re = (
            agent.get("adkAgentDefinition", {})
            .get("provisionedReasoningEngine", {})
            .get("reasoningEngine")
        )
        if current_re == reasoning_engine or agent.get("displayName") == display_name:
            matched_agent = agent
            break

    if not matched_agent:
        post_resp = http.post(agents_url, headers=headers, json=desired_payload)
        if post_resp.status_code == 400 and "sharingConfig" in post_resp.text:
            payload_no_sharing = dict(desired_payload)
            payload_no_sharing.pop("sharingConfig", None)
            post_resp = http.post(agents_url, headers=headers, json=payload_no_sharing)
            post_resp.raise_for_status()
            created_agent = post_resp.json()
            agent_name = created_agent.get("name", "")
            patch_url = f"{base}/{agent_name}?updateMask=sharingConfig"
            patch_resp = http.patch(
                patch_url, headers=headers, json={"sharingConfig": {"scope": "ALL_USERS"}}
            )
            if patch_resp.ok:
                patch_data = patch_resp.json()
                if "name" not in patch_data and agent_name:
                    patch_data["name"] = agent_name
                created_agent = patch_data
        else:
            post_resp.raise_for_status()
            created_agent = post_resp.json()

        agent_name = created_agent.get("name", "")
        agent_id = agent_name.rsplit("/", 1)[-1]

        if created_agent.get("state") == "CONFIGURED":
            deploy_url = f"{base}/{agent_name}:deploy"
            d_resp = http.post(deploy_url, headers=headers, json={})
            if d_resp.ok:
                get_resp = http.get(f"{base}/{agent_name}", headers=headers)
                if get_resp.ok:
                    created_agent = get_resp.json()

        return {"status": "registered", "agent": created_agent, "agent_id": agent_id}

    agent_name = matched_agent.get("name", "")
    agent_id = agent_name.rsplit("/", 1)[-1]
    current_re = (
        matched_agent.get("adkAgentDefinition", {})
        .get("provisionedReasoningEngine", {})
        .get("reasoningEngine")
    )
    if current_re != reasoning_engine:
        patch_url = f"{base}/{agent_name}?updateMask=displayName,description,adkAgentDefinition,starterPrompts"
        patch_resp = http.patch(patch_url, headers=headers, json=desired_payload)
        patch_resp.raise_for_status()
        updated_agent = patch_resp.json()
        if updated_agent.get("state") == "CONFIGURED":
            deploy_url = f"{base}/{agent_name}:deploy"
            d_resp = http.post(deploy_url, headers=headers, json={})
            if d_resp.ok:
                get_resp = http.get(f"{base}/{agent_name}", headers=headers)
                if get_resp.ok:
                    updated_agent = get_resp.json()
        return {"status": "updated", "agent": updated_agent, "agent_id": agent_id}

    return {"status": "already_registered", "agent": matched_agent, "agent_id": agent_id}


def verify_ge_app(
    project: str,
    location: str,
    engine_id: str = DEFAULT_ENGINE_ID,
    kb_engine_id: str = DEFAULT_KB_ENGINE_ID,
    reasoning_engine: str = DEFAULT_REASONING_ENGINE,
    session: Optional[requests.Session] = None,
    token: Optional[str] = None,
    token_provider: Optional[Callable[[], str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Verify Gemini Enterprise Web App setup, cost isolation, and chat URLs.

    # REQ-AGENT-023: End-to-end verification and URL resolution.
    """
    console_url = (
        f"https://console.cloud.google.com/gemini-enterprise/locations/{location}/"
        f"engines/{engine_id}/overview/dashboard?project={project}"
    )

    if dry_run:
        config_id = "default_search_widget_config"
        agent_id = "dry-run-agent-id"
        direct_chat_url = f"https://vertexaisearch.cloud.google.com/home/cid/{config_id}/r/agent/{agent_id}/session/-"
        base_chat_url = f"https://vertexaisearch.cloud.google.com/home/cid/{config_id}"
        return {
            "status": "dry_run",
            "verified": True,
            "kb_engine_id": kb_engine_id,
            "kb_search_tier": "SEARCH_TIER_STANDARD",
            "engine_id": engine_id,
            "app_type": "APP_TYPE_INTRANET",
            "agent_id": agent_id,
            "reasoning_engine": reasoning_engine,
            "widget_config_id": config_id,
            "console_url": console_url,
            "direct_chat_url": direct_chat_url,
            "base_chat_url": base_chat_url,
        }

    token_val = resolve_token(token, token_provider)
    headers = get_headers(project, token_val)
    base = get_base_url(location)
    http = session or requests.Session()

    # 1. Cost isolation check on kb_engine_id
    kb_url = f"{base}/projects/{project}/locations/{location}/collections/default_collection/engines/{kb_engine_id}"
    kb_resp = http.get(kb_url, headers=headers)
    kb_resp.raise_for_status()
    kb_data = kb_resp.json()
    actual_tier = kb_data.get("searchEngineConfig", {}).get("searchTier")
    if actual_tier != "SEARCH_TIER_STANDARD":
        raise ValueError(
            f"Cost-isolation violation: engine {kb_engine_id} has tier {actual_tier!r}, "
            "expected 'SEARCH_TIER_STANDARD'"
        )

    # 2. Verify engine_id exists and is APP_TYPE_INTRANET
    engine_url = f"{base}/projects/{project}/locations/{location}/collections/default_collection/engines/{engine_id}"
    engine_resp = http.get(engine_url, headers=headers)
    engine_resp.raise_for_status()
    engine_data = engine_resp.json()
    if engine_data.get("appType") != "APP_TYPE_INTRANET":
        raise ValueError(
            f"Engine {engine_id} has appType {engine_data.get('appType')!r}, "
            "expected 'APP_TYPE_INTRANET'"
        )

    # 3. Verify registered ADK agent
    agents_url = (
        f"{base}/projects/{project}/locations/{location}/collections/default_collection/engines/{engine_id}/"
        "assistants/default_assistant/agents"
    )
    agents_resp = http.get(agents_url, headers=headers)
    agents_resp.raise_for_status()
    agents = agents_resp.json().get("agents", [])
    matched_agent = None
    for a in agents:
        re_val = (
            a.get("adkAgentDefinition", {})
            .get("provisionedReasoningEngine", {})
            .get("reasoningEngine")
        )
        if re_val == reasoning_engine:
            matched_agent = a
            break
    if not matched_agent:
        raise ValueError(
            f"No ADK agent found pointing to reasoning engine {reasoning_engine!r} in {engine_id}"
        )
    agent_id = matched_agent.get("name", "").rsplit("/", 1)[-1]

    # 4. Fetch default widget config
    widget_url = (
        f"{base}/projects/{project}/locations/{location}/collections/default_collection/engines/{engine_id}/"
        "widgetConfigs/default_search_widget_config"
    )
    widget_resp = http.get(widget_url, headers=headers)
    widget_resp.raise_for_status()
    widget_data = widget_resp.json()
    config_id = widget_data.get("configId") or widget_data.get("name", "").rsplit("/", 1)[-1]

    # 5. Construct URLs
    direct_chat_url = (
        f"https://vertexaisearch.cloud.google.com/home/cid/{config_id}/r/agent/{agent_id}/session/-"
        if config_id and agent_id
        else None
    )
    base_chat_url = (
        f"https://vertexaisearch.cloud.google.com/home/cid/{config_id}"
        if config_id
        else None
    )

    return {
        "status": "verified",
        "verified": True,
        "kb_engine_id": kb_engine_id,
        "kb_search_tier": actual_tier,
        "engine_id": engine_id,
        "app_type": engine_data.get("appType"),
        "agent_id": agent_id,
        "reasoning_engine": reasoning_engine,
        "widget_config_id": config_id,
        "console_url": console_url,
        "direct_chat_url": direct_chat_url,
        "base_chat_url": base_chat_url,
    }


def main() -> int:
    """Parse CLI arguments and run provisioning steps."""
    parser = argparse.ArgumentParser(
        description="Provision and verify Gemini Enterprise Web App front-door and ADK agent registration (REQ-AGENT-023)."
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="GCP project ID")
    parser.add_argument("--location", default=DEFAULT_LOCATION, help="Discovery Engine location (default: global)")
    parser.add_argument("--data-store-id", default=DEFAULT_DATA_STORE_ID, help="Data store ID for GE backing store")
    parser.add_argument("--engine-id", default=DEFAULT_ENGINE_ID, help="Search engine ID for GE App")
    parser.add_argument("--kb-engine-id", default=DEFAULT_KB_ENGINE_ID, help="Corpus KB search engine ID")
    parser.add_argument(
        "--reasoning-engine",
        default=DEFAULT_REASONING_ENGINE,
        help="Vertex AI Reasoning Engine resource name",
    )
    parser.add_argument("--display-name", default=DEFAULT_DISPLAY_NAME, help="Engine display name")
    parser.add_argument("--company-name", default=DEFAULT_COMPANY_NAME, help="Company name")
    parser.add_argument("--agent-display-name", default=DEFAULT_AGENT_DISPLAY_NAME, help="ADK agent display name")
    parser.add_argument("--user-principal", default=DEFAULT_USER_PRINCIPAL, help="User email for GE license assignment")
    parser.add_argument("--ensure-acl", action="store_true", help="Ensure location aclConfig is configured")
    parser.add_argument("--ensure-license", action="store_true", help="Ensure active GE LicenseConfig and user license")
    parser.add_argument("--ensure-engine", action="store_true", help="Ensure GE APP_TYPE_INTRANET engine exists")
    parser.add_argument("--register-agent", action="store_true", help="Register or update ADK agent")
    parser.add_argument("--verify", action="store_true", help="Verify GE app setup and output chat URLs")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run ensure-acl, ensure-license, ensure-engine, register-agent, and verify",
    )
    parser.add_argument("--dry-run", action="store_true", help="Dry run without making network calls")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output results as JSON")

    args = parser.parse_args()

    # Default to --all if no specific action was requested
    run_all = args.all or not (
        args.ensure_acl or args.ensure_license or args.ensure_engine or args.register_agent or args.verify
    )

    results: Dict[str, Any] = {}

    try:
        if run_all or args.ensure_acl:
            results["acl_config"] = ensure_acl_config(
                project=args.project,
                location=args.location,
                dry_run=args.dry_run,
            )

        if run_all or args.ensure_license:
            results["user_license"] = ensure_user_license(
                project=args.project,
                location=args.location,
                user_principal=args.user_principal,
                dry_run=args.dry_run,
            )

        if run_all or args.ensure_engine:
            results["ge_engine"] = ensure_ge_engine(
                project=args.project,
                location=args.location,
                engine_id=args.engine_id,
                data_store_id=args.data_store_id,
                display_name=args.display_name,
                company_name=args.company_name,
                dry_run=args.dry_run,
            )

        if run_all or args.register_agent:
            results["adk_agent"] = register_adk_agent(
                project=args.project,
                location=args.location,
                engine_id=args.engine_id,
                reasoning_engine=args.reasoning_engine,
                display_name=args.agent_display_name,
                dry_run=args.dry_run,
            )

        if run_all or args.verify:
            results["verification"] = verify_ge_app(
                project=args.project,
                location=args.location,
                engine_id=args.engine_id,
                kb_engine_id=args.kb_engine_id,
                reasoning_engine=args.reasoning_engine,
                dry_run=args.dry_run,
            )

        if args.json_output:
            print(json.dumps(results, indent=2))
        else:
            print("Gemini Enterprise Web App Provisioning Summary:")
            for step, data in results.items():
                print(f"  [{step}]: status={data.get('status')}")
            if "verification" in results:
                v = results["verification"]
                print(f"  Console URL: {v.get('console_url')}")
                print(f"  Direct Chat URL: {v.get('direct_chat_url')}")
                print(f"  Base Chat URL: {v.get('base_chat_url')}")

        return 0

    except Exception as e:
        logger.exception("Provisioning failed: %s", e)
        if args.json_output:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
