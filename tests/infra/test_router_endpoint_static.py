# REQ-SERVE-005: Shared GPU Replicas and Vertex Endpoint Infrastructure
# REQ-SERVE-006: Inference Latency Budget and Smoke Verification
"""tests/infra/test_router_endpoint_static.py — Static verification of router endpoint Terraform and CI/CD.

Validates:
1. Module structure: deploy/terraform/single-project/modules/router-endpoint (main, variables, outputs).
2. IAM least privilege: ticket-router-serving granted storage.objectViewer, logging.logWriter,
   monitoring.metricWriter (no roles/editor, no roles/owner).
3. Artifact Registry repository: Docker format, location laya_region.
4. Vertex AI Endpoint: router-s1 resource, gated by enable_router_endpoint.
5. Root wiring: main.tf, variables.tf, outputs.tf.
6. Deployment defaults: MIN_REPLICAS=1, MAX_REPLICAS=4, g2-standard-4 + NVIDIA_L4,
   autoscaling metric target in deploy.sh and cloudbuild-serving.yaml.
"""

from pathlib import Path
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"
MODULE_DIR = TF_DIR / "modules" / "router-endpoint"
CLOUDBUILD_SERVING = REPO_ROOT / "cicd" / "cloudbuild-serving.yaml"
DEPLOY_SH = REPO_ROOT / "serving" / "laya" / "deploy.sh"
DOCKERFILE = REPO_ROOT / "serving" / "laya" / "Dockerfile"
REQUIREMENTS_TXT = REPO_ROOT / "serving" / "laya" / "requirements.txt"


# REQ-SERVE-005
def test_router_endpoint_module_files_exist():
    """Verify router-endpoint module contains required files."""
    assert MODULE_DIR.is_dir(), f"Missing module directory: {MODULE_DIR}"
    assert (MODULE_DIR / "main.tf").is_file(), "Missing main.tf in router-endpoint"
    assert (MODULE_DIR / "variables.tf").is_file(), "Missing variables.tf in router-endpoint"
    assert (MODULE_DIR / "outputs.tf").is_file(), "Missing outputs.tf in router-endpoint"


# REQ-SERVE-005
def test_router_endpoint_artifact_registry():
    """Verify Artifact Registry repository resource definition."""
    content = (MODULE_DIR / "main.tf").read_text()
    assert 'resource "google_artifact_registry_repository" "serving"' in content
    assert 'format        = "DOCKER"' in content or 'format = "DOCKER"' in content
    assert "repository_id = \"serving\"" in content


# REQ-SERVE-005
def test_router_endpoint_service_account_and_iam():
    """Verify service account has least-privilege IAM roles and no editor/owner."""
    content = (MODULE_DIR / "main.tf").read_text()
    assert 'resource "google_service_account" "ticket_router_serving"' in content
    assert 'account_id   = "ticket-router-serving"' in content or 'account_id = "ticket-router-serving"' in content

    # Least privilege roles for runtime container
    assert "roles/storage.objectViewer" in content
    assert "roles/logging.logWriter" in content
    assert "roles/monitoring.metricWriter" in content

    # Impersonation bindings: Vertex AI Service Agent & Cloud Build SA
    assert 'resource "google_service_account_iam_member" "vertex_service_agent_user"' in content
    assert "roles/iam.serviceAccountUser" in content
    assert "gcp-sa-aiplatform.iam.gserviceaccount.com" in content
    assert 'resource "google_service_account_iam_member" "cloudbuild_serving_sa_user"' in content

    # Cloud Build deployment permissions
    assert 'resource "google_project_iam_member" "cloudbuild_aiplatform_user"' in content
    assert "roles/aiplatform.user" in content
    assert 'resource "google_artifact_registry_repository_iam_member" "cloudbuild_serving_writer"' in content
    assert "roles/artifactregistry.writer" in content

    # Security Invariant: no broad editor/owner roles
    assert "roles/editor" not in content
    assert "roles/owner" not in content


