output "ge_data_store_id" {
  description = "Data store ID for the Gemini Enterprise default backing store"
  value       = google_discovery_engine_data_store.ge_default.data_store_id
}

output "ge_engine_id" {
  description = "Engine ID for the Gemini Enterprise Web App search engine"
  value       = var.ge_engine_id
}

output "ge_location" {
  description = "Location for the Gemini Enterprise resources"
  value       = var.location
}

output "ge_oauth_credentials_secret_id" {
  description = "Secret Manager secret ID for Gemini Enterprise OAuth client credentials"
  value       = google_secret_manager_secret.ge_oauth_client_credentials.secret_id
}

