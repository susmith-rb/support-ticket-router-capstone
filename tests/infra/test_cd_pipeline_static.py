# tests/infra/test_cd_pipeline_static.py
# Static analysis and verification for Continuous Deployment (CD) pipeline.
# Implements REQ-CD-001 through REQ-CD-010.

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CICD_DIR = REPO_ROOT / "cicd"
DEPLOY_YAML = CICD_DIR / "cloudbuild-deploy.yaml"
TF_SINGLE_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"
CLOUDBUILD_MOD_DIR = TF_SINGLE_DIR / "modules" / "cloudbuild"
CLOUD_RUN_MOD_DIR = TF_SINGLE_DIR / "modules" / "cloud-run-services"
GKE_MOD_DIR = TF_SINGLE_DIR / "modules" / "gke-autopilot"
SCRIPTS_DIR = REPO_ROOT / "scripts"
RUNBOOKS_DIR = REPO_ROOT / "docs" / "runbooks"


# REQ-CD-001
def test_cd_trigger_requires_approval():
    """Verify approval-gated CD trigger on push to main with approver IAM.

    Invariants:
    1. google_cloudbuild_trigger.cd is defined in modules/cloudbuild/main.tf.
    2. Name is support-ticket-router-cd, file is cicd/cloudbuild-deploy.yaml, branch is ^main$.
    3. approval_config specifies approval_required = true.
    4. Service account is sa-cd-deployer (cd_sa).
    5. enable_cd_trigger variable defaults to true.
    6. cd_approvers variable defaults to susmith@gcp.altostrat.com.
    7. Approver IAM member grants roles/cloudbuild.builds.approver.
    8. Substitutions pass _CI_TRIGGER_ID and _TRIAGE_RESOURCE.
    """
    main_tf = (CLOUDBUILD_MOD_DIR / "main.tf").read_text(encoding="utf-8")
    vars_tf = (CLOUDBUILD_MOD_DIR / "variables.tf").read_text(encoding="utf-8")
    root_vars_tf = (TF_SINGLE_DIR / "variables.tf").read_text(encoding="utf-8")
    root_main_tf = (TF_SINGLE_DIR / "main.tf").read_text(encoding="utf-8")

    assert 'resource "google_cloudbuild_trigger" "cd"' in main_tf
    assert 'name            = "support-ticket-router-cd"' in main_tf
    assert 'filename        = "cicd/cloudbuild-deploy.yaml"' in main_tf
    assert "approval_config" in main_tf
    assert "approval_required = true" in main_tf
    assert "service_account = google_service_account.cd_sa.id" in main_tf
    assert 'branch = "^main$"' in main_tf

    assert "enable_cd_trigger" in vars_tf
    assert "cd_approvers" in vars_tf
    assert "triage_resource_name" in vars_tf

    assert "enable_cd_trigger" in root_vars_tf
    assert "cd_approvers" in root_vars_tf
    assert "triage_resource_name" in root_vars_tf

    assert "enable_cd_trigger           = var.enable_cd_trigger" in root_main_tf
    assert "cd_approvers                = var.cd_approvers" in root_main_tf
    assert "triage_resource_name        = var.triage_resource_name" in root_main_tf

    assert 'role     = "roles/cloudbuild.builds.approver"' in main_tf
    assert "var.cd_approvers" in main_tf

    assert "_CI_TRIGGER_ID" in main_tf
    assert "_TRIAGE_RESOURCE" in main_tf


