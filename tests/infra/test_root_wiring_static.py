"""Static policy and wiring tests for single-project root Terraform configuration.

# REQ-AGENT-031: Escalation Event Consumer (Pub/Sub push -> human-queue)
# REQ-STORE-009: Reversible PII Tokens and KMS Envelope-Encrypted Redaction Maps
# REQ-STORE-010: Redaction Map 30-Day TTL Purge
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SINGLE_PROJECT_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"


# REQ-AGENT-031
def test_root_pubsub_push_wiring_static():
    """Verify root main.tf configures pubsub module with push_endpoint."""
    main_tf = (SINGLE_PROJECT_DIR / "main.tf").read_text(encoding="utf-8")
    vars_tf = (SINGLE_PROJECT_DIR / "variables.tf").read_text(encoding="utf-8")
    tfvars = (SINGLE_PROJECT_DIR / "agents.auto.tfvars").read_text(encoding="utf-8")

    # 1. Root variable human_queue_url exists with expected Cloud Run URL default
    assert 'variable "human_queue_url"' in vars_tf
    assert "https://human-queue-34mwh7k34a-uc.a.run.app" in vars_tf

    # 2. agents.auto.tfvars defines human_queue_url
    assert 'human_queue_url                  = "https://human-queue-34mwh7k34a-uc.a.run.app"' in tfvars or 'human_queue_url' in tfvars

    # 3. module pubsub passes push_endpoint targeting /pubsub/escalations
    assert 'module "pubsub"' in main_tf
    assert 'push_endpoint = "${var.human_queue_url}/pubsub/escalations"' in main_tf

    # 4. module cloud_run_services passes push_audience and sa_queue_push_email
    assert 'module "cloud_run_services"' in main_tf
    assert re.search(r'push_audience\s+=\s+"\$\{var\.human_queue_url\}/pubsub/escalations"', main_tf)
    assert re.search(r"sa_queue_push_email\s+=\s+module\.pubsub\.sa_queue_push_email", main_tf)


# REQ-STORE-009
# REQ-STORE-010
def test_root_kms_and_purge_wiring_static():
    """Verify root main.tf wires redaction KMS keys, IAM members, and purge module."""
    main_tf = (SINGLE_PROJECT_DIR / "main.tf").read_text(encoding="utf-8")
    outputs_tf = (SINGLE_PROJECT_DIR / "outputs.tf").read_text(encoding="utf-8")

    # 1. module kms_identity configures redaction encrypters and decrypters
    assert 'module "kms_identity"' in main_tf
    assert "redaction_encrypter_members = compact([" in main_tf
    assert "module.agents_iam.sa_triage_email" in main_tf
    assert "var.triage_agent_principal" in main_tf
    assert 'redaction_decrypter_members = ["serviceAccount:${module.cloud_sql.sa_queue_email}"]' in main_tf

    # 2. module redaction_purge is instantiated
    assert 'module "redaction_purge"' in main_tf
    assert 'source                   = "./modules/redaction-purge"' in main_tf
    assert "human_queue_url          = var.human_queue_url" in main_tf
    assert 'human_queue_service_name = "human-queue"' in main_tf

    # 3. Root outputs expose redaction KMS key ID and purge scheduler info
    assert 'output "redaction_map_wrapper_key_id"' in outputs_tf
    assert "module.kms_identity.redaction_map_wrapper_key_id" in outputs_tf
    assert 'output "redaction_purge_scheduler_job_id"' in outputs_tf
    assert 'output "redaction_purge_sa_email"' in outputs_tf


# REQ-AGENT-031
# REQ-STORE-010
def test_root_variables_and_tfvars_hygiene():
    """Verify root variables and tfvars style, defaults, and commented future variables."""
    vars_tf = (SINGLE_PROJECT_DIR / "variables.tf").read_text(encoding="utf-8")
    tfvars = (SINGLE_PROJECT_DIR / "agents.auto.tfvars").read_text(encoding="utf-8")

    # 1. faq_agent_principal variable declared with safe empty default
    assert 'variable "faq_agent_principal"' in vars_tf
    assert 'default     = ""' in vars_tf

    # 2. agents.auto.tfvars includes commented-out faq_agent_principal for future GKE upgrade
    assert "faq_agent_principal" in tfvars
