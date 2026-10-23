output "faq_articles_data_store_id" {
  description = "Data store ID for faq-articles"
  value       = google_discovery_engine_data_store.faq_articles.data_store_id
}

output "faq_articles_data_store_name" {
  description = "Full resource name of faq-articles data store"
  value       = google_discovery_engine_data_store.faq_articles.name
}

output "resolved_threads_data_store_id" {
  description = "Data store ID for resolved-threads"
  value       = google_discovery_engine_data_store.resolved_threads.data_store_id
}

output "resolved_threads_data_store_name" {
  description = "Full resource name of resolved-threads data store"
  value       = google_discovery_engine_data_store.resolved_threads.name
}

output "search_engine_id" {
  description = "Engine ID for support-kb-search"
  value       = google_discovery_engine_search_engine.support_kb_search.engine_id
}

output "search_engine_name" {
  description = "Full resource name of support-kb-search engine"
  value       = google_discovery_engine_search_engine.support_kb_search.name
}

output "search_location" {
  description = "Location of the search engine and data stores"
  value       = var.search_location
}

output "search_tier" {
  description = "Configured search tier"
  value       = var.search_tier
}
