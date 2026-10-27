output "cluster_name" {
  description = "Name of the GKE Autopilot cluster"
  value       = google_container_cluster.support_agents.name
}

output "cluster_id" {
  description = "ID of the GKE Autopilot cluster"
  value       = google_container_cluster.support_agents.id
}

output "cluster_endpoint" {
  description = "Cluster endpoint IP address"
  value       = google_container_cluster.support_agents.endpoint
}

output "endpoint" {
  description = "Alias for cluster_endpoint"
  value       = google_container_cluster.support_agents.endpoint
}

