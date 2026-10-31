# deploy/terraform/single-project/modules/cloud-run-services/main.tf
# Cloud Run v2 services for MCP servers and HumanQueueAgent.
# Implements REQ-AGENT-017: Serverless container deployment with IAM-only authentication.

locals {
  service_count = var.agents_image != "" ? 1 : 0
}

# -----------------------------------------------------------------------------
# 1. Ticket DB MCP Server
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "ticket_db_mcp" {
  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version, scaling]
  }

  count    = local.service_count
  name     = "ticket-db-mcp"
  location = var.region
  project  = var.project_id

  # Ingress ALL allows Agent Runtime and GKE callers to invoke service over HTTPS.
  # Security invariant: No allUsers invoker grant is created; calls require Google IAM OIDC auth.
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = var.sa_ticket_mcp_email

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    containers {
      image = var.agents_image

      env {
        name  = "SERVICE"
        value = "ticket_db"
      }
      env {
        name  = "INSTANCE_CONNECTION_NAME"
        value = var.cloud_sql_connection_name
      }
      env {
        name  = "DB_NAME"
        value = "support"
      }
      env {
        name  = "DB_IAM_USER"
        value = trimsuffix(var.sa_ticket_mcp_email, ".gserviceaccount.com")
      }
      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "REGION"
        value = var.region
      }
      env {
        name  = "ENABLE_CLOUD_TRACE"
        value = "1"
      }
      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "NO_CONTENT"
      }

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }
  }

  labels = var.labels
}

# Invokers for ticket-db-mcp: sa-triage, sa-faq, sa-queue
locals {
  ticket_db_invokers = distinct(compact([
    var.sa_triage_email,
    var.sa_faq_email,
    var.sa_queue_email,
  ]))
}

resource "google_cloud_run_v2_service_iam_member" "ticket_db_invoker" {
  for_each = local.service_count > 0 ? toset(local.ticket_db_invokers) : toset([])
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.ticket_db_mcp[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value}"
}

resource "google_cloud_run_v2_service_iam_member" "ticket_db_invoker_triage_agent" {
  count    = local.service_count > 0 && var.triage_agent_principal != "" ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.ticket_db_mcp[0].name
  role     = "roles/run.invoker"
  member   = var.triage_agent_principal
}

# REQ-AUTH-008: Invoker for ticket-db-mcp: FaqResolver GKE Agent Identity SPIFFE principal
# FaqResolver's HumanHandoff persists tickets via ticket-db-mcp.
resource "google_cloud_run_v2_service_iam_member" "ticket_db_invoker_faq_agent" {
  count    = local.service_count > 0 && var.faq_agent_principal != "" ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.ticket_db_mcp[0].name
  role     = "roles/run.invoker"
  member   = var.faq_agent_principal
}

# -----------------------------------------------------------------------------
# 2. Order Lookup MCP Server
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "order_lookup_mcp" {
  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version, scaling]
  }

  count    = local.service_count
  name     = "order-lookup-mcp"
  location = var.region
  project  = var.project_id

  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = var.sa_order_mcp_email

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    containers {
      image = var.agents_image

      env {
        name  = "SERVICE"
        value = "order_lookup"
      }
      env {
        name  = "INSTANCE_CONNECTION_NAME"
        value = var.cloud_sql_connection_name
      }
      env {
        name  = "DB_NAME"
        value = "support"
      }
      env {
        name  = "DB_IAM_USER"
        value = trimsuffix(var.sa_order_mcp_email, ".gserviceaccount.com")
      }
      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "REGION"
        value = var.region
      }
      env {
        name  = "ENABLE_CLOUD_TRACE"
        value = "1"
      }
      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "NO_CONTENT"
      }
      env {
        name  = "CCT_MODE"
        value = var.cct_mode
      }
      env {
        name  = "CCT_AUDIENCE"
        value = "order-lookup-mcp"
      }
      env {
        name  = "CCT_ALLOWED_ISSUERS"
        value = "triage-router"
      }
      env {
        name  = "CCT_KEY_PREFIX"
        value = var.cct_key_prefix
      }
      env {
        name  = "ALLOWED_CALLER_SPIFFE_IDS"
        value = var.allowed_caller_spiffe_ids
      }

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }
  }

  labels = var.labels
}

# TODO(REQ-AUTH-008): remove after live e2e
# Invoker for order-lookup-mcp: sa-faq
resource "google_cloud_run_v2_service_iam_member" "order_lookup_invoker" {
  count    = local.service_count > 0 && var.sa_faq_email != "" ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.order_lookup_mcp[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.sa_faq_email}"
}

# REQ-AUTH-008 / REQ-AUTH-009: Invoker for order-lookup-mcp: FaqResolver GKE Agent Identity SPIFFE principal
resource "google_cloud_run_v2_service_iam_member" "order_lookup_invoker_faq_agent" {
  count    = local.service_count > 0 && var.faq_agent_principal != "" ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.order_lookup_mcp[0].name
  role     = "roles/run.invoker"
  member   = var.faq_agent_principal
}

