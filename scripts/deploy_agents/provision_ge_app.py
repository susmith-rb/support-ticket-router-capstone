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
import base64
import json
import logging
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Sequence

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
DEFAULT_AUTH_ID = "support-customer-oauth"
DEFAULT_OAUTH_SECRET_ID = "ge-oauth-client-credentials"
DEFAULT_OAUTH_REDIRECT_URI = "https://vertexaisearch.cloud.google.com/oauth-redirect"
DEFAULT_OAUTH_SCOPES = ["openid", "email", "profile"]


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


def resolve_project_number(
    session: requests.Session,
    project_id: str,
    location: str = DEFAULT_LOCATION,
    token: Optional[str] = None,
    token_provider: Optional[Callable[[], str]] = None,
    fallback_project_number: str = DEFAULT_PROJECT_NUMBER,
) -> str:
    """Resolve canonical GCP project number from Discovery Engine user store or fallback."""
    base = get_base_url(location)
    user_store_url = f"{base}/projects/{project_id}/locations/{location}/userStores/default_user_store"
    token_val = resolve_token(token, token_provider)
    headers = get_headers(project_id, token_val)
    try:
        resp = session.get(user_store_url, headers=headers)
        if resp.ok:
            name = resp.json().get("name", "")
            parts = name.split("/")
            if len(parts) >= 2 and parts[0] == "projects" and parts[1].isdigit():
                return parts[1]
    except Exception as exc:
        logger.debug("Failed to resolve project number from UserStore: %s", exc)
    return os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER", fallback_project_number)


def load_oauth_client_credentials(
    session: requests.Session,
    project_id: str,
    secret_id: str = DEFAULT_OAUTH_SECRET_ID,
    client_id_override: Optional[str] = None,
    client_secret_override: Optional[str] = None,
    token: Optional[str] = None,
    token_provider: Optional[Callable[[], str]] = None,
) -> Optional[Dict[str, str]]:
    """Load OAuth client credentials with priority: overrides/env vars -> Secret Manager."""
    cid = (client_id_override or os.environ.get("GE_OAUTH_CLIENT_ID") or "").strip()
    csec = (client_secret_override or os.environ.get("GE_OAUTH_CLIENT_SECRET") or "").strip()
    if cid and csec:
        return {"client_id": cid, "client_secret": csec}

    secret_url = (
        f"https://secretmanager.googleapis.com/v1/projects/{project_id}/secrets/{secret_id}/versions/latest:access"
    )
    token_val = resolve_token(token, token_provider)
    headers = {
        "Authorization": f"Bearer {token_val}",
        "X-Goog-User-Project": project_id,
    }
    try:
        resp = session.get(secret_url, headers=headers)
        if resp.status_code in (400, 404):
            return None
        resp.raise_for_status()
        payload_data = resp.json().get("payload", {}).get("data", "")
        if not payload_data:
            return None
        decoded = base64.b64decode(payload_data).decode("utf-8")
        parsed = json.loads(decoded)
        if isinstance(parsed, dict):
            p_cid = str(parsed.get("client_id", "")).strip()
            p_csec = str(parsed.get("client_secret", "")).strip()
            if p_cid and p_csec:
                return {"client_id": p_cid, "client_secret": p_csec}
    except Exception as exc:
        logger.debug("Failed to read Secret Manager credentials: %s", exc)
        return None
    return None


def build_google_authorization_uri(
    client_id: str,
    redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI,
    scopes: Sequence[str] = DEFAULT_OAUTH_SCOPES,
) -> str:
    """Build the Google OAuth 2.0 authorization consent URI."""
    scope_str = " ".join(scopes) if isinstance(scopes, (list, tuple)) else str(scopes)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope_str,
        "include_granted_scopes": "true",
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"


