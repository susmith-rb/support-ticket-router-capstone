output "instance_name" {
  description = "Cloud SQL instance identifier"
  value       = google_sql_database_instance.ticket_store.name
}

output "instance_connection_name" {
  description = "Cloud SQL instance connection name (<project>:<region>:<instance>)"
  value       = google_sql_database_instance.ticket_store.connection_name
}

output "instance_first_ip_address" {
  description = "First IPv4 address assigned to the Cloud SQL instance"
  value       = google_sql_database_instance.ticket_store.first_ip_address
}

output "database_name" {
  description = "PostgreSQL support database name"
  value       = google_sql_database.support.name
}

output "sa_ticket_mcp_email" {
  description = "Email of the sa-ticket-mcp service account"
  value       = google_service_account.sa_ticket_mcp.email
}

output "sa_order_mcp_email" {
  description = "Email of the sa-order-mcp service account"
  value       = google_service_account.sa_order_mcp.email
}

output "sa_queue_email" {
  description = "Email of the sa-queue service account"
  value       = google_service_account.sa_queue.email
}

output "admin_secret_id" {
  description = "Secret Manager secret ID containing break-glass admin password"
  value       = google_secret_manager_secret.admin_password.secret_id
}

output "admin_secret_version" {
  description = "Resource name of the break-glass admin password secret version"
  value       = google_secret_manager_secret_version.admin_password.name
}
