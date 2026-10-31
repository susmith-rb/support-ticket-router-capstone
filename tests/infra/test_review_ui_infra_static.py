"""Static Terraform, IAM, and CD verification for Human Review UI behind Cloud Run IAP.

# REQ-AGENT-034: Human Review UI with Cloud Run IAP and Verified Rep Identity
# REQ-AUTH-010: Human Review UI Rep Identity Assertion and sa-review-ui Least Privilege
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"
MOD_DIR = TF_DIR / "modules"
CLOUD_RUN_MOD_DIR = MOD_DIR / "cloud-run-services"
DEPLOY_YAML = REPO_ROOT / "cicd" / "cloudbuild-deploy.yaml"


# REQ-AUTH-010
def test_review_ui_infra_contract():
    """Verify Terraform configuration for human-review-ui service, direct IAP, and least-privilege IAM.

    Invariants:
    1. human-review-ui declared with provider = google-beta, iap_enabled = true, launch_stage = BETA.
    2. Service account sa-review-ui created as dedicated keyless identity.
    3. sa-review-ui granted roles/run.invoker on human-queue ONLY (no other MCP services, no DB roles).
    4. Cloud Run IAP service agent granted roles/run.invoker on human-review-ui.
    5. review_ui_reps variable defaults to user:susmith@gcp.altostrat.com and is granted roles/iap.httpsResourceAccessor.
    6. human-queue receives REVIEW_UI_CALLER_EMAILS matching sa-review-ui email.
    7. Root outputs expose human_review_ui_url and sa_review_ui_email.
    8. iap.googleapis.com is enabled in project services.
    """
    cr_main = (CLOUD_RUN_MOD_DIR / "main.tf").read_text(encoding="utf-8")
    cr_vars = (CLOUD_RUN_MOD_DIR / "variables.tf").read_text(encoding="utf-8")
    cr_outputs = (CLOUD_RUN_MOD_DIR / "outputs.tf").read_text(encoding="utf-8")
    root_main = (TF_DIR / "main.tf").read_text(encoding="utf-8")
    root_vars = (TF_DIR / "variables.tf").read_text(encoding="utf-8")
    root_outputs = (TF_DIR / "outputs.tf").read_text(encoding="utf-8")

    # 1. Cloud Run service human-review-ui with direct IAP
    assert 'resource "google_cloud_run_v2_service" "human_review_ui"' in cr_main
    assert "iap_enabled  = true" in cr_main or "iap_enabled = true" in cr_main
    assert 'launch_stage = "BETA"' in cr_main
    assert 'ingress      = "INGRESS_TRAFFIC_ALL"' in cr_main or 'ingress = "INGRESS_TRAFFIC_ALL"' in cr_main
    assert "provider = google-beta" in cr_main

    # 2. Service account sa-review-ui
    assert 'resource "google_service_account" "sa_review_ui"' in cr_main
    assert 'account_id   = "sa-review-ui"' in cr_main
    assert "service_account = google_service_account.sa_review_ui.email" in cr_main

    # 3. sa-review-ui invoker on human-queue ONLY
    assert 'resource "google_cloud_run_v2_service_iam_member" "human_queue_invoker_review_ui"' in cr_main
    assert "name     = google_cloud_run_v2_service.human_queue[0].name" in cr_main
    assert 'role     = "roles/run.invoker"' in cr_main
    assert "member   = \"serviceAccount:${google_service_account.sa_review_ui.email}\"" in cr_main

    # Assert sa-review-ui is NOT granted invoker on ticket_db or order_lookup
    assert "ticket_db_mcp[0].name\n  role     = \"roles/run.invoker\"\n  member   = \"serviceAccount:${google_service_account.sa_review_ui.email}\"" not in cr_main
    assert "order_lookup_mcp[0].name\n  role     = \"roles/run.invoker\"\n  member   = \"serviceAccount:${google_service_account.sa_review_ui.email}\"" not in cr_main

    # 4. IAP service agent invoker on human-review-ui
    assert 'resource "google_cloud_run_v2_service_iam_member" "human_review_ui_iap_invoker"' in cr_main
    assert "name     = google_cloud_run_v2_service.human_review_ui[0].name" in cr_main
    assert "gcp-sa-iap.iam.gserviceaccount.com" in cr_main

    # 5. review_ui_reps variable and IAP IAM binding
    assert 'variable "review_ui_reps"' in cr_vars
    assert "user:susmith@gcp.altostrat.com" in cr_vars
    assert 'variable "review_ui_reps"' in root_vars
    assert "user:susmith@gcp.altostrat.com" in root_vars
    assert 'resource "google_iap_web_cloud_run_service_iam_member" "review_ui_reps"' in cr_main
    assert 'role                   = "roles/iap.httpsResourceAccessor"' in cr_main
    assert "cloud_run_service_name = google_cloud_run_v2_service.human_review_ui[0].name" in cr_main

    # 6. human-queue receives REVIEW_UI_CALLER_EMAILS
    assert 'name  = "REVIEW_UI_CALLER_EMAILS"' in cr_main
    assert "value = google_service_account.sa_review_ui.email" in cr_main

    # 7. Outputs
    assert "human_review_ui_url" in cr_outputs
    assert "sa_review_ui_email" in cr_outputs
    assert "human_review_ui_url" in root_outputs
    assert "sa_review_ui_email" in root_outputs

    # 8. iap.googleapis.com is owned by the agent-gateway root, never by this root
    assert '"iap.googleapis.com"' not in root_main.split("# iap.googleapis.com is owned")[0].split("services")[-1]
    gw_main = (REPO_ROOT / "deploy" / "terraform" / "agent-gateway" / "main.tf").read_text(encoding="utf-8")
    assert '"iap.googleapis.com"' in gw_main


# REQ-CD-004
def test_cd_pipeline_deploys_and_rolls_back_human_review_ui():
    """Verify CD pipeline deploys and rolls back human-review-ui service."""
    with open(DEPLOY_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Rollback script
    fetch_step = next((s for s in config.get("steps", []) if s.get("id") == "fetch-last-good"), None)
    assert fetch_step is not None
    rollback_script = " ".join(fetch_step.get("args", []))
    assert "human-review-ui" in rollback_script

    # Deploy step
    deploy_step = next((s for s in config.get("steps", []) if s.get("id") == "deploy-cloud-run"), None)
    assert deploy_step is not None
    deploy_script = " ".join(deploy_step.get("args", []))
    assert "human-review-ui" in deploy_script
