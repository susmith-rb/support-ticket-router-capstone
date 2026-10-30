output "gateway_name" {
  description = "Resource name of the Agent Gateway"
  value       = google_network_services_agent_gateway.tr_egress.name
}

output "gateway_id" {
  description = "Resource ID of the Agent Gateway"
  value       = google_network_services_agent_gateway.tr_egress.id
}

output "faq_resolver_registry_service" {
  description = "Resource ID of the FaqResolver Agent Registry entry"
  value       = google_agent_registry_service.faq_resolver.id
}

output "core_gapi_registry_service" {
  description = "Resource ID of the composite core-gapi Agent Registry entry"
  value       = google_agent_registry_service.core_gapi.id
}

output "authz_extension_id" {
  description = "Resource ID of the IAP authz extension"
  value       = google_network_services_authz_extension.iap_authz.id
}

output "authz_policy_id" {
  description = "Resource ID of the Network Security authz policy"
  value       = google_network_security_authz_policy.tr_authz_policy.id
}
