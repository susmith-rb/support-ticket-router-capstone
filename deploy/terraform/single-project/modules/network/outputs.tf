# deploy/terraform/single-project/modules/network/outputs.tf

output "network_name" {
  description = "Name of the VPC network"
  value       = google_compute_network.support_vpc.name
}

output "network_id" {
  description = "ID of the VPC network"
  value       = google_compute_network.support_vpc.id
}

output "network_self_link" {
  description = "Self-link of the VPC network"
  value       = google_compute_network.support_vpc.self_link
}

output "subnetwork_name" {
  description = "Name of the subnetwork"
  value       = google_compute_subnetwork.support_us_central1.name
}

output "subnetwork_id" {
  description = "ID of the subnetwork"
  value       = google_compute_subnetwork.support_us_central1.id
}

output "subnetwork_self_link" {
  description = "Self-link of the subnetwork"
  value       = google_compute_subnetwork.support_us_central1.self_link
}

output "pods_secondary_range_name" {
  description = "Secondary range name for GKE pods"
  value       = "gke-pods"
}

output "services_secondary_range_name" {
  description = "Secondary range name for GKE services"
  value       = "gke-services"
}

output "faq_resolver_ip" {
  description = "Global static IP address for FaqResolver GKE Ingress"
  value       = google_compute_global_address.faq_resolver.address
}

output "faq_resolver_ip_name" {
  description = "Name of the global static IP for FaqResolver GKE Ingress"
  value       = google_compute_global_address.faq_resolver.name
}

