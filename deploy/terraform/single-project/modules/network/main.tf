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

# -----------------------------------------------------------------------------
# Cloud NAT (egress for private GKE nodes)
# -----------------------------------------------------------------------------
# Private Google Access covers global *.googleapis.com only. FaqResolver must reach
# Model Armor's REGIONAL endpoint (modelarmor.us-central1.rep.googleapis.com) to
# screen generated answers (REQ-MA-004); without NAT those connections time out.
# Decision 2026-10-28 (user): add NAT (~$3-5/mo at 2-3 Autopilot nodes + $0.045/GB).
# Supersedes the earlier "no NAT" cost note.

resource "google_compute_router" "support_router" {
  name    = "${var.network_name}-router"
  project = var.project_id
  region  = var.region
  network = google_compute_network.support_vpc.id
}

resource "google_compute_router_nat" "support_nat" {
  name                               = "${var.network_name}-nat"
  project                            = var.project_id
  region                             = var.region
  router                             = google_compute_router.support_router.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

