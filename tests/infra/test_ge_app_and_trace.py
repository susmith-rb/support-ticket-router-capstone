"""Tests for Gemini Enterprise Web App and Cloud Trace Terraform & CLI contracts.

# REQ-AGENT-023: Gemini Enterprise Web App Front-Door and ADK Agent Registration.
# REQ-AGENT-014: Observability: Distributed Tracing & PII-Free Structured Logging.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, List, Tuple

import pytest
import requests

from scripts.deploy_agents.provision_ge_app import (
    DEFAULT_AGENT_DISPLAY_NAME,
    DEFAULT_REASONING_ENGINE,
    ensure_acl_config,
    ensure_ge_engine,
    ensure_user_license,
    register_adk_agent,
    verify_ge_app,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"
MOD_DIR = TF_DIR / "modules"
SCRIPTS_DIR = REPO_ROOT / "scripts" / "deploy_agents"


class FakeResponse:
    """Mock requests.Response for hermetic unit testing."""

    def __init__(
        self,
        status_code: int,
        json_data: Any = None,
        text: str = "",
    ):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")
        self.ok = 200 <= status_code < 300

    def json(self) -> Any:
        if self._json_data is not None:
            return self._json_data
        raise ValueError("No JSON data in response")

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(
                f"HTTP {self.status_code}: {self.text}", response=self
            )


class FakeSession:
    """Mock requests.Session matching registered URL patterns."""

    def __init__(self) -> None:
        self.routes: List[
            Tuple[str, str, Any]
        ] = []

    def add_route(
        self,
        method: str,
        path_fragment: str,
        response_or_fn: Any,
    ) -> None:
        self.routes.append((method.upper(), path_fragment, response_or_fn))

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        matching = [
            (m, frag, resp)
            for m, frag, resp in self.routes
            if m == method.upper() and frag in url
        ]
        matching.sort(key=lambda item: len(item[1]), reverse=True)
        if matching:
            _, _, resp = matching[0]
            if callable(resp):
                return resp(method, url, **kwargs)
            return resp
        return FakeResponse(404, {"error": f"Unhandled route: {method} {url}"})

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("PATCH", url, **kwargs)


# REQ-AGENT-023
def test_ge_app_infra_and_registration_contract():
    """Verify Gemini Enterprise Web App Terraform module, cost isolation, and CLI contract."""
    ge_mod_dir = MOD_DIR / "ge-app"
    assert ge_mod_dir.is_dir(), f"Missing ge-app module: {ge_mod_dir}"

    # 1. Module file structure
    assert (ge_mod_dir / "main.tf").is_file()
    assert (ge_mod_dir / "variables.tf").is_file()
    assert (ge_mod_dir / "outputs.tf").is_file()
    assert (ge_mod_dir / "versions.tf").is_file()

    main_tf = (ge_mod_dir / "main.tf").read_text(encoding="utf-8")
    vars_tf = (ge_mod_dir / "variables.tf").read_text(encoding="utf-8")
    outputs_tf = (ge_mod_dir / "outputs.tf").read_text(encoding="utf-8")
    versions_tf = (ge_mod_dir / "versions.tf").read_text(encoding="utf-8")

    # Versions match agent-search
    assert 'required_version = ">= 1.9.0"' in versions_tf
    assert 'version = ">= 6.0.0"' in versions_tf

    # Backing store contract: NO_CONTENT, ignore_changes document_processing_config
    assert 'resource "google_discovery_engine_data_store" "ge_default"' in main_tf
    assert 'content_config              = "NO_CONTENT"' in main_tf
    assert 'solution_types              = ["SOLUTION_TYPE_SEARCH"]' in main_tf
    assert "ignore_changes = [" in main_tf
    assert "document_processing_config" in main_tf

    # Service agent IAM: roles/aiplatform.user
    assert 'resource "google_project_iam_member" "discovery_engine_aiplatform_user"' in main_tf
    assert 'role    = "roles/aiplatform.user"' in main_tf

    # Count-gated search engine
    assert 'resource "google_discovery_engine_search_engine" "support_assistant_app"' in main_tf
    assert "count             = var.manage_ge_search_engine ? 1 : 0" in main_tf
    assert 'search_tier    = "SEARCH_TIER_ENTERPRISE"' in main_tf
    assert 'search_add_ons = ["SEARCH_ADD_ON_LLM"]' in main_tf
    assert 'collection_id     = "default_collection"' in main_tf

    # Variables & Outputs
    assert "ge_data_store_id" in vars_tf
    assert "ge_engine_id" in vars_tf
    assert "manage_ge_search_engine" in vars_tf
    assert "ge_data_store_id" in outputs_tf
    assert "ge_engine_id" in outputs_tf
    assert "ge_location" in outputs_tf

    # 2. Cost isolation invariant: support-kb-search MUST remain SEARCH_TIER_STANDARD
    kb_main_tf = (MOD_DIR / "agent-search" / "main.tf").read_text(encoding="utf-8")
    root_vars_tf = (TF_DIR / "variables.tf").read_text(encoding="utf-8")
    assert 'search_tier    = var.search_tier' in kb_main_tf
    assert 'default     = "SEARCH_TIER_STANDARD"' in root_vars_tf
    assert "search_add_ons" in root_vars_tf
    assert "default     = []" in root_vars_tf

    # 3. Root wiring
    root_main = (TF_DIR / "main.tf").read_text(encoding="utf-8")
    assert 'module "ge_app"' in root_main
    assert 'manage_ge_search_engine   = var.manage_ge_search_engine' in root_main
    assert "manage_ge_search_engine" in root_vars_tf

    root_outputs = (TF_DIR / "outputs.tf").read_text(encoding="utf-8")
    assert "ge_app_data_store_id" in root_outputs
    assert "ge_app_engine_id" in root_outputs
    assert "ge_app_console_url" in root_outputs

    # 4. provision_ge_app.py CLI executable and dry-run contract
    script_path = SCRIPTS_DIR / "provision_ge_app.py"
    assert script_path.is_file()
    assert os.access(script_path, os.X_OK), "provision_ge_app.py must be executable"

    run_res = subprocess.run(
        [sys_python(), str(script_path), "--dry-run", "--all", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run_res.returncode == 0, f"Dry run failed: {run_res.stderr}"
    data = json.loads(run_res.stdout)
    assert "acl_config" in data
    assert "user_license" in data
    assert "ge_engine" in data
    assert "adk_agent" in data
    assert "verification" in data
    assert data["verification"]["verified"] is True
    assert "support-assistant-app" in data["verification"]["console_url"]
    assert "default_search_widget_config" in data["verification"]["direct_chat_url"]


# REQ-AGENT-023
def test_ensure_user_license_hermetic():
    """Verify ensure_user_license creates free_trial_gemini LicenseConfig and assigns user seat."""
    fake = FakeSession()
    fake.add_route(
        "GET",
        "/userStores/default_user_store",
        FakeResponse(200, {"name": "projects/620485621180/locations/global/userStores/default_user_store"}),
    )
    fake.add_route("GET", "/licenseConfigs", FakeResponse(200, {}))
    fake.add_route(
        "POST",
        "/licenseConfigs?licenseConfigId=free_trial_gemini",
        FakeResponse(
            200,
            {"name": "projects/620485621180/locations/global/licenseConfigs/free_trial_gemini", "state": "ACTIVE"},
        ),
    )
    fake.add_route("PATCH", "/userStores/default_user_store", FakeResponse(200, {"enableLicenseAutoRegister": True}))
    fake.add_route(
        "GET",
        "/userStores/default_user_store/userLicenses",
        FakeResponse(
            200,
            {"userLicenses": [{"userPrincipal": "susmith@gcp.altostrat.com", "licenseAssignmentState": "NO_LICENSE_ATTEMPTED_LOGIN"}]},
        ),
    )
    fake.add_route(
        "POST",
        "/userStores/default_user_store:batchUpdateUserLicenses",
        FakeResponse(200, {"done": True}),
    )

    res = ensure_user_license("proj", "global", user_principal="susmith@gcp.altostrat.com", session=fake, token="fake-token")
    assert res["status"] == "assigned"
    assert res["license_status"] == "created"
    assert "620485621180" in res["license_config"]

    # Idempotent already-assigned check
    fake_idem = FakeSession()
    fake_idem.add_route(
        "GET",
        "/userStores/default_user_store",
        FakeResponse(
            200,
            {
                "name": "projects/620485621180/locations/global/userStores/default_user_store",
                "enableLicenseAutoRegister": True,
                "defaultLicenseConfig": "projects/620485621180/locations/global/licenseConfigs/free_trial_gemini",
            },
        ),
    )
    fake_idem.add_route(
        "GET",
        "/licenseConfigs",
        FakeResponse(
            200,
            {"licenseConfigs": [{"name": "projects/620485621180/locations/global/licenseConfigs/free_trial_gemini", "state": "ACTIVE"}]},
        ),
    )
    fake_idem.add_route(
        "GET",
        "/userStores/default_user_store/userLicenses",
        FakeResponse(
            200,
            {"userLicenses": [{"userPrincipal": "susmith@gcp.altostrat.com", "licenseAssignmentState": "ASSIGNED"}]},
        ),
    )
    res2 = ensure_user_license("proj", "global", user_principal="susmith@gcp.altostrat.com", session=fake_idem, token="fake-token")
    assert res2["status"] == "already_assigned"
    assert res2["license_status"] == "exists"


# REQ-AGENT-023
def test_ensure_acl_config_hermetic():
    """Verify ensure_acl_config handles both idempotent and configuration states."""
    fake = FakeSession()
    fake.add_route("GET", "/aclConfig", FakeResponse(200, {"idpConfig": {"idpType": "GSUITE"}}))

    res1 = ensure_acl_config("proj", "global", session=fake, token="fake-token")
    assert res1["status"] == "already_configured"

    fake_patch = FakeSession()
    fake_patch.add_route("GET", "/aclConfig", FakeResponse(200, {"idpConfig": {"idpType": "OTHER"}}))
    fake_patch.add_route("PATCH", "/aclConfig", FakeResponse(200, {"idpConfig": {"idpType": "GSUITE"}}))

    res2 = ensure_acl_config("proj", "global", session=fake_patch, token="fake-token")
    assert res2["status"] == "configured"


# REQ-AGENT-023
def test_ensure_ge_engine_hermetic():
    """Verify ensure_ge_engine handles existing engine, mismatch check, and creation with LRO."""
    # 1. Existing engine
    fake_exists = FakeSession()
    fake_exists.add_route(
        "GET",
        "/engines/support-assistant-app",
        FakeResponse(200, {"appType": "APP_TYPE_INTRANET", "engineId": "support-assistant-app"}),
    )
    res1 = ensure_ge_engine("proj", "global", session=fake_exists, token="fake-token")
    assert res1["status"] == "exists"

    # 2. Mismatch appType raises ValueError
    fake_mismatch = FakeSession()
    fake_mismatch.add_route(
        "GET",
        "/engines/support-assistant-app",
        FakeResponse(200, {"appType": "APP_TYPE_SEARCH", "engineId": "support-assistant-app"}),
    )
    with pytest.raises(ValueError, match="unexpected appType"):
        ensure_ge_engine("proj", "global", session=fake_mismatch, token="fake-token")

    # 3. 404 -> POST with LRO poll -> created
    fake_create = FakeSession()
    engine_created = False

    def get_engine_handler(method: str, url: str, **kwargs: Any) -> FakeResponse:
        nonlocal engine_created
        if not engine_created:
            return FakeResponse(404, {"error": "Not Found"})
        return FakeResponse(200, {"appType": "APP_TYPE_INTRANET", "engineId": "support-assistant-app"})

    def post_engine_handler(method: str, url: str, **kwargs: Any) -> FakeResponse:
        nonlocal engine_created
        engine_created = True
        return FakeResponse(200, {"name": "operations/create-op-1", "done": False})

    fake_create.add_route("GET", "/engines/support-assistant-app", get_engine_handler)
    fake_create.add_route("POST", "/engines?engineId=support-assistant-app", post_engine_handler)
    fake_create.add_route("GET", "operations/create-op-1", FakeResponse(200, {"done": True}))

    res2 = ensure_ge_engine(
        "proj",
        "global",
        session=fake_create,
        token="fake-token",
        poll_interval_s=0.01,
    )
    assert res2["status"] == "created"


# REQ-AGENT-023
def test_register_adk_agent_hermetic():
    """Verify register_adk_agent handles creation, idempotency, and sharingConfig fallback."""
    target_re = DEFAULT_REASONING_ENGINE

    # 1. Existing matching agent
    fake_existing = FakeSession()
    fake_existing.add_route(
        "GET",
        "/assistants/default_assistant/agents",
        FakeResponse(
            200,
            {
                "agents": [
                    {
                        "name": "projects/proj/locations/global/collections/default_collection/engines/support-assistant-app/assistants/default_assistant/agents/agent-101",
                        "displayName": DEFAULT_AGENT_DISPLAY_NAME,
                        "adkAgentDefinition": {
                            "provisionedReasoningEngine": {"reasoningEngine": target_re}
                        },
                    }
                ]
            },
        ),
    )
    res1 = register_adk_agent("proj", "global", session=fake_existing, token="fake-token")
    assert res1["status"] == "already_registered"
    assert res1["agent_id"] == "agent-101"

    # 2. New registration
    fake_new = FakeSession()
    fake_new.add_route("GET", "/assistants/default_assistant/agents", FakeResponse(200, {"agents": []}))
    fake_new.add_route(
        "POST",
        "/assistants/default_assistant/agents",
        FakeResponse(
            200,
            {
                "name": "projects/proj/locations/global/collections/default_collection/engines/support-assistant-app/assistants/default_assistant/agents/agent-202",
                "state": "ACTIVE",
            },
        ),
    )
    res2 = register_adk_agent("proj", "global", session=fake_new, token="fake-token")
    assert res2["status"] == "registered"
    assert res2["agent_id"] == "agent-202"

    # 3. 400 on sharingConfig fallback
    fake_fallback = FakeSession()
    fake_fallback.add_route("GET", "/assistants/default_assistant/agents", FakeResponse(200, {"agents": []}))

    def post_fallback_handler(method: str, url: str, **kwargs: Any) -> FakeResponse:
        body = kwargs.get("json", {})
        if "sharingConfig" in body:
            return FakeResponse(400, {"error": "sharingConfig not supported on creation"}, text="sharingConfig rejected")
        return FakeResponse(
            200,
            {
                "name": "projects/proj/locations/global/collections/default_collection/engines/support-assistant-app/assistants/default_assistant/agents/agent-303",
                "state": "ACTIVE",
            },
        )

    fake_fallback.add_route("POST", "/assistants/default_assistant/agents", post_fallback_handler)
    fake_fallback.add_route("PATCH", "agent-303?updateMask=sharingConfig", FakeResponse(200, {"state": "ACTIVE"}))

    res3 = register_adk_agent("proj", "global", session=fake_fallback, token="fake-token")
    assert res3["status"] == "registered"
    assert res3["agent_id"] == "agent-303"


# REQ-AGENT-023
def test_verify_ge_app_hermetic():
    """Verify verify_ge_app validates cost isolation, engine appType, agent, and URLs."""
    fake = FakeSession()
    fake.add_route(
        "GET",
        "/engines/support-kb-search",
        FakeResponse(200, {"searchEngineConfig": {"searchTier": "SEARCH_TIER_STANDARD"}}),
    )
    fake.add_route(
        "GET",
        "/engines/support-assistant-app",
        FakeResponse(200, {"appType": "APP_TYPE_INTRANET"}),
    )
    fake.add_route(
        "GET",
        "/assistants/default_assistant/agents",
        FakeResponse(
            200,
            {
                "agents": [
                    {
                        "name": "projects/proj/locations/global/collections/default_collection/engines/support-assistant-app/assistants/default_assistant/agents/agent-999",
                        "adkAgentDefinition": {
                            "provisionedReasoningEngine": {"reasoningEngine": DEFAULT_REASONING_ENGINE}
                        },
                    }
                ]
            },
        ),
    )
    fake.add_route(
        "GET",
        "/widgetConfigs/default_search_widget_config",
        FakeResponse(200, {"configId": "cfg-12345"}),
    )

    summary = verify_ge_app("proj", "global", session=fake, token="fake-token")
    assert summary["verified"] is True
    assert summary["kb_search_tier"] == "SEARCH_TIER_STANDARD"
    assert summary["widget_config_id"] == "cfg-12345"
    assert "cfg-12345" in summary["direct_chat_url"]
    assert "agent-999" in summary["direct_chat_url"]

    # Cost-isolation violation test
    fake_expensive = FakeSession()
    fake_expensive.add_route(
        "GET",
        "/engines/support-kb-search",
        FakeResponse(200, {"searchEngineConfig": {"searchTier": "SEARCH_TIER_ENTERPRISE"}}),
    )
    with pytest.raises(ValueError, match="Cost-isolation violation"):
        verify_ge_app("proj", "global", session=fake_expensive, token="fake-token")


# REQ-AGENT-014
def test_cloud_trace_iam_and_cloud_run_env_contract():
    """Verify Cloud Trace API enablement, imports, IAM role grants, and Cloud Run env vars."""
    root_main = (TF_DIR / "main.tf").read_text(encoding="utf-8")
    root_imports = (TF_DIR / "imports.tf").read_text(encoding="utf-8")
    iam_main = (MOD_DIR / "agents-iam" / "main.tf").read_text(encoding="utf-8")
    iam_vars = (MOD_DIR / "agents-iam" / "variables.tf").read_text(encoding="utf-8")
    cr_main = (MOD_DIR / "cloud-run-services" / "main.tf").read_text(encoding="utf-8")

    # 1. API enablement and imports
    assert '"cloudtrace.googleapis.com"' in root_main
    assert '"telemetry.googleapis.com"' in root_main
    assert 'services["cloudtrace.googleapis.com"]' in root_imports
    assert 'services["telemetry.googleapis.com"]' in root_imports

    # 2. Service account bindings for roles/cloudtrace.agent
    assert 'resource "google_project_iam_member" "triage_cloudtrace_agent"' in iam_main
    assert 'resource "google_project_iam_member" "faq_cloudtrace_agent"' in iam_main
    assert 'resource "google_project_iam_member" "ticket_mcp_cloudtrace_agent"' in iam_main
    assert 'resource "google_project_iam_member" "order_mcp_cloudtrace_agent"' in iam_main
    assert 'resource "google_project_iam_member" "queue_cloudtrace_agent"' in iam_main
    assert 'resource "google_project_iam_member" "re_service_agent_cloudtrace_agent"' in iam_main

    # Role string check
    assert 'role    = "roles/cloudtrace.agent"' in iam_main
    assert "gcp-sa-aiplatform-re.iam.gserviceaccount.com" in iam_main

    # Variables passed in root main
    assert "sa_ticket_mcp_email" in iam_vars
    assert "sa_order_mcp_email" in iam_vars
    assert "sa_ticket_mcp_email              = module.cloud_sql.sa_ticket_mcp_email" in root_main
    assert "sa_order_mcp_email               = module.cloud_sql.sa_order_mcp_email" in root_main

    # 3. Cloud Run env vars on all 3 services
    assert cr_main.count('name  = "ENABLE_CLOUD_TRACE"') == 3
    assert cr_main.count('value = "1"') >= 3
    assert cr_main.count('name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"') == 3
    assert cr_main.count('value = "NO_CONTENT"') == 3


def sys_python() -> str:
    """Return python executable in venv or current interpreter."""
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    return str(venv_py) if venv_py.is_file() else "python3"
