# deploy/terraform/single-project/modules/network/main.tf
# Dedicated VPC and subnetwork for GKE Autopilot and agent workloads.
# Implements REQ-AGENT-018: Private GKE network with Private Google Access.

resource "google_compute_network" "support_vpc" {
  name                    = var.network_name
  auto_create_subnetworks = false
  project                 = var.project_id
  description             = "VPC network for support ticket router agents and microservices"
}

resource "google_compute_subnetwork" "support_us_central1" {
  name                     = var.subnetwork_name
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.support_vpc.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "gke-pods"
    ip_cidr_range = var.pods_cidr
  }

  secondary_ip_range {
    range_name    = "gke-services"
    ip_cidr_range = var.services_cidr
  }
}

# -----------------------------------------------------------------------------
# Global Static IP for FaqResolver Ingress
# -----------------------------------------------------------------------------

resource "google_compute_global_address" "faq_resolver" {
  name        = "faq-resolver-ip"
  project     = var.project_id
  description = "Global static external IPv4 address for FaqResolver GKE Ingress"
}


# ARCHITECTURAL DECISION NOTE ON CLOUD NAT:
# Cloud NAT is deliberately omitted to preserve minimal capstone cost posture (~$30/mo savings).
# Private GKE worker nodes reach all required services (Artifact Registry, Vertex AI, Agent Search)
# via Private Google Access enabled on the subnetwork. FaqResolver does not connect to the public
# internet or directly to Cloud SQL (all database queries route through Cloud Run MCP servers).