# REQ-SERVE-005
def test_router_endpoint_vertex_resource():
    """Verify Vertex AI Endpoint definition and feature gating."""
    content = (MODULE_DIR / "main.tf").read_text()
    assert 'resource "google_vertex_ai_endpoint" "router_s1"' in content
    assert 'name         = "router-s1"' in content or 'name = "router-s1"' in content
    assert "var.enable_router_endpoint" in content


# REQ-SERVE-005
def test_root_terraform_wiring():
    """Verify router_endpoint module is wired in root single-project Terraform."""
    root_main = (TF_DIR / "main.tf").read_text()
    root_vars = (TF_DIR / "variables.tf").read_text()
    root_outs = (TF_DIR / "outputs.tf").read_text()

    assert 'module "router_endpoint"' in root_main
    assert '"./modules/router-endpoint"' in root_main
    assert "artifactregistry.googleapis.com" in root_main
    assert "cloudbuild_sa_email" in root_main

    assert 'variable "laya_region"' in root_vars
    assert 'variable "enable_router_endpoint"' in root_vars

    assert 'output "router_endpoint_id"' in root_outs
    assert 'output "router_endpoint_name"' in root_outs
    assert 'output "router_serving_service_account"' in root_outs
    assert 'output "serving_artifact_registry_repo"' in root_outs


# REQ-SERVE-005
def test_deploy_sh_defaults_and_autoscaling():
    """Verify deploy.sh enforces MIN_REPLICAS=1, MAX_REPLICAS=4, serving SA, and explicit autoscaling."""
    assert DEPLOY_SH.is_file(), f"Missing {DEPLOY_SH}"
    content = DEPLOY_SH.read_text()

    # User decision (2026-10-26): MIN_REPLICAS=1 default, MAX_REPLICAS=4
    assert 'MIN_REPLICAS="${MIN_REPLICAS:-1}"' in content
    assert 'MAX_REPLICAS="${MAX_REPLICAS:-4}"' in content
    assert 'AUTOSCALING_METRIC_TARGET="${AUTOSCALING_METRIC_TARGET:-60}"' in content
    assert 'SERVING_SA="${SERVING_SA:-ticket-router-serving@${PROJECT}.iam.gserviceaccount.com}"' in content
    assert 'MACHINE_TYPE="${MACHINE_TYPE:-g2-standard-4}"' in content
    assert 'ACCELERATOR="${ACCELERATOR:-NVIDIA_L4}"' in content

    # CLI invocation parameters
    assert '--service-account="${SERVING_SA}"' in content
    assert '--autoscaling-metric-specs="gpu-duty-cycle=${AUTOSCALING_METRIC_TARGET}"' in content
    assert "--min-replica-count=" in content or "--min-replica-count" in content
    assert "--max-replica-count=" in content or "--max-replica-count" in content


# REQ-SERVE-005, REQ-SERVE-006
def test_cloudbuild_serving_yaml_validity():
    """Verify cicd/cloudbuild-serving.yaml substitutions, build steps, and service account."""
    assert CLOUDBUILD_SERVING.is_file(), f"Missing {CLOUDBUILD_SERVING}"
    raw_content = CLOUDBUILD_SERVING.read_text()
    data = yaml.safe_load(raw_content)

    assert "serviceAccount: 'projects/${PROJECT_ID}/serviceAccounts/ticket-router-build@${PROJECT_ID}.iam.gserviceaccount.com'" in raw_content or data.get("serviceAccount") is not None

    subs = data.get("substitutions", {})
    assert subs.get("_MIN_REPLICAS") == "1", "Default _MIN_REPLICAS must be '1' for capstone"
    assert subs.get("_MAX_REPLICAS") == "4", "Default _MAX_REPLICAS must be '4'"
    assert subs.get("_MACHINE_TYPE") == "g2-standard-4"
    assert subs.get("_ACCELERATOR") == "NVIDIA_L4"
    assert subs.get("_AUTOSCALING_METRIC_TARGET") == "60"
    assert "_SERVING_SA" in subs

    steps = data.get("steps", [])
    step_ids = [s.get("id") for s in steps]
    assert "build-serving-image" in step_ids
    assert "push-serving-image" in step_ids
    assert "deploy-serving-model" in step_ids
    assert 'SERVING_SA="${_SERVING_SA}"' in raw_content


