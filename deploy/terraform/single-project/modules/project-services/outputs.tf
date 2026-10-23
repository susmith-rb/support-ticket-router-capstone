output "enabled_services" {
  description = "List of enabled GCP service APIs"
  value       = [for s in google_project_service.services : s.service]
}
