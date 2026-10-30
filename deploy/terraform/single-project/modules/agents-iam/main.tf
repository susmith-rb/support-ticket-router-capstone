# deploy/terraform/single-project/modules/agents-iam/main.tf
# Service accounts and least-privilege IAM policies for agent runtime and microservices.
# Implements REQ-AGENT-016: Dedicated identities for TriageRouter and FaqResolver.
# Implements REQ-AUTH-001: Least-privilege custom roles for agent inference and runtime.

data "google_project" "project" {
  project_id = var.project_id
}

# -----------------------------------------------------------------------------
# 1. Dedicated Service Accounts
# -----------------------------------------------------------------------------

resource "google_service_account" "sa_triage" {
  account_id   = "sa-triage"
  display_name = "Triage Router Agent Service Account"
  description  = "Identity for TriageRouterAgent running on Agent Runtime (or Cloud Run fallback)"
  project      = var.project_id
}

resource "google_service_account" "sa_faq" {
  account_id   = "sa-faq"
  display_name = "FAQ Resolver Agent Service Account"
  description  = "Identity for FaqResolverAgent running on GKE Autopilot via Workload Identity"
  project      = var.project_id
}

# -----------------------------------------------------------------------------
# 2. GKE Workload Identity Binding for FaqResolverAgent
# -----------------------------------------------------------------------------

resource "google_service_account_iam_member" "faq_workload_identity" {
  service_account_id = google_service_account.sa_faq.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[faq-resolver/ksa-faq]"
}

# -----------------------------------------------------------------------------
# 3. Model Prediction User on router-s1 Vertex AI Endpoint (Resource-Level IAM)
# -----------------------------------------------------------------------------

resource "google_vertex_ai_endpoint_iam_member" "triage_prediction_user" {
  provider = google-beta
  count    = var.router_endpoint_id != null && var.router_endpoint_id != "" ? 1 : 0
  project  = var.project_id
  location = var.region
  endpoint = var.router_endpoint_id
  role     = "roles/aiplatform.predictionUser"
  member   = "serviceAccount:${google_service_account.sa_triage.email}"
}

resource "google_vertex_ai_endpoint_iam_member" "faq_prediction_user" {
  provider = google-beta
  count    = var.router_endpoint_id != null && var.router_endpoint_id != "" ? 1 : 0
  project  = var.project_id
  location = var.region
  endpoint = var.router_endpoint_id
  role     = "roles/aiplatform.predictionUser"
  member   = "serviceAccount:${google_service_account.sa_faq.email}"
}

resource "google_vertex_ai_endpoint_iam_member" "triage_dedicated_prediction_user" {
  provider = google-beta
  count    = var.dedicated_router_endpoint_id != "" ? 1 : 0
  project  = var.project_id
  location = var.region
  endpoint = var.dedicated_router_endpoint_id
  role     = "roles/aiplatform.predictionUser"
  member   = "serviceAccount:${google_service_account.sa_triage.email}"
}

resource "google_vertex_ai_endpoint_iam_member" "faq_dedicated_prediction_user" {
  provider = google-beta
  count    = var.dedicated_router_endpoint_id != "" ? 1 : 0
  project  = var.project_id
  location = var.region
  endpoint = var.dedicated_router_endpoint_id
  role     = "roles/aiplatform.predictionUser"
  member   = "serviceAccount:${google_service_account.sa_faq.email}"
}


# -----------------------------------------------------------------------------
# 4. Generative AI Inference & Runtime Custom Roles (REQ-AUTH-001)
# -----------------------------------------------------------------------------

# REQ-AUTH-001: Least-privilege custom role permissions for agent inference and runtime.
locals {
  agent_inference_permissions = [
    "aiplatform.endpoints.predict",
  ]
  triage_runtime_permissions = [
    "aiplatform.endpoints.predict",
    "aiplatform.sessions.create",
    "aiplatform.sessions.get",
    "aiplatform.sessions.list",
    "aiplatform.sessions.update",
    "aiplatform.sessions.delete",
    "aiplatform.sessions.run",
    "aiplatform.sessionEvents.append",
    "aiplatform.sessionEvents.list",
  ]
}

