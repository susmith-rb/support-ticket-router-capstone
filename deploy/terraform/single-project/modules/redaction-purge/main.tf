# deploy/terraform/single-project/modules/redaction-purge/main.tf
# Cloud Scheduler and IAM for daily 30-day TTL redaction map purge.
# Implements REQ-STORE-010: Redaction Map 30-Day TTL Purge.

locals {
  endpoint_url = "${trimsuffix(var.human_queue_url, "/")}/internal/purge-expired-redaction-maps"
  oidc_audience = var.purge_audience != "" ? var.purge_audience : local.endpoint_url
}

# -----------------------------------------------------------------------------
# 1. Service Account for Cloud Scheduler Purge Job (6-30 chars)
# -----------------------------------------------------------------------------
resource "google_service_account" "redaction_purge" {
  account_id   = "sa-redaction-purge"
  display_name = "Redaction Map Purge Scheduler Service Account"
  project      = var.project_id
}

# -----------------------------------------------------------------------------
# 2. Cloud Run Invoker IAM Grant for Scheduler SA on human-queue service
# -----------------------------------------------------------------------------
resource "google_cloud_run_v2_service_iam_member" "human_queue_purge_invoker" {
  project  = var.project_id
  location = var.region
  name     = var.human_queue_service_name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.redaction_purge.email}"
}

# -----------------------------------------------------------------------------
# 3. Daily Cloud Scheduler Job (03:15 UTC)
# -----------------------------------------------------------------------------
resource "google_cloud_scheduler_job" "redaction_map_purge" {
  name             = "redaction-map-purge"
  description      = "Daily purge of expired PII redaction maps past 30-day TTL (REQ-STORE-010)"
  schedule         = "15 3 * * *"
  time_zone        = "UTC"
  region           = var.region
  project          = var.project_id
  attempt_deadline = "320s"

  http_target {
    http_method = "POST"
    uri         = local.endpoint_url

    oidc_token {
      service_account_email = google_service_account.redaction_purge.email
      audience              = local.oidc_audience
    }
  }
}