def ensure_oauth_authorization(
    session: Optional[requests.Session] = None,
    project_id: str = DEFAULT_PROJECT,
    location: str = DEFAULT_LOCATION,
    auth_id: str = DEFAULT_AUTH_ID,
    secret_id: str = DEFAULT_OAUTH_SECRET_ID,
    redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI,
    project_number_override: Optional[str] = None,
    client_id_override: Optional[str] = None,
    client_secret_override: Optional[str] = None,
    token: Optional[str] = None,
    token_provider: Optional[Callable[[], str]] = None,
    dry_run: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Ensure Discovery Engine OAuth Authorization resource exists.

    # REQ-AGENT-023 / REQ-IDENT-009: End-user OAuth consent resource.
    CRITICAL SECRET SAFETY RULE: Never print or include clientSecret in summaries or logs.
    """
    http = session or requests.Session()
    project_number = project_number_override or resolve_project_number(
        http, project_id, location=location, token=token, token_provider=token_provider
    )
    canonical_name = f"projects/{project_number}/locations/{location}/authorizations/{auth_id}"

    if dry_run:
        return {
            "status": "dry_run",
            "action": "ensure_oauth_authorization",
            "project": project_id,
            "project_number": project_number,
            "location": location,
            "auth_id": auth_id,
            "authorization_name": canonical_name,
            "secret_id": secret_id,
            "redirect_uri": redirect_uri,
        }

    creds = load_oauth_client_credentials(
        session=http,
        project_id=project_id,
        secret_id=secret_id,
        client_id_override=client_id_override,
        client_secret_override=client_secret_override,
        token=token,
        token_provider=token_provider,
    )

    base = get_base_url(location)
    token_val = resolve_token(token, token_provider)
    headers = get_headers(project_id, token_val)
    auth_url = f"{base}/projects/{project_number}/locations/{location}/authorizations/{auth_id}"

    if creds is None:
        resp = http.get(auth_url, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "status": "exists",
                "authorization_name": data.get("name", canonical_name),
                "auth_id": auth_id,
            }
        return {
            "status": "skipped_no_credentials",
            "authorization_name": None,
            "auth_id": auth_id,
            "secret_id": secret_id,
        }

    payload = {
        "name": canonical_name,
        "serverSideOauth2": {
            "clientId": creds["client_id"],
            "clientSecret": creds["client_secret"],
            "authorizationUri": build_google_authorization_uri(
                creds["client_id"], redirect_uri=redirect_uri
            ),
            "tokenUri": "https://oauth2.googleapis.com/token",
            "scopes": list(DEFAULT_OAUTH_SCOPES),
        },
    }

    get_resp = http.get(auth_url, headers=headers)
    if get_resp.status_code == 404:
        post_url = f"{base}/projects/{project_number}/locations/{location}/authorizations?authorizationId={auth_id}"
        post_resp = http.post(post_url, headers=headers, json=payload)
        post_resp.raise_for_status()
        status = "created"
    elif get_resp.status_code == 200:
        patch_url = f"{auth_url}?updateMask=serverSideOauth2"
        patch_resp = http.patch(patch_url, headers=headers, json=payload)
        if patch_resp.status_code == 400 and (
            "bound" in patch_resp.text.lower()
            or "used" in patch_resp.text.lower()
            or "conflict" in patch_resp.text.lower()
        ):
            status = "exists_bound"
        else:
            patch_resp.raise_for_status()
            status = "updated"
    else:
        get_resp.raise_for_status()
        status = "exists"

    return {
        "status": status,
        "authorization_name": canonical_name,
        "auth_id": auth_id,
        "client_id": creds["client_id"],
    }


def register_adk_agent(
    project: str,
    location: str,
    engine_id: str = DEFAULT_ENGINE_ID,
    reasoning_engine: str = DEFAULT_REASONING_ENGINE,
    display_name: str = DEFAULT_AGENT_DISPLAY_NAME,
    authorization_resource_name: Optional[str] = None,
    session: Optional[requests.Session] = None,
    token: Optional[str] = None,
    token_provider: Optional[Callable[[], str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Register or update TriageRouter ADK reasoning engine in Gemini Enterprise.

    # REQ-AGENT-023: ADK agent registration with starter prompts, org sharing, and toolAuthorizations.
    """
    base = get_base_url(location)
    agents_url = (
        f"{base}/projects/{project}/locations/{location}/collections/default_collection/engines/{engine_id}/"
        "assistants/default_assistant/agents"
    )

    if dry_run:
        res = {
            "status": "dry_run",
            "action": "register_adk_agent",
            "engine_id": engine_id,
            "reasoning_engine": reasoning_engine,
            "display_name": display_name,
            "agent_id": "dry-run-agent-id",
            "planned_request": f"GET {agents_url} and POST/PATCH",
        }
        if authorization_resource_name:
            res["authorization_resource_name"] = authorization_resource_name
        return res

    token_val = resolve_token(token, token_provider)
    headers = get_headers(project, token_val)
    http = session or requests.Session()

    resp = http.get(agents_url, headers=headers)
    resp.raise_for_status()
    agents_list = resp.json().get("agents", [])

    desired_payload: Dict[str, Any] = {
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

    if authorization_resource_name:
        desired_payload["authorizationConfig"] = {
            "toolAuthorizations": [authorization_resource_name]
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
    current_auth = (
        matched_agent.get("authorizationConfig", {}).get("toolAuthorizations")
    )
    desired_auth = (
        [authorization_resource_name] if authorization_resource_name else None
    )

    re_matches = (current_re == reasoning_engine)
    auth_matches = True if authorization_resource_name is None else (current_auth == desired_auth)

    if not re_matches or not auth_matches:
        mask_fields = ["displayName", "description", "adkAgentDefinition", "starterPrompts"]
        if authorization_resource_name and not auth_matches:
            mask_fields.append("authorizationConfig")
        patch_url = f"{base}/{agent_name}?updateMask={','.join(mask_fields)}"
        patch_resp = http.patch(patch_url, headers=headers, json=desired_payload)

        if patch_resp.status_code == 400 and "authorizationConfig" in mask_fields:
            logger.warning(
                "Agent PATCH with authorizationConfig returned 400 (%s); recreating agent cleanly...",
                patch_resp.text,
            )
            del_resp = http.delete(f"{base}/{agent_name}", headers=headers)
            if del_resp.status_code not in (200, 204):
                logger.warning("Agent DELETE returned %s: %s", del_resp.status_code, del_resp.text)
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
            new_agent_id = agent_name.rsplit("/", 1)[-1]
            if created_agent.get("state") == "CONFIGURED":
                deploy_url = f"{base}/{agent_name}:deploy"
                d_resp = http.post(deploy_url, headers=headers, json={})
                if d_resp.ok:
                    get_resp = http.get(f"{base}/{agent_name}", headers=headers)
                    if get_resp.ok:
                        created_agent = get_resp.json()
            return {"status": "registered", "agent": created_agent, "agent_id": new_agent_id}

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
            "agent_authorizations": [],
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
        "agent_authorizations": matched_agent.get("authorizationConfig", {}).get(
            "toolAuthorizations", []
        ),
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
    parser.add_argument(
        "--ensure-oauth",
        action="store_true",
        help="Ensure Discovery Engine OAuth Authorization resource exists",
    )
    parser.add_argument(
        "--auth-id",
        default=DEFAULT_AUTH_ID,
        help=f"OAuth Authorization ID (default: {DEFAULT_AUTH_ID})",
    )
    parser.add_argument(
        "--oauth-secret-id",
        default=DEFAULT_OAUTH_SECRET_ID,
        help=f"Secret Manager secret ID for OAuth credentials (default: {DEFAULT_OAUTH_SECRET_ID})",
    )
    parser.add_argument(
        "--oauth-redirect-uri",
        default=DEFAULT_OAUTH_REDIRECT_URI,
        help=f"OAuth authorized redirect URI (default: {DEFAULT_OAUTH_REDIRECT_URI})",
    )
    parser.add_argument(
        "--project-number",
        default=None,
        help="GCP project number override for authorization resource name",
    )
    parser.add_argument("--register-agent", action="store_true", help="Register or update ADK agent")
    parser.add_argument("--verify", action="store_true", help="Verify GE app setup and output chat URLs")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run ensure-acl, ensure-license, ensure-engine, ensure-oauth, register-agent, and verify",
    )
    parser.add_argument("--dry-run", action="store_true", help="Dry run without making network calls")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output results as JSON")

    args = parser.parse_args()

    # Default to --all if no specific action was requested
    run_all = args.all or not (
        args.ensure_acl
        or args.ensure_license
        or args.ensure_engine
        or args.ensure_oauth
        or args.register_agent
        or args.verify
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

        oauth_res: Optional[Dict[str, Any]] = None
        if run_all or args.ensure_oauth:
            oauth_res = ensure_oauth_authorization(
                project_id=args.project,
                location=args.location,
                auth_id=args.auth_id,
                secret_id=args.oauth_secret_id,
                redirect_uri=args.oauth_redirect_uri,
                project_number_override=args.project_number,
                dry_run=args.dry_run,
            )
            results["oauth_authorization"] = oauth_res

        auth_resource_name = oauth_res.get("authorization_name") if oauth_res else None

        if run_all or args.register_agent:
            results["adk_agent"] = register_adk_agent(
                project=args.project,
                location=args.location,
                engine_id=args.engine_id,
                reasoning_engine=args.reasoning_engine,
                display_name=args.agent_display_name,
                authorization_resource_name=auth_resource_name,
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