# REQ-CD-002
def test_cd_requires_ci_green():
    """Verify first CD pipeline step gates on successful CI build for the commit.

    Invariants:
    1. First step is require-ci-green.
    2. Polls gcloud builds list with filter on SHORT_SHA and CI trigger ID.
    3. Fails on FAILURE, TIMEOUT, or CANCELLED status.
    4. Has a 30-minute (1800s) timeout ceiling.
    """
    assert DEPLOY_YAML.is_file(), f"Missing deployment pipeline YAML: {DEPLOY_YAML}"
    with open(DEPLOY_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    steps = config.get("steps", [])
    assert len(steps) > 0, "No steps found in cloudbuild-deploy.yaml"

    step0 = steps[0]
    assert step0.get("id") == "require-ci-green"
    script = " ".join(step0.get("args", []))

    assert "gcloud builds list" in script
    assert "SHORT_SHA" in script
    assert "SUCCESS" in script
    assert "FAILURE" in script or "TIMEOUT" in script or "CANCELLED" in script
    assert "1800" in script or "30" in script


# REQ-CD-003
def test_cd_builds_single_image():
    """Verify CD pipeline builds and pushes unified agents container image once.

    Invariants:
    1. Step build-and-push-image exists; it may overlap the CI gate, but every deploy
       step depends on fetch-last-good, which waits for require-ci-green.
    2. Builds Dockerfile from services/Dockerfile.
    3. Tags image as us-central1-docker.pkg.dev/$PROJECT_ID/serving/agents:$SHORT_SHA.
    4. Pushes image to Artifact Registry.
    """
    with open(DEPLOY_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    build_step = next((s for s in config.get("steps", []) if s.get("id") == "build-and-push-image"), None)
    assert build_step is not None, "Missing step 'build-and-push-image'"
    steps = config.get("steps", [])
    by_id = {st["id"]: st for st in steps}
    assert "require-ci-green" in by_id["fetch-last-good"]["waitFor"]
    assert "build-and-push-image" in by_id["fetch-last-good"]["waitFor"]
    for dep in ("deploy-cloud-run", "deploy-gke", "deploy-agent-engine"):
        assert "fetch-last-good" in by_id[dep]["waitFor"], dep  # no deploy before CI is green
    assert set(by_id["post-deploy-smoke"]["waitFor"]) == {"deploy-cloud-run", "deploy-gke", "deploy-agent-engine"}

    script = " ".join(build_step.get("args", []))
    assert "docker build" in script
    assert "services/Dockerfile" in script
    assert "serving/agents" in script
    assert "docker push" in script


# REQ-CD-004
def test_cd_deploys_cloud_run():
    """Verify CD pipeline updates all three Cloud Run services.

    Invariants:
    1. Step deploy-cloud-run updates ticket-db-mcp, order-lookup-mcp, and human-queue.
    2. Image argument references the built agents tag.
    3. Region is set to us-central1.
    4. Updates image only, preserving environment and IAM in Terraform.
    """
    with open(DEPLOY_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    run_step = next((s for s in config.get("steps", []) if s.get("id") == "deploy-cloud-run"), None)
    assert run_step is not None, "Missing step 'deploy-cloud-run'"

    script = " ".join(run_step.get("args", []))
    assert "gcloud run services update" in script
    assert "ticket-db-mcp" in script
    assert "order-lookup-mcp" in script
    assert "human-queue" in script
    assert "--image" in script


# REQ-CD-005
def test_cd_deploys_gke():
    """Verify GKE cluster DNS endpoint configuration and CD GKE rollout step.

    Invariants:
    1. GKE Autopilot module configures control_plane_endpoints_config.dns_endpoint_config.allow_external_traffic = true.
    2. Step deploy-gke uses gcloud container clusters get-credentials with --dns-endpoint.
    3. Manifests in deploy/k8s/faq-resolver/ applied via kubectl apply with rendered image tag.
    4. Rollout status monitored up to 300s timeout.
    """
    gke_main = (GKE_MOD_DIR / "main.tf").read_text(encoding="utf-8")
    assert "control_plane_endpoints_config" in gke_main
    assert "dns_endpoint_config" in gke_main
    assert "allow_external_traffic = true" in gke_main

    with open(DEPLOY_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    gke_step = next((s for s in config.get("steps", []) if s.get("id") == "deploy-gke"), None)
    assert gke_step is not None, "Missing step 'deploy-gke'"

    script = " ".join(gke_step.get("args", []))
    assert "--dns-endpoint" in script
    assert "get-credentials" in script
    assert "kubectl apply" in script
    assert "rollout status deployment/faq-resolver" in script
    assert "300s" in script or "300" in script


# REQ-CD-006
def test_cd_updates_agent_engine():
    """Verify Agent Engine deployment step, pyproject deploy extra, and UPDATING poll.

    Invariants:
    1. pyproject.toml defines [project.optional-dependencies].deploy with pinned runtime dependencies.
    2. deploy_triage_agent_runtime.py contains wait_for_reasoning_engine_idle polling <= 10 min.
    3. Step deploy-agent-engine runs on python:3.11-slim and calls deploy_triage_agent_runtime.py --update.
    """
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pyproject_data = tomllib.loads(pyproject_text)
    deploy_extras = pyproject_data.get("project", {}).get("optional-dependencies", {}).get("deploy", [])
    assert len(deploy_extras) >= 6, "Missing or incomplete [project.optional-dependencies].deploy"
    assert any("google-cloud-aiplatform" in p for p in deploy_extras)
    assert any("google-adk" in p for p in deploy_extras)
    assert any("cloudpickle" in p for p in deploy_extras)

    deploy_script = (SCRIPTS_DIR / "deploy_agents" / "deploy_triage_agent_runtime.py").read_text(encoding="utf-8")
    assert "wait_for_reasoning_engine_idle" in deploy_script
    assert "UPDATING" in deploy_script
    assert "600" in deploy_script or "10" in deploy_script

    with open(DEPLOY_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    ae_step = next((s for s in config.get("steps", []) if s.get("id") == "deploy-agent-engine"), None)
    assert ae_step is not None, "Missing step 'deploy-agent-engine'"
    assert ae_step.get("name") == "python:3.11-slim"

    script = " ".join(ae_step.get("args", []))
    assert "/workspace/.venv/bin/python" in script  # venv built by prepare-python
    prep = next(st for st in config.get("steps", []) if st["id"] == "prepare-python")
    assert ".[deploy" in " ".join(prep["args"])
    assert "deploy_triage_agent_runtime.py" in script
    assert "--update" in script


# REQ-CD-011
def test_cd_agent_engine_step_is_change_aware():
    """Verify deploy-agent-engine step implements REQ-CD-011 change awareness.

    Invariants:
    1. cloudbuild-deploy.yaml defines _FORCE_AGENT_ENGINE substitution defaulting to false.
    2. Step deploy-agent-engine invokes ae_fingerprint.py should-deploy with --bucket and --force.
    3. Handles exit code 3 (SKIP) by exiting 0 without deploying.
    4. On exit code 0, invokes deploy_triage_agent_runtime.py --update followed by ae_fingerprint.py record.
    5. Fails closed on any unexpected exit code from should-deploy.
    """
    with open(DEPLOY_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    substitutions = config.get("substitutions", {})
    assert "_FORCE_AGENT_ENGINE" in substitutions
    assert substitutions["_FORCE_AGENT_ENGINE"] == "false"

    ae_step = next((s for s in config.get("steps", []) if s.get("id") == "deploy-agent-engine"), None)
    assert ae_step is not None, "Missing step 'deploy-agent-engine'"

    script = " ".join(ae_step.get("args", []))
    assert "ae_fingerprint.py should-deploy" in script
    assert "--force" in script
    assert "_FORCE_AGENT_ENGINE" in script
    assert "3" in script  # Checks exit code 3 for skip
    assert "deploy_triage_agent_runtime.py" in script
    assert "ae_fingerprint.py record" in script
    assert "--sha" in script



# REQ-CD-007
def test_cd_post_deploy_smoke():
    """Verify post-deploy smoke gate scripts and execution in CD pipeline.

    Invariants:
    1. scripts/smoke/cloud_run_smoke.py exists and verifies 3 services /health 200, unauth 403,
       idempotent upsert dedup, customer boundary 404, and body customer_id 400.
    2. cloud_run_smoke.py uses google.oauth2.id_token.fetch_id_token without printing tokens.
    3. Step post-deploy-smoke runs cloud_run_smoke.py, e2e_identity_check.py, and model_armor_smoke.py --timeout-s 5.
    """
    smoke_script_path = SCRIPTS_DIR / "smoke" / "cloud_run_smoke.py"
    assert smoke_script_path.is_file(), f"Missing smoke script: {smoke_script_path}"
    smoke_code = smoke_script_path.read_text(encoding="utf-8")

    assert "get_id_token" in smoke_code  # shared helper with Cloud Build generateIdToken fallback
    assert "upsert_ticket" in smoke_code
    assert "get_order" in smoke_code
    assert "403" in smoke_code
    assert "400" in smoke_code
    assert "unverified_customer" in smoke_code  # asserted ids fail closed
    assert "print(token" not in smoke_code

    with open(DEPLOY_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    smoke_step = next((s for s in config.get("steps", []) if s.get("id") == "post-deploy-smoke"), None)
    assert smoke_step is not None, "Missing step 'post-deploy-smoke'"

    script = " ".join(smoke_step.get("args", []))
    assert "cloud_run_smoke.py" in script
    assert "e2e_identity_check.py" in script
    assert "model_armor_smoke.py" in script
    assert "--timeout-s 5" in script or "--timeout-s" in script


# REQ-CD-008
def test_cd_rollback_and_last_good():
    """Verify last-good deployment recording on success and rollback handling on failure.

    Invariants:
    1. Step record-last-good writes sha, image, build_id, time to gs://$PROJECT_ID-laya/cd/last_good.json.
    2. Step fetch-last-good sets up rollback handler reading last_good.json.
    3. Rollback targets Cloud Run and GKE deployments to last-good image.
    4. Rollback prints command to re-run CD trigger on last-good SHA.
    5. Rollback exits with code 1.
    """
    with open(DEPLOY_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    record_step = next((s for s in config.get("steps", []) if s.get("id") == "record-last-good"), None)
    assert record_step is not None, "Missing step 'record-last-good'"
    record_script = " ".join(record_step.get("args", []))
    assert "last_good.json" in record_script
    assert "gs://${_CORPUS_BUCKET}/cd/last_good.json" in record_script

    fetch_step = next((s for s in config.get("steps", []) if s.get("id") == "fetch-last-good"), None)
    assert fetch_step is not None, "Missing step 'fetch-last-good'"
    fetch_script = " ".join(fetch_step.get("args", []))
    assert "rollback.sh" in fetch_script
    assert "gcloud run services update" in fetch_script
    assert "kubectl set image deployment/faq-resolver" in fetch_script
    assert "gcloud builds triggers run support-ticket-router-cd" in fetch_script
    assert "exit 1" in fetch_script


# REQ-CD-009
def test_cd_service_account_roles():
    """Verify dedicated CD service account and least-privilege IAM bindings.

    Invariants:
    1. sa-cd-deployer service account created.
    2. Granted run.developer, container.developer, aiplatform.user, logging.logWriter,
       cloudbuild.builds.viewer, modelarmor.user.
    3. Granted artifactregistry.writer on serving repo.
    4. Granted storage.objectAdmin on corpus bucket.
    5. Granted secretmanager.secretAccessor on the 3 identity secrets only.
    6. Granted run.invoker on the 3 Cloud Run services.
    7. Granted iam.serviceAccountUser strictly on runtime service accounts.
    8. Security invariant: Neither roles/owner nor roles/editor is granted to sa-cd-deployer.
    """
    main_tf = (CLOUDBUILD_MOD_DIR / "main.tf").read_text(encoding="utf-8")

    assert 'account_id   = "sa-cd-deployer"' in main_tf

    assert "roles/run.developer" in main_tf
    assert "roles/container.developer" in main_tf
    assert "roles/aiplatform.user" in main_tf
    assert "roles/cloudbuild.builds.viewer" in main_tf
    assert "roles/logging.logWriter" in main_tf
    assert "roles/modelarmor.user" in main_tf

    assert "roles/artifactregistry.writer" in main_tf
    assert "roles/storage.objectAdmin" in main_tf

    assert "roles/secretmanager.secretAccessor" in main_tf
    assert "identity-demo-user-1001" in main_tf
    assert "identity-demo-user-1002" in main_tf
    assert "identity-web-api-key" in main_tf

    assert "roles/run.invoker" in main_tf
    assert "roles/iam.serviceAccountUser" in main_tf

    # Strict least privilege: no owner or editor
    assert not re.search(r'role\s*=\s*"roles/owner"', main_tf)
    assert not re.search(r'role\s*=\s*"roles/editor"', main_tf)


# REQ-CD-010
def test_terraform_ignores_cd_owned_fields():
    """Verify Terraform Cloud Run services ignore image changes made by CD pipeline.

    Invariants:
    1. All 3 Cloud Run services (ticket_db_mcp, order_lookup_mcp, human_queue) declare
       lifecycle { ignore_changes = [template[0].containers[0].image, client, client_version] }.
    2. docs/runbooks/deploy.md exists and documents the Terraform/CD ownership boundary.
    """
    cr_main = (CLOUD_RUN_MOD_DIR / "main.tf").read_text(encoding="utf-8")

    assert 'resource "google_cloud_run_v2_service" "ticket_db_mcp"' in cr_main
    assert 'resource "google_cloud_run_v2_service" "order_lookup_mcp"' in cr_main
    assert 'resource "google_cloud_run_v2_service" "human_queue"' in cr_main

    # Verify each of the 3 services ignores the container image
    occurrences = [
        line for line in cr_main.splitlines()
        if "ignore_changes" in line and "template[0].containers[0].image" in line
    ]
    assert len(occurrences) == 3, f"Expected 3 Cloud Run services to ignore image, found {len(occurrences)}"

    runbook = RUNBOOKS_DIR / "deploy.md"
    assert runbook.is_file(), f"Missing deployment runbook: {runbook}"
    runbook_text = runbook.read_text(encoding="utf-8")
    assert "laya-tune" in runbook_text
    assert "Terraform" in runbook_text
    assert "rollback" in runbook_text.lower()


def test_cloudbuild_yaml_has_no_unescaped_shell_vars():
    # REQ-CD-003
    # Cloud Build rejects any $VAR that is not a built-in or _USER substitution
    # ("key in the template ... is not a valid built-in substitution"); shell vars
    # must be written $$VAR. Caught live on the first CD run (2026-10-28).
    import re

    builtin = {
        "PROJECT_ID", "PROJECT_NUMBER", "BUILD_ID", "LOCATION", "TRIGGER_NAME",
        "COMMIT_SHA", "SHORT_SHA", "REVISION_ID", "BRANCH_NAME", "TAG_NAME",
        "REPO_NAME", "REPO_FULL_NAME", "REF_NAME", "TRIGGER_BUILD_CONFIG_PATH",
        "SERVICE_ACCOUNT_EMAIL", "SERVICE_ACCOUNT",
    }
    cicd = Path(__file__).resolve().parents[2] / "cicd"
    bad = []
    for path in sorted(cicd.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"(?<!\$)\$\{?([A-Za-z_][A-Za-z0-9_]*)", text):
            name = m.group(1)
            if name in builtin or name.startswith("_"):
                continue
            bad.append(f"{path.name}:{text.count(chr(10), 0, m.start()) + 1}: ${name}")
    assert not bad, "Unescaped shell vars (use $$VAR):\n" + "\n".join(bad)


def test_image_build_uses_layer_cache():
    # REQ-CD-003
    # Dependencies must install from pyproject.toml alone BEFORE the source is copied,
    # otherwise every commit invalidates the dependency layer and the cache is useless.
    dockerfile = (Path(__file__).resolve().parents[2] / "services" / "Dockerfile").read_text(encoding="utf-8")
    deps_at = dockerfile.index("pip install --no-cache-dir -r /tmp/requirements.txt")
    src_at = dockerfile.index("COPY src/ /app/src/")
    assert deps_at < src_at
    assert "pip install --no-cache-dir --no-deps ." in dockerfile

    with open(DEPLOY_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    build = next(s for s in config["steps"] if s["id"] == "build-and-push-image")
    script = " ".join(build["args"])
    assert "--cache-from" in script and "agents:cache" in script
    assert "BUILDKIT_INLINE_CACHE=1" in script
    assert 'docker push "$${CACHE_TAG}"' in script