# -----------------------------------------------------------------------------
# 3. Human Queue Agent Service
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "human_queue" {
  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version, scaling]
  }

  count    = local.service_count
  name     = "human-queue"
  location = var.region
  project  = var.project_id

  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = var.sa_queue_email

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    containers {
      image = var.agents_image

      env {
        name  = "SERVICE"
        value = "human_queue"
      }
      env {
        name  = "INSTANCE_CONNECTION_NAME"
        value = var.cloud_sql_connection_name
      }
      env {
        name  = "DB_NAME"
        value = "support"
      }
      env {
        name  = "DB_IAM_USER"
        value = trimsuffix(var.sa_queue_email, ".gserviceaccount.com")
      }
      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "REGION"
        value = var.region
      }
      env {
        name  = "ENABLE_CLOUD_TRACE"
        value = "1"
      }
      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "NO_CONTENT"
      }
      env {
        name  = "PUSH_AUDIENCE"
        value = var.push_audience
      }
      env {
        name  = "PUSH_INVOKER_EMAIL"
        value = var.sa_queue_push_email
      }
      env {
        name  = "REDACTION_KMS_KEY"
        value = var.redaction_kms_key
      }
      env {
        name  = "PURGE_AUDIENCE"
        value = var.purge_audience
      }
      env {
        name  = "PURGE_INVOKER_EMAIL"
        value = var.purge_invoker_email
      }
      env {
        name  = "REVIEW_UI_CALLER_EMAILS"
        value = google_service_account.sa_review_ui.email
      }

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }
  }

  labels = var.labels
}

# Invoker for human-queue: sa-triage (for direct A2A handoff fallback)
resource "google_cloud_run_v2_service_iam_member" "human_queue_invoker" {
  count    = local.service_count > 0 && var.sa_triage_email != "" ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.human_queue[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.sa_triage_email}"
}

resource "google_cloud_run_v2_service_iam_member" "human_queue_invoker_triage_agent" {
  count    = local.service_count > 0 && var.triage_agent_principal != "" ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.human_queue[0].name
  role     = "roles/run.invoker"
  member   = var.triage_agent_principal
}

# Invoker for human-queue: sa-queue-push (for Pub/Sub push subscription delivery REQ-AGENT-031)
resource "google_cloud_run_v2_service_iam_member" "human_queue_push_invoker" {
  count    = local.service_count > 0 && var.sa_queue_push_email != "" ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.human_queue[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.sa_queue_push_email}"
}

# -----------------------------------------------------------------------------
# 4. Human Review UI Service (IAP-protected web interface)
# -----------------------------------------------------------------------------

resource "google_service_account" "sa_review_ui" {
  account_id   = "sa-review-ui"
  display_name = "Human Review UI Service Account"
  project      = var.project_id
}

# Invoker for human-queue: sa-review-ui ONLY (REQ-AUTH-010)
resource "google_cloud_run_v2_service_iam_member" "human_queue_invoker_review_ui" {
  count    = local.service_count
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.human_queue[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.sa_review_ui.email}"
}

data "google_project" "project" {
  project_id = var.project_id
}

resource "google_cloud_run_v2_service" "human_review_ui" {
  provider = google-beta

  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version, scaling]
  }

  count        = local.service_count
  name         = "human-review-ui"
  location     = var.region
  project      = var.project_id
  ingress      = "INGRESS_TRAFFIC_ALL"
  launch_stage = "BETA"
  iap_enabled  = true

  template {
    service_account = google_service_account.sa_review_ui.email

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    containers {
      image = var.agents_image

      env {
        name  = "SERVICE"
        value = "review_ui"
      }
      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "REGION"
        value = var.region
      }
      env {
        name  = "HUMAN_QUEUE_URL"
        value = length(google_cloud_run_v2_service.human_queue) > 0 ? google_cloud_run_v2_service.human_queue[0].uri : var.human_queue_url
      }
      env {
        name  = "IAP_EXPECTED_AUDIENCE"
        value = "/projects/${data.google_project.project.number}/locations/${var.region}/services/human-review-ui"
      }
      env {
        name  = "ENABLE_CLOUD_TRACE"
        value = "1"
      }
      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "NO_CONTENT"
      }

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }
  }

  labels = var.labels
}

# Invoker for human-review-ui: IAP service agent (required by Cloud Run direct IAP)
resource "google_cloud_run_v2_service_iam_member" "human_review_ui_iap_invoker" {
  count    = local.service_count
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.human_review_ui[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-iap.iam.gserviceaccount.com"
}

# Reps authorized to access review UI through IAP
resource "google_iap_web_cloud_run_service_iam_member" "review_ui_reps" {
  provider               = google-beta
  for_each               = local.service_count > 0 ? toset(var.review_ui_reps) : toset([])
  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.human_review_ui[0].name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}

