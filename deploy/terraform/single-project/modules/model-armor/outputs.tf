output "template_id" {
  description = "ID of the Model Armor template"
  value       = google_model_armor_template.support_router_lean.template_id
}

output "template_name" {
  description = "Full resource name of the Model Armor template"
  value       = google_model_armor_template.support_router_lean.name
}

output "inspect_template_id" {
  description = "Resource name (id) of the Cloud DLP inspect template"
  value       = google_data_loss_prevention_inspect_template.support_router_inspect.id
}
