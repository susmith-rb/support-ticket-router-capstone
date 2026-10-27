# deploy/terraform/single-project/modules/pubsub/main.tf
# Pub/Sub escalation topic and dead-letter queue for human queue agent.
# Implements REQ-AGENT-018: Asynchronous decoupling with DLQ and backpressure.

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

  labels = var.labels
}
