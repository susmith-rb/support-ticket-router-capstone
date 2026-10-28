# -----------------------------------------------------------------------------
# Cloud Build Service Account & Least-Privilege IAM (TDD §12.6, PLAN8)
# -----------------------------------------------------------------------------

resource "google_service_account" "cloudbuild_sa" {
  account_id   = "ticket-router-build"
  display_name = "Cloud Build Service Account for Ticket Router CI/CD"
  project      = var.project_id
}

resource "google_project_iam_member" "cloudbuild_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.cloudbuild_sa.email}"
}

resource "google_project_iam_member" "cloudbuild_discoveryengine" {
  project = var.project_id
  role    = "roles/discoveryengine.editor"
  member  = "serviceAccount:${google_service_account.cloudbuild_sa.email}"
}

resource "google_storage_bucket_iam_member" "cloudbuild_corpus_storage" {
  bucket = var.corpus_bucket_name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cloudbuild_sa.email}"
}

# -----------------------------------------------------------------------------
# Cloud Scheduler Service Account & IAM (PLAN8)
# -----------------------------------------------------------------------------

resource "google_service_account" "scheduler_sa" {
  account_id   = "ticket-router-scheduler"
  display_name = "Cloud Scheduler SA for Nightly CI/CD Trigger"
  project      = var.project_id
}

resource "google_project_iam_member" "scheduler_builds_editor" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.editor"
  member  = "serviceAccount:${google_service_account.scheduler_sa.email}"
}

# -----------------------------------------------------------------------------
# Cloud Build v2 GitHub Connection & Repository (gated by github_connection_ready)
# -----------------------------------------------------------------------------

resource "google_cloudbuildv2_connection" "github" {
  count    = var.github_connection_ready ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = var.github_connection_name

  github_config {
    app_installation_id = var.github_app_installation_id
    authorizer_credential {
      oauth_token_secret_version = var.github_oauth_secret_version
    }
  }
}

# -----------------------------------------------------------------------------
# CD Deployment Service Account & Least-Privilege IAM (REQ-CD-009)
# -----------------------------------------------------------------------------

resource "google_service_account" "cd_sa" {
  account_id   = "sa-cd-deployer"
  display_name = "Cloud Build CD Service Account for Ticket Router Deployments"
  project      = var.project_id
}

locals {
  cd_project_roles = [
    "roles/run.developer",
    "roles/container.developer",
    "roles/aiplatform.user",
    "roles/cloudbuild.builds.viewer",
    "roles/logging.logWriter",
    "roles/modelarmor.user",
  ]

  cd_secret_ids = [
    "identity-demo-user-1001",
    "identity-demo-user-1002",
    "identity-web-api-key",
  ]

  cd_cloud_run_services = [
    "ticket-db-mcp",
    "order-lookup-mcp",
    "human-queue",
  ]

  cd_runtime_service_account_emails = [
    "sa-ticket-mcp@${var.project_id}.iam.gserviceaccount.com",
    "sa-order-mcp@${var.project_id}.iam.gserviceaccount.com",
    "sa-queue@${var.project_id}.iam.gserviceaccount.com",
    "sa-triage@${var.project_id}.iam.gserviceaccount.com",
  ]
}

# Project-level permissions
resource "google_project_iam_member" "cd_project_roles" {
  for_each = toset(local.cd_project_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.cd_sa.email}"
}

# Artifact Registry writer on serving repo
resource "google_artifact_registry_repository_iam_member" "cd_serving_writer" {
  project    = var.project_id
  location   = var.region
  repository = "serving"
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.cd_sa.email}"
}

# Storage objectAdmin on laya staging/corpus bucket (staging + last_good.json)
resource "google_storage_bucket_iam_member" "cd_corpus_storage" {
  bucket = var.corpus_bucket_name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cd_sa.email}"
}

# Secret Manager secretAccessor on the 3 identity secrets only
resource "google_secret_manager_secret_iam_member" "cd_identity_secrets" {
  for_each  = toset(local.cd_secret_ids)
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cd_sa.email}"
}

# Cloud Run invoker on the 3 services for smoke testing
resource "google_cloud_run_v2_service_iam_member" "cd_run_invoker" {
  for_each = toset(local.cd_cloud_run_services)
  project  = var.project_id
  location = var.region
  name     = each.value
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.cd_sa.email}"
}

# Service Account User only on runtime service accounts
resource "google_service_account_iam_member" "cd_sa_user" {
  for_each           = toset(local.cd_runtime_service_account_emails)
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.cd_sa.email}"
}

# Approvers for CD deployments (REQ-CD-001)
resource "google_project_iam_member" "cd_approvers" {
  for_each = toset(var.cd_approvers)
  project  = var.project_id
  role     = "roles/cloudbuild.builds.approver"
  member   = each.value
}

# -----------------------------------------------------------------------------
# CD Deployment Trigger (push to main with manual approval gate) (REQ-CD-001)
# -----------------------------------------------------------------------------

