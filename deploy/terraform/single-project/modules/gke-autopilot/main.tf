# deploy/terraform/single-project/modules/gke-autopilot/main.tf
# GKE Autopilot cluster for containerized agents (FaqResolverAgent).
# Implements REQ-AGENT-019: Regional Autopilot cluster with Workload Identity.

resource "google_container_cluster" "support_agents" {
  name       = var.cluster_name
  location   = var.region
  project    = var.project_id
  network    = var.network
  subnetwork = var.subnetwork

  enable_autopilot = true

  release_channel {
    channel = "REGULAR"
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.cluster_secondary_range_name
    services_secondary_range_name = var.services_secondary_range_name
  }

  # Capstone network posture: private worker nodes with public control plane endpoint
  # Allows administrative access via kubectl and CI/CD without dedicated bastion VM.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }

  deletion_protection = var.deletion_protection

  resource_labels = var.labels
}
