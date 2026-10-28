output "faq_articles_data_store_id" {
  description = "Data store ID for faq-articles"
  value       = module.agent_search.faq_articles_data_store_id
}

output "resolved_threads_data_store_id" {
  description = "Data store ID for resolved-threads"
  value       = module.agent_search.resolved_threads_data_store_id
}

output "search_engine_id" {
  description = "Engine ID for support-kb-search"
  value       = module.agent_search.search_engine_id
}

output "search_location" {
  description = "Location for Agent Search resources"
  value       = module.agent_search.search_location
}

output "search_tier" {
  description = "Search tier for Agent Search"
  value       = module.agent_search.search_tier
}

output "discovery_engine_service_account" {
  description = "Email of the Discovery Engine service agent"
  value       = google_project_service_identity.discovery_engine.email
}

output "labels" {
  description = "Configured resource labels"
  value       = var.labels
}

output "cloudbuild_service_account" {
  description = "Email of the Cloud Build service account"
  value       = module.cloudbuild.cloudbuild_service_account
}

output "scheduler_service_account" {
  description = "Email of the Cloud Scheduler service account"
  value       = module.cloudbuild.scheduler_service_account
}

output "nightly_trigger_id" {
  description = "ID of the nightly Cloud Build trigger"
  value       = module.cloudbuild.nightly_trigger_id
}

output "scheduler_job_name" {
  description = "Name of the Cloud Scheduler job"
  value       = module.cloudbuild.scheduler_job_name
}

output "router_endpoint_id" {
  description = "Resource ID of the router-s1 Vertex AI endpoint"
  value       = module.router_endpoint.endpoint_id
}

output "router_endpoint_name" {
  description = "Display name of the router-s1 Vertex AI endpoint"
  value       = module.router_endpoint.endpoint_name
}

output "router_serving_service_account" {
  description = "Email of the model serving service account"
  value       = module.router_endpoint.service_account_email
}

output "serving_artifact_registry_repo" {
  description = "Repository URL for serving container images in Artifact Registry"
  value       = module.router_endpoint.artifact_registry_repository_url
}

output "cloud_sql_instance_name" {
  description = "Cloud SQL instance identifier"
  value       = module.cloud_sql.instance_name
}

output "cloud_sql_connection_name" {
  description = "Cloud SQL connection name"
  value       = module.cloud_sql.instance_connection_name
}

output "cloud_sql_database_name" {
  description = "PostgreSQL support database name"
  value       = module.cloud_sql.database_name
}

output "cloud_sql_sa_ticket_mcp_email" {
  description = "Email of the sa-ticket-mcp service account"
  value       = module.cloud_sql.sa_ticket_mcp_email
}

output "cloud_sql_sa_order_mcp_email" {
  description = "Email of the sa-order-mcp service account"
  value       = module.cloud_sql.sa_order_mcp_email
}

output "cloud_sql_sa_queue_email" {
  description = "Email of the sa-queue service account"
  value       = module.cloud_sql.sa_queue_email
}

output "cloud_sql_admin_secret_id" {
  description = "Secret Manager secret ID for break-glass admin password"
  value       = module.cloud_sql.admin_secret_id
}

# -----------------------------------------------------------------------------
# Agent Tier Outputs
# -----------------------------------------------------------------------------

output "pubsub_human_queue_topic" {
  description = "Name of the human-queue Pub/Sub escalation topic"
  value       = module.pubsub.topic_name
}

output "pubsub_human_queue_dlq_topic" {
  description = "Name of the human-queue dead-letter Pub/Sub topic"
  value       = module.pubsub.dlq_topic_name
}

output "pubsub_human_queue_subscription" {
  description = "Name of the human-queue pull subscription"
  value       = module.pubsub.subscription_name
}

output "gke_cluster_name" {
  description = "Name of the GKE Autopilot cluster for agent workloads"
  value       = module.gke_autopilot.cluster_name
}

output "gke_cluster_id" {
  description = "Resource ID of the GKE Autopilot cluster"
  value       = module.gke_autopilot.cluster_id
}

output "gke_cluster_endpoint" {
  description = "Public control-plane endpoint for the GKE Autopilot cluster"
  value       = module.gke_autopilot.endpoint
}

output "sa_triage_email" {
  description = "Email of the TriageRouter agent service account"
  value       = module.agents_iam.sa_triage_email
}

output "sa_faq_email" {
  description = "Email of the FaqResolver agent service account"
  value       = module.agents_iam.sa_faq_email
}

output "cloud_run_ticket_db_mcp_url" {
  description = "Service URL for ticket-db-mcp Cloud Run service"
  value       = module.cloud_run_services.ticket_db_mcp_url
}

output "cloud_run_order_lookup_mcp_url" {
  description = "Service URL for order-lookup-mcp Cloud Run service"
  value       = module.cloud_run_services.order_lookup_mcp_url
}

output "cloud_run_human_queue_url" {
  description = "Service URL for human-queue Cloud Run service"
  value       = module.cloud_run_services.human_queue_url
}

output "vpc_network_name" {
  description = "Name of the VPC network"
  value       = module.network.network_name
}

output "vpc_subnetwork_name" {
  description = "Name of the VPC subnetwork"
  value       = module.network.subnetwork_name
}

output "faq_resolver_ip" {
  description = "Global static external IP address for FaqResolver GKE Ingress"
  value       = module.network.faq_resolver_ip
}

output "faq_resolver_ip_name" {
  description = "Resource name of the global static IP for FaqResolver GKE Ingress"
  value       = module.network.faq_resolver_ip_name
}

output "faq_resolver_url" {
  description = "Public HTTPS URL for FaqResolver on GKE Autopilot via sslip.io"
  value       = "https://faq.${replace(module.network.faq_resolver_ip, ".", "-")}.sslip.io"
}

output "faq_resolver_audience" {
  description = "Target audience URI for Google ID token authentication on FaqResolver"
  value       = "https://faq.${replace(module.network.faq_resolver_ip, ".", "-")}.sslip.io"
}

output "router_s1_dedicated_endpoint_id" {
  description = "Resource ID of the dedicated router-s1 Vertex AI Endpoint"
  value       = module.router_endpoint.dedicated_endpoint_id
}

output "router_s1_dedicated_base_url" {
  description = "Base URL of the dedicated router-s1 Vertex AI Endpoint"
  value       = "https://${module.router_endpoint.dedicated_endpoint_dns}"
}

output "identity_web_api_key_secret_id" {
  description = "Secret Manager secret ID for Identity Platform Web API key"
  value       = module.identity.identity_web_api_key_secret_id
}

output "model_armor_template_id" {
  description = "ID of the Model Armor template"
  value       = module.model_armor.template_id
}

output "model_armor_template_name" {
  description = "Full resource name of the Model Armor template"
  value       = module.model_armor.template_name
}






