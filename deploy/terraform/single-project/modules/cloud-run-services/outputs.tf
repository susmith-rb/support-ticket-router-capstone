output "ticket_db_mcp_url" {
  description = "Service URI for ticket-db-mcp"
  value       = length(google_cloud_run_v2_service.ticket_db_mcp) > 0 ? google_cloud_run_v2_service.ticket_db_mcp[0].uri : ""
}

output "order_lookup_mcp_url" {
  description = "Service URI for order-lookup-mcp"
  value       = length(google_cloud_run_v2_service.order_lookup_mcp) > 0 ? google_cloud_run_v2_service.order_lookup_mcp[0].uri : ""
}

output "human_queue_url" {
  description = "Service URI for human-queue"
  value       = length(google_cloud_run_v2_service.human_queue) > 0 ? google_cloud_run_v2_service.human_queue[0].uri : ""
}

output "human_review_ui_url" {
  description = "Service URI for human-review-ui"
  value       = length(google_cloud_run_v2_service.human_review_ui) > 0 ? google_cloud_run_v2_service.human_review_ui[0].uri : ""
}

output "sa_review_ui_email" {
  description = "Email of the service account running human-review-ui"
  value       = google_service_account.sa_review_ui.email
}

output "triage_fallback_url" {
  description = "Service URI for triage-fallback"
  value       = length(google_cloud_run_v2_service.triage_fallback) > 0 ? google_cloud_run_v2_service.triage_fallback[0].uri : ""
}

