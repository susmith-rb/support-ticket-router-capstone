
"""Static analysis and architectural verification for Agent Gateway egress governance.

Fulfills REQ-AUTH-007:
- Verify Agent Gateway resource tr-egress definition, governedAccessPath, and registries.
- Verify IAP authz extension and REQUEST_AUTHZ policy targeting the gateway.
- Verify fail_open is false and DRY_RUN is the default enforcement mode.
- Verify Agent Registry service entries for FaqResolver (agent card <= 10 KB) and composite core-gapi.
- Verify composite core-gapi endpoint lists all required Google API and service hosts (standard + mTLS).
- Verify Unified Access Policy (IAM v3 UAP) grants egressViaIAP ONLY to the triage agent principal.
- Verify provider 7.x isolation: google/google-beta ~> 7.20 pinned only in agent-gateway, single-project stays ~> 6.50.
"""

from __future__ import annotations

import re


def _has(hcl: str, snippet: str) -> bool:
    """Whitespace-insensitive containment check (terraform fmt re-aligns `=`)."""
    def norm(s):
        return re.sub(r"\s+", " ", s)
    return norm(snippet) in norm(hcl)


import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGW_DIR = REPO_ROOT / "deploy" / "terraform" / "agent-gateway"
SINGLE_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"
AGENT_CARD_PATH = AGW_DIR / "agent-cards" / "faq-resolver.json"


REQUIRED_CORE_GAPI_HOSTS = [
    "aiplatform.googleapis.com",
    "aiplatform.mtls.googleapis.com",
    "us-central1-aiplatform.googleapis.com",
    "us-central1-aiplatform.mtls.googleapis.com",
    "aiplatform.us-central1.rep.googleapis.com",
    "modelarmor.us-central1.rep.googleapis.com",
    "modelarmor.us-central1.rep.mtls.googleapis.com",
    "cloudkms.googleapis.com",
    "cloudkms.mtls.googleapis.com",
    "pubsub.googleapis.com",
    "pubsub.mtls.googleapis.com",
    "iamcredentials.googleapis.com",
    "iamcredentials.mtls.googleapis.com",
    "cloudtrace.googleapis.com",
    "cloudtrace.mtls.googleapis.com",
    "telemetry.googleapis.com",
    "telemetry.mtls.googleapis.com",
    "logging.googleapis.com",
    "logging.mtls.googleapis.com",
    "monitoring.googleapis.com",
    "monitoring.mtls.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudresourcemanager.mtls.googleapis.com",
    "secretmanager.googleapis.com",
    "secretmanager.mtls.googleapis.com",
    "agentregistry.googleapis.com",
    "oauth2.googleapis.com",
    "sts.googleapis.com",
    "www.googleapis.com",
    "accounts.google.com",
]


# REQ-AUTH-007
def test_gateway_root_defines_registry_authz_and_uap():
    """Verify that agent-gateway root defines Agent Gateway, IAP authz extension, registry, and UAP.

    Acceptance test for REQ-AUTH-007.
    """
    assert (AGW_DIR / "main.tf").is_file(), "deploy/terraform/agent-gateway/main.tf missing"
    assert (AGW_DIR / "variables.tf").is_file(), "deploy/terraform/agent-gateway/variables.tf missing"
    assert (AGW_DIR / "versions.tf").is_file(), "deploy/terraform/agent-gateway/versions.tf missing"
    assert (AGW_DIR / "outputs.tf").is_file(), "deploy/terraform/agent-gateway/outputs.tf missing"

    main_hcl = (AGW_DIR / "main.tf").read_text(encoding="utf-8")
    vars_hcl = (AGW_DIR / "variables.tf").read_text(encoding="utf-8")
    versions_hcl = (AGW_DIR / "versions.tf").read_text(encoding="utf-8")

    # 1. Provider 7.x pinned in agent-gateway versions.tf
    assert "~> 7.20" in versions_hcl or "7." in versions_hcl
    assert 'backend "gcs"' in versions_hcl
    assert 'prefix = "terraform/state/agent-gateway"' in versions_hcl

    # 2. Agent Gateway resource tr-egress
    assert 'resource "google_network_services_agent_gateway" "tr_egress"' in main_hcl
    assert _has(main_hcl, 'name     = "tr-egress"')
    # `protocols` is deprecated in provider 7.x and must not be set on the gateway.
    assert "protocols =" not in main_hcl
    assert 'governed_access_path = "AGENT_TO_ANYWHERE"' in main_hcl
    assert "//agentregistry.googleapis.com/projects/" in main_hcl

    # 3. IAP Authz Extension: fail_open = false, DRY_RUN support, V2 policy
    assert 'resource "google_network_services_authz_extension" "iap_authz"' in main_hcl
    assert _has(main_hcl, 'service      = "iap.googleapis.com"')
    assert _has(main_hcl, "fail_open = false")
    assert 'iapPolicyVersion = "V2"' in main_hcl
    assert 'iamEnforcementMode = "DRY_RUN"' in main_hcl

    # 4. Authz Policy targeting gateway with CUSTOM / REQUEST_AUTHZ
    assert 'resource "google_network_security_authz_policy" "tr_authz_policy"' in main_hcl
    assert _has(main_hcl, 'policy_profile = "REQUEST_AUTHZ"')
    assert _has(main_hcl, 'action         = "CUSTOM"')
    assert "google_network_services_agent_gateway.tr_egress.id" in main_hcl
    assert "google_network_services_authz_extension.iap_authz.id" in main_hcl

    # 5. Agent Registry resources: FaqResolver and core-gapi
    assert 'resource "google_agent_registry_service" "faq_resolver"' in main_hcl
    assert 'resource "google_agent_registry_service" "core_gapi"' in main_hcl
    assert _has(main_hcl, 'type    = "A2A_AGENT_CARD"')

    # 6. UAP (IAM v3 Access Policy) and Project Binding
    assert 'resource "google_iam_project_access_policy" "triage_egress"' in main_hcl
    # The ACCESS-kind binding is created by bind_uap.sh (provider 7.46 lacks target.resource).
    bind_sh = (AGW_DIR / "bind_uap.sh").read_text(encoding="utf-8")
    assert '"policyKind": "ACCESS"' in bind_sh
    assert '"target": {"resource": "//cloudresourcemanager.googleapis.com/projects/' in bind_sh
    assert "tr-egress-uap" in bind_sh
    assert "iap.googleapis.com/resources.egressViaIAP" in main_hcl

    # UAP principal must ONLY be the triage agent principal
    assert "var.triage_agent_principal" in main_hcl
    assert "allUsers" not in main_hcl
    assert "allAuthenticatedUsers" not in main_hcl

    # 7. Variables check: enforcement_mode defaults to DRY_RUN
    assert 'variable "enforcement_mode"' in vars_hcl
    assert 'default     = "DRY_RUN"' in vars_hcl
    assert 'contains(["DRY_RUN", "ENFORCE"], var.enforcement_mode)' in vars_hcl


