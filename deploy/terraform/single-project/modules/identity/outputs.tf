output "identity_web_api_key_secret_id" {
  description = "Secret Manager secret ID containing the Identity Platform Web API key"
  value       = google_secret_manager_secret.identity_web_api_key.secret_id
}
