# deploy/terraform/single-project/modules/pubsub/outputs.tf

output "human_queue_topic_name" {
  description = "Name of the human queue Pub/Sub topic"
  value       = google_pubsub_topic.human_queue.name
}

output "topic_name" {
  description = "Alias for human_queue_topic_name"
  value       = google_pubsub_topic.human_queue.name
}

output "human_queue_topic_id" {
  description = "ID of the human queue Pub/Sub topic"
  value       = google_pubsub_topic.human_queue.id
}

output "topic_id" {
  description = "Alias for human_queue_topic_id"
  value       = google_pubsub_topic.human_queue.id
}

output "human_queue_dlq_topic_name" {
  description = "Name of the dead-letter queue Pub/Sub topic"
  value       = google_pubsub_topic.human_queue_dlq.name
}

output "dlq_topic_name" {
  description = "Alias for human_queue_dlq_topic_name"
  value       = google_pubsub_topic.human_queue_dlq.name
}

output "human_queue_sub_name" {
  description = "Name of the human queue Pub/Sub pull subscription"
  value       = google_pubsub_subscription.human_queue_sub.name
}

output "subscription_name" {
  description = "Alias for human_queue_sub_name"
  value       = google_pubsub_subscription.human_queue_sub.name
}

output "human_queue_sub_id" {
  description = "ID of the human queue Pub/Sub pull subscription"
  value       = google_pubsub_subscription.human_queue_sub.id
}

output "subscription_id" {
  description = "Alias for human_queue_sub_id"
  value       = google_pubsub_subscription.human_queue_sub.id
}

output "sa_queue_push_email" {
  description = "Email of the dedicated Pub/Sub push service account"
  value       = google_service_account.sa_queue_push.email
}

