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
