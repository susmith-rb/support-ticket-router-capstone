# deploy/terraform/single-project/modules/pubsub/main.tf
# Pub/Sub escalation topic and dead-letter queue for human queue agent.
# Implements REQ-AGENT-018: Asynchronous decoupling with DLQ and backpressure.
# Implements REQ-AGENT-031: Push subscription to human-queue endpoint with OIDC auth.

data "google_project" "project" {
  project_id = var.project_id
}

# REQ-AGENT-031: Dedicated service account for Pub/Sub push subscription OIDC auth
resource "google_service_account" "sa_queue_push" {
  account_id   = "sa-queue-push"
  display_name = "Pub/Sub Human Queue Push Subscription Service Account"
  description  = "Identity used by Pub/Sub to sign OIDC tokens for human-queue push endpoint (REQ-AGENT-031)"
  project      = var.project_id
}

# Grant Pub/Sub service agent permission to create OIDC tokens for sa-queue-push
resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.sa_queue_push.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# Pub/Sub service agent requires publisher permissions on the dead-letter topic
resource "google_pubsub_topic_iam_member" "pubsub_dlq_publisher" {
  topic   = google_pubsub_topic.human_queue_dlq.name
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# Pub/Sub service agent requires subscriber permissions on the forwarding subscription
resource "google_pubsub_subscription_iam_member" "pubsub_sub_subscriber" {
  subscription = google_pubsub_subscription.human_queue_sub.name
  project      = var.project_id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic" "human_queue" {
  name    = var.topic_name
  project = var.project_id
  labels  = var.labels
}

resource "google_pubsub_topic" "human_queue_dlq" {
  name    = "${var.topic_name}-dlq"
  project = var.project_id
  labels  = var.labels
}

resource "google_pubsub_subscription" "human_queue_sub" {
  name    = "${var.topic_name}-sub"
  topic   = google_pubsub_topic.human_queue.id
  project = var.project_id

  ack_deadline_seconds       = 60
  message_retention_duration = "604800s" # 7 days
  retain_acked_messages      = false

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.human_queue_dlq.id
    max_delivery_attempts = 5
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dynamic "push_config" {
    for_each = var.push_endpoint != "" ? [var.push_endpoint] : []
    content {
      push_endpoint = push_config.value

      oidc_token {
        service_account_email = google_service_account.sa_queue_push.email
        audience              = push_config.value
      }
    }
  }

  labels = var.labels
}