resource "google_project_iam_custom_role" "agent_inference" {
  project     = var.project_id
  role_id     = "ticketRouterAgentInference"
  title       = "Ticket Router Agent Inference"
  description = "Least-privilege role for FaqResolver agent inference via Vertex AI (REQ-AUTH-001)"
  permissions = local.agent_inference_permissions
}

resource "google_project_iam_custom_role" "triage_runtime" {
  project     = var.project_id
  role_id     = "ticketRouterTriageRuntime"
  title       = "Ticket Router Triage Runtime"
  description = "Least-privilege role for TriageRouter runtime sessions and inference via Vertex AI (REQ-AUTH-001)"
  permissions = local.triage_runtime_permissions
}

resource "google_project_iam_member" "triage_runtime_custom_role" {
  project = var.project_id
  role    = google_project_iam_custom_role.triage_runtime.id
  member  = "serviceAccount:${google_service_account.sa_triage.email}"
}

resource "google_project_iam_member" "faq_inference_custom_role" {
  project = var.project_id
  role    = google_project_iam_custom_role.agent_inference.id
  member  = "serviceAccount:${google_service_account.sa_faq.email}"
}

# -----------------------------------------------------------------------------
# 5. Agent Search (Discovery Engine) Viewer for FaqResolverAgent
# -----------------------------------------------------------------------------

resource "google_project_iam_member" "faq_discovery_engine_viewer" {
  project = var.project_id
  role    = "roles/discoveryengine.viewer"
  member  = "serviceAccount:${google_service_account.sa_faq.email}"
}

# -----------------------------------------------------------------------------
# 6. Pub/Sub Escalation Queue Permissions
# -----------------------------------------------------------------------------

resource "google_pubsub_topic_iam_member" "triage_human_queue_publisher" {
  count   = var.pubsub_human_queue_topic_name != "" ? 1 : 0
  topic   = var.pubsub_human_queue_topic_name
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.sa_triage.email}"
}

resource "google_pubsub_subscription_iam_member" "queue_human_queue_subscriber" {
  count        = var.pubsub_human_queue_sub_name != "" && var.sa_queue_email != "" ? 1 : 0
  subscription = var.pubsub_human_queue_sub_name
  project      = var.project_id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${var.sa_queue_email}"
}

# -----------------------------------------------------------------------------
# 7. Reasoning Engine Service Agent Permissions (Agent Runtime staging bucket)
# -----------------------------------------------------------------------------

resource "google_storage_bucket_iam_member" "re_staging_reader" {
  count  = var.enable_re_service_agent_bindings && var.corpus_bucket_name != "" ? 1 : 0
  bucket = var.corpus_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
}

# -----------------------------------------------------------------------------
# 8. Cloud Trace Agent Role Grants (REQ-AGENT-014)
# -----------------------------------------------------------------------------

resource "google_project_iam_member" "triage_cloudtrace_agent" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.sa_triage.email}"
}

resource "google_project_iam_member" "faq_cloudtrace_agent" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.sa_faq.email}"
}

resource "google_project_iam_member" "ticket_mcp_cloudtrace_agent" {
  count   = var.sa_ticket_mcp_email != "" ? 1 : 0
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${var.sa_ticket_mcp_email}"
}

resource "google_project_iam_member" "order_mcp_cloudtrace_agent" {
  count   = var.sa_order_mcp_email != "" ? 1 : 0
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${var.sa_order_mcp_email}"
}

resource "google_project_iam_member" "queue_cloudtrace_agent" {
  count   = var.sa_queue_email != "" ? 1 : 0
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${var.sa_queue_email}"
}

resource "google_project_iam_member" "re_service_agent_cloudtrace_agent" {
  count   = var.enable_re_service_agent_bindings ? 1 : 0
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
}
