# deploy/terraform/agent-gateway/main.tf
# Agent Gateway Egress Governance for TriageRouter (REQ-AUTH-007)
# Governs outbound calls from TriageRouter to FaqResolver (A2A) and core Google APIs.

provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

provider "google-beta" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

# -----------------------------------------------------------------------------
# Required Project Services
# -----------------------------------------------------------------------------
locals {
  # compute, iam and modelarmor are already owned by the single-project root;
  # this root only enables the APIs that are new for Agent Gateway.
  required_services = [
    "networksecurity.googleapis.com",
    "networkservices.googleapis.com",
    "iap.googleapis.com",
    "agentregistry.googleapis.com",
    "serviceextensions.googleapis.com",
  ]
}

resource "google_project_service" "services" {
  for_each           = toset(local.required_services)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# -----------------------------------------------------------------------------
# Agent Gateway: tr-egress
# -----------------------------------------------------------------------------
resource "google_network_services_agent_gateway" "tr_egress" {
  provider = google-beta
  name     = "tr-egress"
  location = var.region

  google_managed {
    governed_access_path = "AGENT_TO_ANYWHERE"
  }

  registries = [
    "//agentregistry.googleapis.com/projects/${var.project_id}/locations/${var.region}"
  ]

  depends_on = [
    google_project_service.services["networkservices.googleapis.com"],
    google_project_service.services["agentregistry.googleapis.com"],
  ]
}

# -----------------------------------------------------------------------------
# IAP Authz Extension & Authz Policy
# -----------------------------------------------------------------------------
resource "google_network_services_authz_extension" "iap_authz" {
  provider  = google-beta
  name      = "tr-iap-authz-extension"
  location  = var.region
  service   = "iap.googleapis.com"
  timeout   = "1s"
  fail_open = false

  # When var.enforcement_mode == "ENFORCE", iamEnforcementMode is omitted.
  # In DRY_RUN, iamEnforcementMode="DRY_RUN" logs policy decisions without blocking.
  metadata = var.enforcement_mode == "ENFORCE" ? {
    iapPolicyVersion = "V2"
    } : {
    iapPolicyVersion   = "V2"
    iamEnforcementMode = "DRY_RUN"
  }

  depends_on = [
    google_project_service.services["networkservices.googleapis.com"],
    google_project_service.services["iap.googleapis.com"],
  ]
}

resource "google_network_security_authz_policy" "tr_authz_policy" {
  provider       = google-beta
  name           = "tr-egress-authz-policy"
  location       = var.region
  policy_profile = "REQUEST_AUTHZ"
  action         = "CUSTOM"

  target {
    resources = [google_network_services_agent_gateway.tr_egress.id]
  }

  custom_provider {
    authz_extension {
      resources = [google_network_services_authz_extension.iap_authz.id]
    }
  }

  depends_on = [
    google_project_service.services["networksecurity.googleapis.com"],
  ]
}

# -----------------------------------------------------------------------------
# Agent Registry: FaqResolver A2A Agent & Composite Core-GAPI Endpoint
# -----------------------------------------------------------------------------
resource "google_agent_registry_service" "faq_resolver" {
  provider   = google-beta
  service_id = "faq-resolver"
  location   = var.region

  display_name = "FaqResolver"

  # A2A_AGENT_CARD services must not set `interfaces`; the agent card carries the URL.
  agent_spec {
    type    = "A2A_AGENT_CARD"
    content = file("${path.module}/agent-cards/faq-resolver.json")
  }

  depends_on = [
    google_project_service.services["agentregistry.googleapis.com"],
  ]
}

locals {
  core_gapi_hosts = [
    "aiplatform.googleapis.com",
    "aiplatform.mtls.googleapis.com",
    "us-central1-aiplatform.googleapis.com",
    "us-central1-aiplatform.mtls.googleapis.com",
    "aiplatform.us-central1.rep.googleapis.com",
    "modelarmor.us-central1.rep.googleapis.com",
    "modelarmor.us-central1.rep.mtls.googleapis.com",
    "cloudkms.googleapis.com",
    "cloudkms.mtls.googleapis.com",
    "pubsub.googleapis.com",
    "pubsub.mtls.googleapis.com",
    "iamcredentials.googleapis.com",
    "iamcredentials.mtls.googleapis.com",
    "cloudtrace.googleapis.com",
    "cloudtrace.mtls.googleapis.com",
    "telemetry.googleapis.com",
    "telemetry.mtls.googleapis.com",
    "logging.googleapis.com",
    "logging.mtls.googleapis.com",
    "monitoring.googleapis.com",
    "monitoring.mtls.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudresourcemanager.mtls.googleapis.com",
    "secretmanager.googleapis.com",
    "secretmanager.mtls.googleapis.com",
    "agentregistry.googleapis.com",
    var.router_s1_dedicated_host,
    var.ticket_db_mcp_host,
    "oauth2.googleapis.com",
    "sts.googleapis.com",
    "www.googleapis.com",
    "accounts.google.com",
  ]

  core_gapi_urls = [
    for host in local.core_gapi_hosts : "https://${host}"
  ]
}

resource "google_agent_registry_service" "core_gapi" {
  provider     = google-beta
  service_id   = "core-gapi"
  location     = var.region
  display_name = "Core Google APIs used by TriageRouter"

  dynamic "interfaces" {
    for_each = local.core_gapi_urls
    content {
      url              = interfaces.value
      protocol_binding = "HTTP_JSON"
    }
  }

  endpoint_spec {
    type = "NO_SPEC"
  }

  depends_on = [
    google_project_service.services["agentregistry.googleapis.com"],
  ]
}

# -----------------------------------------------------------------------------
# Unified Access Policy (IAM v3 UAP) for Agent Egress
# -----------------------------------------------------------------------------
# Grants iap.googleapis.com/resources.egressViaIAP ONLY to var.triage_agent_principal
# restricted to destinations matching the FaqResolver agent or core-gapi endpoint.
resource "google_iam_project_access_policy" "triage_egress" {
  provider         = google-beta
  project          = var.project_id
  location         = "global"
  access_policy_id = "tr-egress-uap"
  display_name     = "TriageRouter Agent Gateway egress"

  details {
    rules {
      description = "Allow only the TriageRouter agent identity to egress via Agent Gateway to FaqResolver and core Google APIs"
      effect      = "ALLOW"
      principals  = [var.triage_agent_principal]

      operation {
        permissions = ["iap.googleapis.com/resources.egressViaIAP"]
      }

      conditions {
        service    = "iap.googleapis.com"
        expression = "destination.agent_registry.agent.name == '${google_agent_registry_service.faq_resolver.registry_resource}' || destination.agent_registry.endpoint.name == '${google_agent_registry_service.core_gapi.registry_resource}'"
      }
    }
  }

  depends_on = [
    google_project_service.services["iap.googleapis.com"],
  ]
}

# The ACCESS-kind policy binding for tr-egress-uap is created by ./bind_uap.sh:
# provider 7.46 only supports `target.principal_set`, and access-policy bindings
# need `target.resource` (API error "Policy Binding target must be a resource for
# Access Policy"). Move it back to google_iam_projects_policy_binding when supported.
