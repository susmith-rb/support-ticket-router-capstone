output "faq_articles_data_store_id" {
  description = "Data store ID for faq-articles"
  value       = module.agent_search.faq_articles_data_store_id
}

output "resolved_threads_data_store_id" {
  description = "Data store ID for resolved-threads"
  value       = module.agent_search.resolved_threads_data_store_id
}

output "search_engine_id" {
  description = "Engine ID for support-kb-search"
  value       = module.agent_search.search_engine_id
}

output "search_location" {
  description = "Location for Agent Search resources"
  value       = module.agent_search.search_location
}

output "search_tier" {
  description = "Search tier for Agent Search"
  value       = module.agent_search.search_tier
}

output "discovery_engine_service_account" {
  description = "Email of the Discovery Engine service agent"
  value       = google_project_service_identity.discovery_engine.email
}

output "labels" {
  description = "Configured resource labels"
  value       = var.labels
}

output "cloudbuild_service_account" {
  description = "Email of the Cloud Build service account"
  value       = module.cloudbuild.cloudbuild_service_account
}

output "scheduler_service_account" {
  description = "Email of the Cloud Scheduler service account"
  value       = module.cloudbuild.scheduler_service_account
}

output "nightly_trigger_id" {
  description = "ID of the nightly Cloud Build trigger"
  value       = module.cloudbuild.nightly_trigger_id
}

output "scheduler_job_name" {
  description = "Name of the Cloud Scheduler job"
  value       = module.cloudbuild.scheduler_job_name
}

output "router_endpoint_id" {
  description = "Resource ID of the router-s1 Vertex AI endpoint"
  value       = module.router_endpoint.endpoint_id
}

output "router_endpoint_name" {
  description = "Display name of the router-s1 Vertex AI endpoint"
  value       = module.router_endpoint.endpoint_name
}

output "router_serving_service_account" {
  description = "Email of the model serving service account"
  value       = module.router_endpoint.service_account_email
}

output "serving_artifact_registry_repo" {
  description = "Repository URL for serving container images in Artifact Registry"
  value       = module.router_endpoint.artifact_registry_repository_url
}
