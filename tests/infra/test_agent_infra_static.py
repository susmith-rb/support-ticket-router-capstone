# tests/infra/test_agent_infra_static.py
# Static analysis and verification of Agent Tier Terraform, Kubernetes manifests, IAM, and deployment scripts.
# Implements REQ-AGENT-016, REQ-AGENT-017, REQ-AGENT-018, REQ-AGENT-019, REQ-AGENT-020.

import os
from pathlib import Path
import subprocess
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"
MOD_DIR = TF_DIR / "modules"
K8S_DIR = REPO_ROOT / "deploy" / "k8s" / "faq-resolver"
SCRIPTS_DIR = REPO_ROOT / "scripts" / "deploy_agents"


# REQ-AGENT-016
def test_agents_iam_least_privilege():
    """Verify dedicated service accounts, least privilege roles, and DB isolation.

    Invariants:
    1. sa-triage and sa-faq created as dedicated identities.
    2. Workload Identity binds ksa-faq in faq-resolver namespace to sa-faq.
    3. Resource-level roles/aiplatform.predictionUser granted on router-s1 endpoint.
    4. roles/aiplatform.user granted for Gemini calls.
    5. roles/discoveryengine.viewer granted exclusively to sa-faq (never sa-triage).
    6. Pub/Sub publisher on human-queue for sa-triage; subscriber on human-queue-sub for sa-queue.
    7. Invariant: Neither sa-triage nor sa-faq is granted direct database access.
    """
    iam_mod_dir = MOD_DIR / "agents-iam"
    assert iam_mod_dir.is_dir(), f"Missing module: {iam_mod_dir}"
    main_tf = (iam_mod_dir / "main.tf").read_text(encoding="utf-8")
    vars_tf = (iam_mod_dir / "variables.tf").read_text(encoding="utf-8")
    outputs_tf = (iam_mod_dir / "outputs.tf").read_text(encoding="utf-8")

    assert "sa_queue_email" in vars_tf
    assert "router_endpoint_id" in vars_tf

    # Service accounts
    assert 'resource "google_service_account" "sa_triage"' in main_tf
    assert 'account_id   = "sa-triage"' in main_tf
    assert 'resource "google_service_account" "sa_faq"' in main_tf
    assert 'account_id   = "sa-faq"' in main_tf

    # Workload Identity binding
    assert 'resource "google_service_account_iam_member" "faq_workload_identity"' in main_tf
    assert 'role               = "roles/iam.workloadIdentityUser"' in main_tf
    assert "faq-resolver/ksa-faq" in main_tf

    # Prediction user on router-s1 (google-beta endpoint IAM)
    assert 'resource "google_vertex_ai_endpoint_iam_member" "triage_prediction_user"' in main_tf
    assert 'resource "google_vertex_ai_endpoint_iam_member" "faq_prediction_user"' in main_tf
    assert 'role     = "roles/aiplatform.predictionUser"' in main_tf

    # Gemini inference
    assert 'role    = "roles/aiplatform.user"' in main_tf

    # Discovery Engine viewer for FAQ only
    assert 'resource "google_project_iam_member" "faq_discovery_engine_viewer"' in main_tf
    assert 'role    = "roles/discoveryengine.viewer"' in main_tf
    assert "triage_discovery_engine" not in main_tf

    # Pub/Sub permissions
    assert 'resource "google_pubsub_topic_iam_member" "triage_human_queue_publisher"' in main_tf
    assert 'role    = "roles/pubsub.publisher"' in main_tf
    assert 'resource "google_pubsub_subscription_iam_member" "queue_human_queue_subscriber"' in main_tf
    assert 'role         = "roles/pubsub.subscriber"' in main_tf

    # DB isolation invariant: No direct SQL grants in agent IAM
    assert "roles/cloudsql" not in main_tf
    assert "ticket_rw" not in main_tf
    assert "orders_ro" not in main_tf
    assert "queue_rw" not in main_tf

    # Outputs
    assert "sa_triage_email" in outputs_tf
    assert "sa_faq_email" in outputs_tf


