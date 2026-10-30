"""Static infrastructure and manifest tests for human handoff and push queue.

# REQ-AGENT-030: Shared HumanHandoff Workflow (deterministic, used by TriageRouter and FaqResolver)
# REQ-AGENT-031: Escalation Event Consumer (Pub/Sub push -> human-queue)
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TF_MODULES_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project" / "modules"
K8S_DIR = REPO_ROOT / "deploy" / "k8s"
SCRIPTS_DIR = REPO_ROOT / "scripts" / "deploy_agents"


# REQ-AGENT-031
def test_pubsub_terraform_module_push_config_static():
    """Verify Pub/Sub module defines push endpoint, sa-queue-push, and IAM bindings."""
    pubsub_dir = TF_MODULES_DIR / "pubsub"
    vars_tf = (pubsub_dir / "variables.tf").read_text(encoding="utf-8")
    main_tf = (pubsub_dir / "main.tf").read_text(encoding="utf-8")
    outputs_tf = (pubsub_dir / "outputs.tf").read_text(encoding="utf-8")

    # 1. Variable push_endpoint exists with default ""
    assert 'variable "push_endpoint"' in vars_tf
    assert 'default     = ""' in vars_tf

    # 2. Dedicated service account sa-queue-push exists
    assert 'resource "google_service_account" "sa_queue_push"' in main_tf
    assert 'account_id   = "sa-queue-push"' in main_tf

    # 3. Token creator role on sa-queue-push for Pub/Sub service agent
    assert 'resource "google_service_account_iam_member" "pubsub_token_creator"' in main_tf
    assert 'role               = "roles/iam.serviceAccountTokenCreator"' in main_tf
    assert "gcp-sa-pubsub.iam.gserviceaccount.com" in main_tf

    # 4. DLQ publisher and subscriber permissions for Pub/Sub service agent
    assert 'resource "google_pubsub_topic_iam_member" "pubsub_dlq_publisher"' in main_tf
    assert 'resource "google_pubsub_subscription_iam_member" "pubsub_sub_subscriber"' in main_tf

    # 5. Dynamic push_config block inside human_queue_sub
    assert 'dynamic "push_config"' in main_tf
    assert "oidc_token" in main_tf
    assert "google_service_account.sa_queue_push.email" in main_tf

    # 6. Output sa_queue_push_email
    assert 'output "sa_queue_push_email"' in outputs_tf


# REQ-AGENT-030
# REQ-AGENT-031
def test_agents_iam_terraform_module_handoff_static():
    """Verify FaqResolver has publisher rights on human-queue topic (REQ-AGENT-030)."""
    iam_dir = TF_MODULES_DIR / "agents-iam"
    main_tf = (iam_dir / "main.tf").read_text(encoding="utf-8")

    # 1. TriageRouter and FaqResolver both have publisher roles on human-queue topic
    assert 'resource "google_pubsub_topic_iam_member" "triage_human_queue_publisher"' in main_tf
    assert 'resource "google_pubsub_topic_iam_member" "faq_human_queue_publisher"' in main_tf
    assert 'member  = "serviceAccount:${google_service_account.sa_faq.email}"' in main_tf
    assert 'role    = "roles/pubsub.publisher"' in main_tf


# REQ-AGENT-031
def test_cloud_run_services_push_consumer_static():
    """Verify Cloud Run human-queue configuration includes push env vars and invoker IAM."""
    cr_dir = TF_MODULES_DIR / "cloud-run-services"
    vars_tf = (cr_dir / "variables.tf").read_text(encoding="utf-8")
    main_tf = (cr_dir / "main.tf").read_text(encoding="utf-8")

    # 1. Variables for push audience and push invoker SA
    assert 'variable "push_audience"' in vars_tf
    assert 'variable "sa_queue_push_email"' in vars_tf

    # 2. Environment variables in human_queue container
    assert 'name  = "PUSH_AUDIENCE"' in main_tf
    assert 'name  = "PUSH_INVOKER_EMAIL"' in main_tf

    # 3. Cloud Run invoker IAM grant for sa-queue-push
    assert 'resource "google_cloud_run_v2_service_iam_member" "human_queue_push_invoker"' in main_tf
    assert 'role     = "roles/run.invoker"' in main_tf
    assert "var.sa_queue_push_email" in main_tf


# REQ-AGENT-030
def test_faq_resolver_k8s_deployment_manifest_static():
    """Verify GKE deployment for faq-resolver sets HUMAN_QUEUE_TOPIC."""
    deploy_yaml_path = K8S_DIR / "faq-resolver" / "deployment.yaml"
    assert deploy_yaml_path.is_file(), f"Missing deployment manifest: {deploy_yaml_path}"

    with open(deploy_yaml_path, encoding="utf-8") as f:
        manifest = yaml.safe_load(f)

    containers = manifest["spec"]["template"]["spec"]["containers"]
    faq_container = next(c for c in containers if c["name"] == "faq-resolver")
    env_map = {item["name"]: item.get("value") for item in faq_container["env"]}

    assert "HUMAN_QUEUE_TOPIC" in env_map
    assert env_map["HUMAN_QUEUE_TOPIC"] == "human-queue"


# REQ-AGENT-030
def test_deploy_triage_agent_runtime_script_static():
    """Verify deploy_triage_agent_runtime.py wires HUMAN_QUEUE_TOPIC in env and CLI."""
    script_path = SCRIPTS_DIR / "deploy_triage_agent_runtime.py"
    assert script_path.is_file(), f"Missing script: {script_path}"

    script_text = script_path.read_text(encoding="utf-8")

    assert 'DEFAULT_HUMAN_QUEUE_TOPIC = "human-queue"' in script_text
    assert '"HUMAN_QUEUE_TOPIC": human_queue_topic' in script_text
    assert '"--human-queue-topic"' in script_text
    assert '"HUMAN_QUEUE_TOPIC": args.human_queue_topic' in script_text
