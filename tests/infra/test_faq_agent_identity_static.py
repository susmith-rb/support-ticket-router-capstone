"""Static verification of GKE Agent Identity and Terraform grants for FaqResolver.

# REQ-AUTH-008: GKE Agent Identity for FaqResolver (Standard Cluster)
Verifies:
1. GKE Deployment (deploy/k8s/faq-resolver/deployment.yaml):
   - Metadata label registry.gke.io/functional-type: "AGENT"
   - Metadata annotation a2a-protocol.org/agent-card endpoint /.well-known/agent-card.json
   - Pod template annotations:
     - iam.gke.io/identity: "spiffe://agents.global.org-349636765856.system.id.goog/*"
     - iam.gke.io/inject-podcertificates: "true"
     - iam.gke.io/spiffe-identity-type: "agent-identity"
   - spec.serviceAccountName: ksa-faq (base manifest); ksa-agent.yaml defines ksa-faq-agent without GCP SA annotation
   - Container env:
     - FAQ_AGENT_IDENTITY == "1"
     - GOOGLE_API_ENABLE_RUNTIME_BOUND_TOKEN == "false"
2. Terraform module variables and resource grants for faq_agent_principal:
   - agents-iam: router predictionUser (standard + dedicated), ticketRouterAgentInference custom role,
     discoveryengine.viewer, pubsub.publisher, cloudtrace.agent
   - model-armor: modelarmor.user
   - cloud-run-services: run.invoker on order-lookup-mcp and ticket-db-mcp, ALLOWED_CALLER_SPIFFE_IDS env var,
     and variables for redaction_kms_key, purge_audience, purge_invoker_email
3. agents.auto.tfvars:
   - faq_agent_principal ends with /sa/ksa-faq-agent
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_DEPLOYMENT = REPO_ROOT / "deploy" / "k8s" / "faq-resolver" / "deployment.yaml"
K8S_AGENT_IDENTITY_PATCH = REPO_ROOT / "deploy" / "k8s" / "faq-resolver-patches" / "agent-identity.patch.yaml"
TF_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"
MOD_DIR = TF_DIR / "modules"


def _extract_block(text: str, header_regex: str) -> str:
    """Extract a balanced-brace HCL block matching header_regex."""
    match = re.search(header_regex + r"\s*\{", text)
    assert match is not None, f"Missing HCL block matching: {header_regex}"
    start = match.end()
    depth = 1
    idx = start
    while idx < len(text) and depth > 0:
        if text[idx] == "{":
            depth += 1
        elif text[idx] == "}":
            depth -= 1
        idx += 1
    assert depth == 0, f"Unbalanced braces in block: {header_regex}"
    return text[start : idx - 1]


# REQ-AUTH-008
def test_faq_deployment_requests_spiffe_agent_identity() -> None:
    """Verify FaqResolver GKE Deployment requests GKE Agent Identity and declares A2A agent card."""
    assert K8S_DEPLOYMENT.is_file(), f"Missing deployment manifest: {K8S_DEPLOYMENT}"
    raw_yaml = K8S_DEPLOYMENT.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw_yaml)

    assert doc.get("kind") == "Deployment"
    meta = doc.get("metadata", {})
    labels = meta.get("labels", {})
    assert labels.get("registry.gke.io/functional-type") == "AGENT"

    annotations = meta.get("annotations", {})
    agent_card_annot = annotations.get("a2a-protocol.org/agent-card", "")
    assert "/.well-known/agent-card.json" in agent_card_annot
    assert "HTTP" in agent_card_annot
    assert "8080" in agent_card_annot

    template = doc.get("spec", {}).get("template", {})
    pod_meta = template.get("metadata", {})
    pod_annotations = pod_meta.get("annotations", {})

    # The SPIFFE pod annotations are applied by CD as a strategic-merge patch only when every
    # node supports ClusterTrustBundle projection (>= 1.37.0-gke.3503000); the base manifest
    # must stay runnable on older nodes.
    assert "iam.gke.io/inject-podcertificates" not in pod_annotations
    patch = yaml.safe_load(K8S_AGENT_IDENTITY_PATCH.read_text(encoding="utf-8"))
    patch_annotations = patch["spec"]["template"]["metadata"]["annotations"]
    assert patch_annotations.get("iam.gke.io/identity") == "spiffe://agents.global.org-349636765856.system.id.goog/*"
    assert patch_annotations.get("iam.gke.io/inject-podcertificates") == "true"
    assert patch_annotations.get("iam.gke.io/spiffe-identity-type") == "agent-identity"
    patch_spec = patch["spec"]["template"]["spec"]
    assert patch_spec.get("serviceAccountName") == "ksa-faq-agent"
    patch_env = {e["name"]: e["value"] for e in patch_spec["containers"][0]["env"]}
    assert patch_env.get("FAQ_AGENT_IDENTITY") == "1"
    cd_yaml = (REPO_ROOT / "cicd" / "cloudbuild-deploy.yaml").read_text(encoding="utf-8")
    assert "faq-resolver-patches/agent-identity.patch.yaml" in cd_yaml
    assert 'MIN_NODE="v1.37.0-gke.3503000"' in cd_yaml
    # Opt-in switch (default off) plus an active revert to Workload Identity.
    assert "_FAQ_AGENT_IDENTITY: '0'" in cd_yaml
    assert '"${_FAQ_AGENT_IDENTITY}" = "1"' in cd_yaml
    assert "faq-resolver-patches/workload-identity.patch.yaml" in cd_yaml
    revert = yaml.safe_load((K8S_AGENT_IDENTITY_PATCH.parent / "workload-identity.patch.yaml").read_text(encoding="utf-8"))
    revert_annotations = revert["spec"]["template"]["metadata"]["annotations"]
    for key in patch_annotations:
        assert key in revert_annotations and revert_annotations[key] is None
    revert_spec = revert["spec"]["template"]["spec"]
    assert revert_spec.get("serviceAccountName") == "ksa-faq"
    revert_env = {e["name"]: e["value"] for e in revert_spec["containers"][0]["env"]}
    assert revert_env.get("FAQ_AGENT_IDENTITY") == "0"

    # REQ-AUTH-008: ksa-agent.yaml exists and defines ksa-faq-agent without gcp-service-account annotation
    ksa_agent_path = K8S_DEPLOYMENT.parent / "ksa-agent.yaml"
    assert ksa_agent_path.is_file(), f"Missing ksa-agent.yaml: {ksa_agent_path}"
    ksa_agent_doc = yaml.safe_load(ksa_agent_path.read_text(encoding="utf-8"))
    assert ksa_agent_doc.get("kind") == "ServiceAccount"
    assert ksa_agent_doc.get("metadata", {}).get("name") == "ksa-faq-agent"
    assert ksa_agent_doc.get("metadata", {}).get("namespace") == "faq-resolver"
    ksa_agent_annotations = ksa_agent_doc.get("metadata", {}).get("annotations") or {}
    assert "iam.gke.io/gcp-service-account" not in ksa_agent_annotations

    spec = template.get("spec", {})
    assert spec.get("serviceAccountName") == "ksa-faq"

    containers = spec.get("containers", [])
    assert len(containers) >= 1
    faq_c = next((c for c in containers if c.get("name") == "faq-resolver"), containers[0])
    env_vars = {item["name"]: item.get("value") for item in faq_c.get("env", []) if "name" in item}

    assert env_vars.get("FAQ_AGENT_IDENTITY") == "0"  # switched to "1" by the patch
    assert env_vars.get("GOOGLE_API_ENABLE_RUNTIME_BOUND_TOKEN") == "false"


# REQ-AUTH-008
def test_faq_agent_principal_terraform_variables_declared() -> None:
    """Verify faq_agent_principal is declared in owning modules with default empty string."""
    for mod_name in ("agents-iam", "model-armor", "cloud-run-services"):
        vars_tf = (MOD_DIR / mod_name / "variables.tf").read_text(encoding="utf-8")
        block = _extract_block(vars_tf, r'variable\s+"faq_agent_principal"')
        assert re.search(r"type\s*=\s*string\b", block)
        assert re.search(r'default\s*=\s*""', block)


# REQ-AUTH-008
def test_agents_iam_module_faq_agent_principal_grants() -> None:
    """Verify agents-iam module grants required permissions to faq_agent_principal."""
    main_tf = (MOD_DIR / "agents-iam" / "main.tf").read_text(encoding="utf-8")

    # 1. Prediction user on router_endpoint_id
    assert 'resource "google_vertex_ai_endpoint_iam_member" "faq_agent_prediction_user"' in main_tf
    pred_block = _extract_block(main_tf, r'resource\s+"google_vertex_ai_endpoint_iam_member"\s+"faq_agent_prediction_user"')
    assert "roles/aiplatform.predictionUser" in pred_block
    assert "var.faq_agent_principal" in pred_block

    # 2. Prediction user on dedicated_router_endpoint_id
    assert 'resource "google_vertex_ai_endpoint_iam_member" "faq_agent_dedicated_prediction_user"' in main_tf
    ded_block = _extract_block(main_tf, r'resource\s+"google_vertex_ai_endpoint_iam_member"\s+"faq_agent_dedicated_prediction_user"')
    assert "roles/aiplatform.predictionUser" in ded_block
    assert "var.faq_agent_principal" in ded_block

    # 3. Custom role ticketRouterAgentInference
    assert 'resource "google_project_iam_member" "faq_agent_inference_custom_role"' in main_tf
    inf_block = _extract_block(main_tf, r'resource\s+"google_project_iam_member"\s+"faq_agent_inference_custom_role"')
    assert "google_project_iam_custom_role.agent_inference.id" in inf_block
    assert "var.faq_agent_principal" in inf_block

    # 4. Discovery Engine viewer
    assert 'resource "google_project_iam_member" "faq_agent_discovery_engine_viewer"' in main_tf
    disc_block = _extract_block(main_tf, r'resource\s+"google_project_iam_member"\s+"faq_agent_discovery_engine_viewer"')
    assert "roles/discoveryengine.viewer" in disc_block
    assert "var.faq_agent_principal" in disc_block

    # 5. Pub/Sub human queue publisher
    assert 'resource "google_pubsub_topic_iam_member" "faq_agent_human_queue_publisher"' in main_tf
    ps_block = _extract_block(main_tf, r'resource\s+"google_pubsub_topic_iam_member"\s+"faq_agent_human_queue_publisher"')
    assert "roles/pubsub.publisher" in ps_block
    assert "var.faq_agent_principal" in ps_block

    # 6. Cloud Trace agent
    assert 'resource "google_project_iam_member" "faq_agent_cloudtrace_agent"' in main_tf
    trace_block = _extract_block(main_tf, r'resource\s+"google_project_iam_member"\s+"faq_agent_cloudtrace_agent"')
    assert "roles/cloudtrace.agent" in trace_block
    assert "var.faq_agent_principal" in trace_block

    # 7. Invariant: deleted transitional workloadIdentityUser binding on sa-faq
    assert "faq_agent_wi_user_on_sa_faq" not in main_tf


# REQ-AUTH-008
def test_model_armor_module_faq_agent_principal_grant() -> None:
    """Verify model-armor module grants roles/modelarmor.user to faq_agent_principal."""
    main_tf = (MOD_DIR / "model-armor" / "main.tf").read_text(encoding="utf-8")
    assert 'resource "google_project_iam_member" "model_armor_user_faq_agent"' in main_tf
    ma_block = _extract_block(main_tf, r'resource\s+"google_project_iam_member"\s+"model_armor_user_faq_agent"')
    assert "roles/modelarmor.user" in ma_block
    assert "var.faq_agent_principal" in ma_block


# REQ-AUTH-008 / REQ-AUTH-009
def test_cloud_run_services_module_order_lookup_spiffe_grants() -> None:
    """Verify cloud-run-services module grants run.invoker to faq_agent_principal and passes ALLOWED_CALLER_SPIFFE_IDS."""
    main_tf = (MOD_DIR / "cloud-run-services" / "main.tf").read_text(encoding="utf-8")
    vars_tf = (MOD_DIR / "cloud-run-services" / "variables.tf").read_text(encoding="utf-8")

    # Order lookup invoker grant for FaqResolver agent principal
    assert 'resource "google_cloud_run_v2_service_iam_member" "order_lookup_invoker_faq_agent"' in main_tf
    inv_block = _extract_block(main_tf, r'resource\s+"google_cloud_run_v2_service_iam_member"\s+"order_lookup_invoker_faq_agent"')
    assert "roles/run.invoker" in inv_block
    assert "var.faq_agent_principal" in inv_block

    # Ticket DB invoker grant for FaqResolver agent principal (REQ-AUTH-008 HumanHandoff)
    assert 'resource "google_cloud_run_v2_service_iam_member" "ticket_db_invoker_faq_agent"' in main_tf
    tdb_block = _extract_block(main_tf, r'resource\s+"google_cloud_run_v2_service_iam_member"\s+"ticket_db_invoker_faq_agent"')
    assert "roles/run.invoker" in tdb_block
    assert "var.faq_agent_principal" in tdb_block
    assert "google_cloud_run_v2_service.ticket_db_mcp" in tdb_block

    # Order lookup env ALLOWED_CALLER_SPIFFE_IDS
    assert "ALLOWED_CALLER_SPIFFE_IDS" in main_tf
    assert "var.allowed_caller_spiffe_ids" in main_tf

    # Redaction / Purge variables
    assert 'variable "allowed_caller_spiffe_ids"' in vars_tf
    assert 'variable "redaction_kms_key"' in vars_tf
    assert 'variable "purge_audience"' in vars_tf
    assert 'variable "purge_invoker_email"' in vars_tf


# REQ-AUTH-008
def test_faq_agent_principal_tfvars() -> None:
    """Verify faq_agent_principal in agents.auto.tfvars ends with /sa/ksa-faq-agent."""
    tfvars = (TF_DIR / "agents.auto.tfvars").read_text(encoding="utf-8")
    m = re.search(r'faq_agent_principal\s*=\s*"([^"]+)"', tfvars)
    assert m is not None, "faq_agent_principal not found in agents.auto.tfvars"
    assert m.group(1).endswith("/sa/ksa-faq-agent")

