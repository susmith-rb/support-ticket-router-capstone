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
