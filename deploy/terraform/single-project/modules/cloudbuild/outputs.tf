output "cloudbuild_service_account" {
  description = "Email of the Cloud Build pipeline service account"
  value       = google_service_account.cloudbuild_sa.email
}

output "scheduler_service_account" {
  description = "Email of the Cloud Scheduler service account"
  value       = google_service_account.scheduler_sa.email
}

output "nightly_trigger_id" {
  description = "ID of the nightly Cloud Build trigger"
  value       = google_cloudbuild_trigger.nightly.trigger_id
}

output "scheduler_job_name" {
  description = "Name of the Cloud Scheduler job"
  value       = google_cloud_scheduler_job.nightly_eval.name
}

output "github_connection_id" {
  description = "ID of the Cloud Build v2 GitHub connection (if enabled)"
  value       = var.github_connection_ready ? google_cloudbuildv2_connection.github[0].id : null
}

output "github_repository_id" {
  description = "ID of the Cloud Build v2 repository resource (if enabled)"
  value       = var.github_connection_ready ? google_cloudbuildv2_repository.repo[0].id : null
}