resource "google_cloudbuild_trigger" "cd" {
  count           = var.github_connection_ready && var.enable_cd_trigger ? 1 : 0
  project         = var.project_id
  location        = var.region
  name            = "support-ticket-router-cd"
  description     = "CD pipeline executed on push to main branch with manual approval gate"
  service_account = google_service_account.cd_sa.id
  filename        = "cicd/cloudbuild-deploy.yaml"

  approval_config {
    approval_required = true
  }

  substitutions = {
    _CI_TRIGGER_ID   = length(google_cloudbuild_trigger.ci) > 0 ? google_cloudbuild_trigger.ci[0].trigger_id : ""
    _TRIAGE_RESOURCE = var.triage_resource_name
  }

  repository_event_config {
    repository = google_cloudbuildv2_repository.repo[0].id
    push {
      branch = "^main$"
    }
  }
}

resource "google_cloudbuildv2_repository" "repo" {
  count             = var.github_connection_ready ? 1 : 0
  project           = var.project_id
  location          = var.region
  name              = var.github_repo_name
  parent_connection = google_cloudbuildv2_connection.github[0].name
  remote_uri        = var.github_repo_uri
}

# -----------------------------------------------------------------------------
# Cloud Build Triggers
# -----------------------------------------------------------------------------

# CI Trigger on Push to main (gated by github_connection_ready)
resource "google_cloudbuild_trigger" "ci" {
  count           = var.github_connection_ready ? 1 : 0
  project         = var.project_id
  location        = var.region
  name            = "support-ticket-router-ci"
  description     = "CI pipeline executed on push to main branch"
  service_account = google_service_account.cloudbuild_sa.id
  filename        = "cicd/cloudbuild.yaml"
  disabled        = !var.enable_ci_triggers

  substitutions = {
    _RUN_EVAL = "auto"
  }

  repository_event_config {
    repository = google_cloudbuildv2_repository.repo[0].id
    push {
      branch = "^main$"
    }
  }
}

# CI Trigger on Pull Requests targeting main (gated by github_connection_ready)
resource "google_cloudbuild_trigger" "ci_pr" {
  count           = var.github_connection_ready ? 1 : 0
  project         = var.project_id
  location        = var.region
  name            = "support-ticket-router-ci-pr"
  description     = "CI pipeline executed on pull requests targeting main branch"
  service_account = google_service_account.cloudbuild_sa.id
  filename        = "cicd/cloudbuild.yaml"
  disabled        = !var.enable_ci_triggers

  substitutions = {
    _RUN_EVAL = "auto"
  }

  repository_event_config {
    repository = google_cloudbuildv2_repository.repo[0].id
    pull_request {
      branch = "^main$"
    }
  }
}

# Corpus Reindexing Trigger (gated by github_connection_ready)
resource "google_cloudbuild_trigger" "reindex" {
  count           = var.github_connection_ready ? 1 : 0
  project         = var.project_id
  location        = var.region
  name            = "support-ticket-router-reindex"
  description     = "Corpus reindexing pipeline triggered on changes to rag/corpus_v2/**"
  service_account = google_service_account.cloudbuild_sa.id
  filename        = "cicd/cloudbuild-reindex.yaml"
  included_files  = ["rag/corpus_v2/**"]
  disabled        = !var.enable_ci_triggers

  repository_event_config {
    repository = google_cloudbuildv2_repository.repo[0].id
    push {
      branch = "^main$"
    }
  }
}

# Nightly Retrieval Health & Drift Trigger (Manual-invocation, called by Scheduler)
resource "google_cloudbuild_trigger" "nightly" {
  project         = var.project_id
  location        = var.region
  name            = "support-ticket-router-nightly"
  description     = "Nightly retrieval health and drift evaluation against DEV query set"
  service_account = google_service_account.cloudbuild_sa.id
  # Nightly evals are a stretch item: keep the trigger disabled alongside the paused schedule.
  disabled = !var.enable_nightly_schedule

  git_file_source {
    path       = "cicd/cloudbuild-nightly.yaml"
    uri        = var.github_connection_ready ? null : var.github_repo_uri
    repository = var.github_connection_ready ? google_cloudbuildv2_repository.repo[0].id : null
    revision   = "refs/heads/main"
    repo_type  = "GITHUB"
  }

  source_to_build {
    uri        = var.github_connection_ready ? null : var.github_repo_uri
    repository = var.github_connection_ready ? google_cloudbuildv2_repository.repo[0].id : null
    ref        = "refs/heads/main"
    repo_type  = "GITHUB"
  }
}

# -----------------------------------------------------------------------------
# Cloud Scheduler Job (Daily at 02:00 UTC)
# -----------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "nightly_eval" {
  project          = var.project_id
  region           = var.region
  name             = "support-ticket-router-nightly-eval"
  description      = "Trigger nightly retrieval health and drift evaluation in Cloud Build"
  schedule         = var.nightly_schedule_cron
  time_zone        = "UTC"
  attempt_deadline = "320s"
  paused           = !var.enable_nightly_schedule

  http_target {
    http_method = "POST"
    uri         = "https://cloudbuild.googleapis.com/v1/${google_cloudbuild_trigger.nightly.id}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler_sa.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
}
