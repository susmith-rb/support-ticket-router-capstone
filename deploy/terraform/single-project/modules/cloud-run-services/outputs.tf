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
