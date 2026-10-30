# deploy/terraform/single-project/modules/redaction-purge/outputs.tf
# Outputs for redaction-purge module.

output "service_account_email" {
  description = "Email of the Cloud Scheduler redaction purge service account"
  value       = google_service_account.redaction_purge.email
}

output "service_account_id" {
  description = "Resource ID of the Cloud Scheduler redaction purge service account"
  value       = google_service_account.redaction_purge.id
}

output "scheduler_job_id" {
  description = "Resource ID of the Cloud Scheduler redaction map purge job"
  value       = google_cloud_scheduler_job.redaction_map_purge.id
}

output "endpoint_url" {
  description = "Full URL of the internal purge endpoint targeted by Cloud Scheduler"
  value       = local.endpoint_url
}
