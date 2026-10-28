"""Static analysis and verification of Model Armor Terraform, IAM, and deployment contracts.

# REQ-MA-001: Declarative Terraform Infrastructure, Template & Least-Privilege IAM
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"
MOD_DIR = TF_DIR / "modules" / "model-armor"
ROOT_MAIN = TF_DIR / "main.tf"
ROOT_OUTPUTS = TF_DIR / "outputs.tf"
K8S_DEPLOY = REPO_ROOT / "deploy" / "k8s" / "faq-resolver" / "deployment.yaml"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_agents" / "deploy_triage_agent_runtime.py"


def test_model_armor_template_and_iam():
    """Verify Model Armor Terraform module, template configuration, and least-privilege IAM."""
    assert MOD_DIR.is_dir(), f"Missing module directory: {MOD_DIR}"

    main_tf = (MOD_DIR / "main.tf").read_text(encoding="utf-8")
    vars_tf = (MOD_DIR / "variables.tf").read_text(encoding="utf-8")
    outputs_tf = (MOD_DIR / "outputs.tf").read_text(encoding="utf-8")
    versions_tf = (MOD_DIR / "versions.tf").read_text(encoding="utf-8")

    # 1. Versions & variables
    assert 'required_version = ">= 1.9.0"' in versions_tf
    assert "triage_sa_email" in vars_tf
    assert "faq_sa_email" in vars_tf
    assert "template_id" in vars_tf

    # 2. Template resource definition
    assert 'resource "google_model_armor_template" "support_router_lean"' in main_tf
    assert "location    = var.region" in main_tf
    assert "template_id = var.template_id" in main_tf

    # 3. Prompt injection and jailbreak settings
    assert "pi_and_jailbreak_filter_settings" in main_tf
    assert 'confidence_level   = "MEDIUM_AND_ABOVE"' in main_tf

    # 4. Malicious URI settings
    assert "malicious_uri_filter_settings" in main_tf

    # 5. RAI filters (4 categories at MEDIUM_AND_ABOVE)
    assert "rai_settings" in main_tf
    for cat in ["SEXUALLY_EXPLICIT", "HATE_SPEECH", "HARASSMENT", "DANGEROUS"]:
        assert f'filter_type      = "{cat}"' in main_tf

    # 6. Basic SDP settings
    assert "sdp_settings" in main_tf
    assert "basic_config" in main_tf
    assert 'filter_enforcement = "ENABLED"' in main_tf

    # 7. Least-privilege IAM grants (roles/modelarmor.user for triage and faq)
    assert 'resource "google_project_iam_member" "model_armor_user_triage"' in main_tf
    assert 'resource "google_project_iam_member" "model_armor_user_faq"' in main_tf
    assert 'role    = "roles/modelarmor.user"' in main_tf
    assert "serviceAccount:${var.triage_sa_email}" in main_tf
    assert "serviceAccount:${var.faq_sa_email}" in main_tf

    # 8. Outputs
    assert 'output "template_id"' in outputs_tf
    assert 'output "template_name"' in outputs_tf

    # 9. Root Terraform wiring
    root_main_content = ROOT_MAIN.read_text(encoding="utf-8")
    assert '"modelarmor.googleapis.com"' in root_main_content
    assert 'module "model_armor"' in root_main_content
    assert 'source          = "./modules/model-armor"' in root_main_content
    assert "triage_sa_email = module.agents_iam.sa_triage_email" in root_main_content
    assert "faq_sa_email    = module.agents_iam.sa_faq_email" in root_main_content

    # 10. Root outputs
    root_outputs_content = ROOT_OUTPUTS.read_text(encoding="utf-8")
    assert "model_armor_template_id" in root_outputs_content
    assert "model_armor_template_name" in root_outputs_content


def test_deploy_contracts_model_armor_configuration():
    """Verify runtime deployment artifacts declare Model Armor environment variables."""
    # 1. GKE FaqResolver deployment manifest
    assert K8S_DEPLOY.is_file(), f"Missing K8s deployment: {K8S_DEPLOY}"
    k8s_docs = list(yaml.safe_load_all(K8S_DEPLOY.read_text(encoding="utf-8")))
    containers = k8s_docs[0]["spec"]["template"]["spec"]["containers"]
    faq_container = next(c for c in containers if c["name"] == "faq-resolver")
    env_vars = {item["name"]: item.get("value") for item in faq_container["env"]}

    assert "MODEL_ARMOR_TEMPLATE" in env_vars
    assert "support-router-lean" in env_vars["MODEL_ARMOR_TEMPLATE"]
    assert env_vars.get("MODEL_ARMOR_ENABLED") == "1"

    # 2. TriageRouter Agent Engine deployment script
    assert DEPLOY_SCRIPT.is_file(), f"Missing deploy script: {DEPLOY_SCRIPT}"
    script_content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "MODEL_ARMOR_TEMPLATE" in script_content
    assert "MODEL_ARMOR_ENABLED" in script_content
    assert "support-router-lean" in script_content
