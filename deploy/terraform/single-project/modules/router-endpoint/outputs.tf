# deploy/terraform/single-project/modules/router-endpoint/outputs.tf

output "endpoint_id" {
  description = "Resource ID of the Vertex AI Endpoint"
  value       = var.enable_router_endpoint && length(google_vertex_ai_endpoint.router_s1) > 0 ? google_vertex_ai_endpoint.router_s1[0].id : ""
}

output "endpoint_name" {
  description = "Display name of the Vertex AI Endpoint"
  value       = var.enable_router_endpoint && length(google_vertex_ai_endpoint.router_s1) > 0 ? google_vertex_ai_endpoint.router_s1[0].name : ""
}

output "service_account_email" {
  description = "Email of the model serving service account"
  value       = google_service_account.ticket_router_serving.email
}

output "artifact_registry_repository_id" {
  description = "Repository ID of the serving Docker repository"
  value       = google_artifact_registry_repository.serving.repository_id
}

output "artifact_registry_repository_url" {
  description = "Docker repository URI for pushing serving container images"
  value       = "${var.laya_region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.serving.repository_id}"
}

output "dedicated_endpoint_id" {
  description = "Resource ID of the dedicated Vertex AI Endpoint"
  value       = google_vertex_ai_endpoint.router_s1_dedicated.id
}

output "dedicated_endpoint_name" {
  description = "Name of the dedicated Vertex AI Endpoint"
  value       = google_vertex_ai_endpoint.router_s1_dedicated.name
}

output "dedicated_endpoint_dns" {
  description = "Dedicated DNS of the Vertex AI Endpoint"
  value       = google_vertex_ai_endpoint.router_s1_dedicated.dedicated_endpoint_dns
}

