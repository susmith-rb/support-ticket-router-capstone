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

  # ARCHITECTURAL DECISION & SECURITY NOTE ON NODE SERVICE ACCOUNT:
  # Autopilot clusters default to the Compute Engine default service account
  # (<project_number>-compute@developer.gserviceaccount.com).
  # Changing cluster node service account on an active cluster forces recreation/replacement.
  # To avoid cluster replacement during live operations, image pull access on Artifact Registry
  # repo 'serving' is granted to this default SA via modules/router-endpoint (REQ-AGENT-021).
  # TODO: On next cluster rebuild, provision dedicated least-privilege node SA 'gke-nodes' with:
  # - roles/logging.logWriter
  # - roles/monitoring.metricWriter
  # - roles/monitoring.viewer
  # - roles/stackdriver.resourceMetadata.writer
  # - roles/artifactregistry.reader

  deletion_protection = var.deletion_protection


  resource_labels = var.labels
}
