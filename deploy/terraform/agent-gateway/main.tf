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
  # REQ-AUTH-007: IAP evaluates registry names in project-ID form
  # (projects/<project_id>/...), while the provider's registry_resource uses the
  # project number, so each registry match accepts both forms. gRPC trace export
  # (cloudtrace.mtls BatchWriteSpans) does not match an HTTP_JSON interface and
  # arrives as an unregistered destination, so it is allowed by exact host + path.
  # Verified with IAP data-access audit logs in DRY_RUN (2026-10-31).
  faq_agent_names = [
    google_agent_registry_service.faq_resolver.registry_resource,
    replace(google_agent_registry_service.faq_resolver.registry_resource, "projects/${var.project_number}/", "projects/${var.project_id}/"),
  ]
  core_gapi_endpoint_names = [
    google_agent_registry_service.core_gapi.registry_resource,
    replace(google_agent_registry_service.core_gapi.registry_resource, "projects/${var.project_number}/", "projects/${var.project_id}/"),
  ]
  # One UAP rule per condition: a rule's CEL expression is capped at 1024 characters.
  egress_rules = {
    registry = {
      description = "TriageRouter to the FaqResolver agent or the core-gapi endpoint (both name forms)"
      expression  = "(destination.agent_registry.agent.name in ['${join("', '", local.faq_agent_names)}']) || (destination.agent_registry.endpoint.name in ['${join("', '", local.core_gapi_endpoint_names)}'])"
    }
    trace = {
      description = "gRPC trace export (arrives unregistered)"
      expression  = "(destination.is_registered == false) && (destination.unregistered.host in ['cloudtrace.mtls.googleapis.com', 'cloudtrace.mtls.googleapis.com:443']) && (destination.unregistered.path == '/google.devtools.cloudtrace.v2.TraceService/BatchWriteSpans')"
    }
    faq_handoff = {
      # TriageRouter -> FaqResolver handoff is a REST call (POST /a2a/handoff), not the A2A
      # JSON-RPC URL from the agent card, so IAP sees it as unregistered: exact host+path+verb.
      description = "TriageRouter REST handoff to FaqResolver"
      expression  = "(destination.is_registered == false) && (destination.unregistered.host in ['${var.faq_resolver_host}', '${var.faq_resolver_host}:443']) && (destination.unregistered.path == '/a2a/handoff') && (destination.unregistered.method == 'POST')"
    }
  }
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
    dynamic "rules" {
      for_each = local.egress_rules
      content {
        description = rules.value.description
        effect      = "ALLOW"
        principals  = [var.triage_agent_principal]

        operation {
          permissions = ["iap.googleapis.com/resources.egressViaIAP"]
        }

        conditions {
          service    = "iap.googleapis.com"
          expression = rules.value.expression
        }
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

# REQ-AUTH-007: IAP data-access audit logs carry the per-request dry-run / enforced
# authorization verdict and the evaluated principal. The gateway's load-balancer log
# reports ALLOWED for every request while the authz extension is in DRY_RUN, so these
# logs are the only way to see what ENFORCE would deny before switching.
resource "google_project_iam_audit_config" "iap_data_access" {
  project = var.project_id
  service = "iap.googleapis.com"

  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
}