# REQ-AUTH-007
def test_provider_7x_pinned_only_in_agent_gateway_root():
    """Verify provider 7.x is isolated to agent-gateway; single-project stays ~> 6.50."""
    agw_versions = (AGW_DIR / "versions.tf").read_text(encoding="utf-8")
    single_versions = (SINGLE_DIR / "versions.tf").read_text(encoding="utf-8")

    assert "~> 7.20" in agw_versions or "7." in agw_versions
    assert "~> 6.50.0" in single_versions
    assert "7." not in single_versions


# REQ-AUTH-007
def test_uap_principal_is_only_triage_agent():
    """Verify Unified Access Policy rules grant access ONLY to the triage agent principal."""
    main_hcl = (AGW_DIR / "main.tf").read_text(encoding="utf-8")

    # Extract the clause / rules block for triage_egress
    uap_start = main_hcl.find('resource "google_iam_project_access_policy" "triage_egress"')
    assert uap_start != -1
    uap_end = main_hcl.find('resource "google_iam_projects_policy_binding"', uap_start)
    uap_block = main_hcl[uap_start:uap_end]

    # Must contain var.triage_agent_principal in principals
    assert "var.triage_agent_principal" in uap_block
    # Must not contain any hardcoded unauthorized service accounts or wildcards
    assert "serviceAccount:" not in uap_block
    assert "allUsers" not in uap_block
    assert "allAuthenticatedUsers" not in uap_block
    assert "domain:" not in uap_block


# REQ-AUTH-007
def test_core_gapi_contains_every_required_host():
    """Verify core-gapi composite endpoint lists all standard and mTLS Google API hosts."""
    main_hcl = (AGW_DIR / "main.tf").read_text(encoding="utf-8")

    for host in REQUIRED_CORE_GAPI_HOSTS:
        assert host in main_hcl, f"Missing required host {host} in core-gapi definition"

    # Dedicated hosts are referenced via variables
    assert "var.router_s1_dedicated_host" in main_hcl
    assert "var.ticket_db_mcp_host" in main_hcl


# REQ-AUTH-007
def test_faq_resolver_agent_card_validity_and_size():
    """Verify FaqResolver A2A agent card JSON is valid, <= 10 KB, and contains required interface."""
    assert AGENT_CARD_PATH.is_file(), f"Missing agent card at {AGENT_CARD_PATH}"

    raw_bytes = AGENT_CARD_PATH.read_bytes()
    assert len(raw_bytes) <= 10 * 1024, f"Agent card size {len(raw_bytes)} bytes exceeds 10 KB limit"

    card = json.loads(raw_bytes.decode("utf-8"))
    assert card.get("name") == "FaqResolverAgent"
    assert "supportedInterfaces" in card

    interfaces = card["supportedInterfaces"]
    assert isinstance(interfaces, list) and len(interfaces) > 0
    faq_iface = interfaces[0]
    assert faq_iface.get("protocolBinding") in ("HTTP+JSON", "JSONRPC") and faq_iface.get("protocolVersion")
    assert "https://faq.8-232-184-225.sslip.io/resolve" in faq_iface.get("url", "")


# REQ-AUTH-007
def test_cloudbuild_deploy_step_wires_agent_gateway():
    """Verify cicd/cloudbuild-deploy.yaml passes --agent-gateway only when substitution is non-empty."""
    deploy_yaml = (REPO_ROOT / "cicd" / "cloudbuild-deploy.yaml").read_text(encoding="utf-8")

    assert "_TRIAGE_AGENT_GATEWAY: ''" in deploy_yaml
    assert '--agent-gateway "${_TRIAGE_AGENT_GATEWAY}"' in deploy_yaml
    assert 'if [ -n "${_TRIAGE_AGENT_GATEWAY}" ]; then' in deploy_yaml