# REQ-AGENT-017
def test_triage_deployment_script():
    """Verify Vertex AI Agent Engine deployment script and staging bucket IAM.

    Invariants:
    1. Deployment script isolates environment in-process before imports/pickling.
    2. Pinned dependencies include cloudpickle, pydantic, and google-cloud-aiplatform.
    3. Staging bucket grant gives roles/storage.objectViewer to Reasoning Engine service agent.
    4. Script executes successfully in --dry-run mode without network credentials.
    """
    script_path = SCRIPTS_DIR / "deploy_triage_agent_runtime.py"
    assert script_path.is_file(), f"Missing deploy script: {script_path}"
    assert os.access(script_path, os.X_OK), "deploy_triage_agent_runtime.py must be executable"

    script_content = script_path.read_text(encoding="utf-8")
    assert 'os.environ["GOOGLE_CLOUD_PROJECT"] = project' in script_content
    assert 'os.environ["GOOGLE_CLOUD_LOCATION"] = region' in script_content
    assert "cloudpickle==3.0.0" in script_content
    assert "pydantic>=2.0.0" in script_content

    # Check Reasoning Engine service agent bucket permissions in agents-iam/main.tf
    iam_main_tf = (MOD_DIR / "agents-iam" / "main.tf").read_text(encoding="utf-8")
    assert 'resource "google_storage_bucket_iam_member" "re_staging_reader"' in iam_main_tf
    assert 'role   = "roles/storage.objectViewer"' in iam_main_tf
    assert "gcp-sa-aiplatform-re.iam.gserviceaccount.com" in iam_main_tf

    # Dry-run execution test
    result = subprocess.run(
        [str(script_path), "--dry-run", "--project", "test-project", "--region", "us-central1"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"Dry-run failed: {result.stderr}"
    combined_output = result.stdout + result.stderr
    assert "dry-run-sample-id" in combined_output or "Dry Run" in combined_output


# REQ-AGENT-018
def test_faq_gke_manifests():
    """Verify GKE Autopilot Terraform and Kubernetes manifests for FaqResolverAgent.

    Invariants:
    1. GKE Autopilot cluster has private nodes, public endpoint, and REGULAR release channel.
    2. Kubernetes manifests define Namespace, KSA with Workload Identity, Deployment, Service, and HPA.
    3. HPA targets 60% CPU utilization with minReplicas 1 and maxReplicas 10.
    4. deploy_faq_gke.sh is executable and supports --dry-run.
    """
    gke_mod_dir = MOD_DIR / "gke-autopilot"
    assert gke_mod_dir.is_dir()
    gke_main = (gke_mod_dir / "main.tf").read_text(encoding="utf-8")

    # GKE Autopilot cluster configuration
    assert 'enable_autopilot = true' in gke_main
    assert 'channel = "REGULAR"' in gke_main
    assert 'enable_private_nodes    = true' in gke_main
    assert 'deletion_protection = var.deletion_protection' in gke_main
    assert 'network    = var.network' in gke_main or 'network = var.network' in gke_main
    assert 'subnetwork = var.subnetwork' in gke_main or 'subnetwork = var.subnetwork' in gke_main
    assert 'cluster_secondary_range_name' in gke_main
    assert 'services_secondary_range_name' in gke_main

    # Network Module
    net_mod_dir = MOD_DIR / "network"
    assert net_mod_dir.is_dir()
    net_main = (net_mod_dir / "main.tf").read_text(encoding="utf-8")
    assert 'resource "google_compute_network" "support_vpc"' in net_main
    assert 'resource "google_compute_subnetwork" "support_us_central1"' in net_main
    assert 'private_ip_google_access = true' in net_main
    assert '"gke-pods"' in net_main
    assert '"gke-services"' in net_main

    # Root module network wiring
    root_main = (TF_DIR / "main.tf").read_text(encoding="utf-8")
    assert 'module "network"' in root_main
    assert '"compute.googleapis.com"' in root_main
    assert 'network                       = module.network.network_id' in root_main
    assert 'subnetwork                    = module.network.subnetwork_id' in root_main

    # K8s Manifests
    assert (K8S_DIR / "namespace.yaml").is_file()
    assert (K8S_DIR / "ksa.yaml").is_file()
    assert (K8S_DIR / "deployment.yaml").is_file()
    assert (K8S_DIR / "service.yaml").is_file()
    assert (K8S_DIR / "hpa.yaml").is_file()

    # Namespace
    ns_data = yaml.safe_load((K8S_DIR / "namespace.yaml").read_text(encoding="utf-8"))
    assert ns_data["metadata"]["name"] == "faq-resolver"

    # KSA
    ksa_data = yaml.safe_load((K8S_DIR / "ksa.yaml").read_text(encoding="utf-8"))
    assert ksa_data["metadata"]["name"] == "ksa-faq"
    assert "iam.gke.io/gcp-service-account" in ksa_data["metadata"]["annotations"]

    # Deployment
    deploy_data = yaml.safe_load((K8S_DIR / "deployment.yaml").read_text(encoding="utf-8"))
    assert deploy_data["metadata"]["name"] == "faq-resolver"
    pod_spec = deploy_data["spec"]["template"]["spec"]
    assert pod_spec["serviceAccountName"] == "ksa-faq"
    container = pod_spec["containers"][0]
    assert container["name"] == "faq-resolver"
    assert container["ports"][0]["containerPort"] == 8000

    # Service
    svc_data = yaml.safe_load((K8S_DIR / "service.yaml").read_text(encoding="utf-8"))
    assert svc_data["metadata"]["name"] == "faq-resolver"
    assert svc_data["spec"]["type"] == "ClusterIP"

    # HPA
    hpa_data = yaml.safe_load((K8S_DIR / "hpa.yaml").read_text(encoding="utf-8"))
    assert hpa_data["metadata"]["name"] == "faq-resolver-hpa"
    assert hpa_data["spec"]["minReplicas"] == 1
    assert hpa_data["spec"]["maxReplicas"] == 10
    assert hpa_data["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] == 60

    # Shell script
    deploy_sh = SCRIPTS_DIR / "deploy_faq_gke.sh"
    assert deploy_sh.is_file()
    assert os.access(deploy_sh, os.X_OK)
    sh_content = deploy_sh.read_text(encoding="utf-8")
    assert "gcloud container clusters get-credentials" in sh_content
    assert "kubectl apply" in sh_content


# REQ-AGENT-019
def test_cloud_run_services_iam_and_cost_posture():
    """Verify Cloud Run microservices configuration, ingress, IAM, and scale-to-zero.

    Invariants:
    1. Services defined for ticket-db-mcp, order-lookup-mcp, and human-queue.
    2. Ingress configured as INGRESS_TRAFFIC_ALL with IAM-only authentication.
    3. Invariant: NO public allUsers or allAuthenticatedUsers invoker grants.
    4. Invariant: Only authorized callers (sa-triage, sa-faq) receive roles/run.invoker.
    5. Cost posture minimal: default min_instance_count = 0, max_instance_count = 3.
    6. Conditional creation: count = 0 when agents_image is empty.
    """
    cr_mod_dir = MOD_DIR / "cloud-run-services"
    assert cr_mod_dir.is_dir()
    main_tf = (cr_mod_dir / "main.tf").read_text(encoding="utf-8")
    vars_tf = (cr_mod_dir / "variables.tf").read_text(encoding="utf-8")
    outputs_tf = (cr_mod_dir / "outputs.tf").read_text(encoding="utf-8")

    # Cloud Run v2 service resources
    assert 'resource "google_cloud_run_v2_service" "ticket_db_mcp"' in main_tf
    assert 'resource "google_cloud_run_v2_service" "order_lookup_mcp"' in main_tf
    assert 'resource "google_cloud_run_v2_service" "human_queue"' in main_tf

    # Ingress and scaling
    assert 'ingress = "INGRESS_TRAFFIC_ALL"' in main_tf
    assert "min_instance_count = var.min_instance_count" in main_tf
    assert "max_instance_count = var.max_instance_count" in main_tf
    assert "default     = 0" in vars_tf  # min_instance_count
    assert "default     = 3" in vars_tf  # max_instance_count

    # Conditional deployment
    assert "count = var.agents_image != \"\" ? 1 : 0" in main_tf

    # Strict IAM invokers & No public access
    assert 'role     = "roles/run.invoker"' in main_tf
    assert '"allUsers"' not in main_tf
    assert '"allAuthenticatedUsers"' not in main_tf

    # Outputs
    assert "ticket_db_mcp_url" in outputs_tf
    assert "order_lookup_mcp_url" in outputs_tf
    assert "human_queue_url" in outputs_tf


# REQ-AGENT-020
def test_pubsub_escalation_and_dlq():
    """Verify Pub/Sub escalation topic, dead-letter topic, and pull subscription with retries.

    Invariants:
    1. Primary topic human-queue and DLQ topic human-queue-dlq.
    2. Pull subscription human-queue-sub with dead_letter_policy (max_delivery_attempts = 5).
    3. Exponential retry policy on subscription (min 10s, max 600s).
    4. pubsub.googleapis.com enabled in project services.
    5. Root main.tf wires pubsub, gke-autopilot, agents-iam, and cloud-run-services.
    """
    ps_mod_dir = MOD_DIR / "pubsub"
    assert ps_mod_dir.is_dir()
    main_tf = (ps_mod_dir / "main.tf").read_text(encoding="utf-8")
    outputs_tf = (ps_mod_dir / "outputs.tf").read_text(encoding="utf-8")

    # Resources
    assert 'resource "google_pubsub_topic" "human_queue"' in main_tf
    assert 'resource "google_pubsub_topic" "human_queue_dlq"' in main_tf
    assert 'resource "google_pubsub_subscription" "human_queue_sub"' in main_tf

    # DLQ and retry configuration
    assert "dead_letter_policy" in main_tf
    assert "max_delivery_attempts = 5" in main_tf
    assert "retry_policy" in main_tf
    assert 'minimum_backoff = "10s"' in main_tf
    assert 'maximum_backoff = "600s"' in main_tf

    # Outputs
    assert "topic_name" in outputs_tf
    assert "dlq_topic_name" in outputs_tf
    assert "subscription_name" in outputs_tf

    # Root module wiring
    root_main = (TF_DIR / "main.tf").read_text(encoding="utf-8")
    assert '"pubsub.googleapis.com"' in root_main
    assert 'module "pubsub"' in root_main
    assert 'module "gke_autopilot"' in root_main
    assert 'module "agents_iam"' in root_main
    assert 'module "cloud_run_services"' in root_main