# REQ-SERVE-005
def test_deploy_sh_model_id_resolution_and_validation():
    """Verify deploy.sh resolves Model ID via gcloud ai models list, validates non-empty, and normalizes accelerator."""
    assert DEPLOY_SH.is_file(), f"Missing {DEPLOY_SH}"
    content = DEPLOY_SH.read_text()

    # Model resolution after LRO upload
    assert "gcloud ai models list" in content
    assert "--filter=" in content
    assert "--sort-by=" in content
    assert "--limit=1" in content
    assert '--format="value(name)"' in content

    # Extraction of last path segment
    assert "awk -F'/' '{print $NF}'" in content or "basename" in content

    # Empty check and loud failure
    assert '[ -z "${MODEL_ID}" ]' in content
    assert "exit 1" in content

    # Accelerator normalization to lowercase-hyphen
    assert "tr '[:upper:]_' '[:lower:]-'" in content


# REQ-SERVE-001
def test_serving_requirements_pinned_to_laya_tune():
    """Verify serving/laya/requirements.txt pins exact versions matching validated laya-tune venv."""
    assert REQUIREMENTS_TXT.is_file(), f"Missing {REQUIREMENTS_TXT}"
    content = REQUIREMENTS_TXT.read_text()

    # Exact ML pins
    assert "transformers==5.20.2" in content
    assert "laya==0.3.20" in content
    assert "tokenizers==0.23.3" in content
    assert "safetensors==0.8.0" in content
    assert "huggingface_hub==2.2.0" in content
    assert "numpy==2.5.4" in content

    # Serving web stack
    assert "fastapi==" in content
    assert "uvicorn" in content
    assert "pydantic==" in content
    assert "google-cloud-storage==" in content


# REQ-SERVE-001
def test_serving_dockerfile_base_and_sanity_check():
    """Verify Dockerfile uses Python 3.12, installs torch from cu126, and runs build-time sanity."""
    assert DOCKERFILE.is_file(), f"Missing {DOCKERFILE}"
    content = DOCKERFILE.read_text()

    # Python 3.12 base
    assert "python:3.12" in content

    # Dedicated PyTorch CUDA 12.6 installation from PyTorch wheel index
    assert "--index-url https://download.pytorch.org/whl/cu126" in content
    assert "torch==" in content

    # REQUIRE_GPU environment variable configured
    assert "REQUIRE_GPU=1" in content

    # Requirements installation
    assert "requirements.txt" in content
    assert "pip install --no-cache-dir -r" in content

    # Build-time sanity verification step asserting torch version, CUDA 12, and PreTrainedModel import
    assert "assert torch.__version__.startswith('2.14')" in content
    assert "torch.version.cuda.startswith('12')" in content
    assert "from transformers import PreTrainedModel" in content

    # Non-root appuser
    assert "useradd -u 10001" in content
    assert "USER appuser" in content


# REQ-SERVE-001, REQ-SERVE-005
def test_serving_dockerfile_nvidia_runtime_env():
    """Verify Dockerfile configures NVIDIA container runtime environment variables for Vertex/GKE driver injection."""
    assert DOCKERFILE.is_file(), f"Missing {DOCKERFILE}"
    content = DOCKERFILE.read_text()

    assert "NVIDIA_VISIBLE_DEVICES=all" in content
    assert "NVIDIA_DRIVER_CAPABILITIES=compute,utility" in content
    assert "LD_LIBRARY_PATH=/usr/local/nvidia/lib64:/usr/local/nvidia/lib:${LD_LIBRARY_PATH}" in content
    assert "PATH=/usr/local/nvidia/bin:${PATH}" in content

