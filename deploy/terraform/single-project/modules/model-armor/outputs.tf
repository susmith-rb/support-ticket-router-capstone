output "template_id" {
  description = "ID of the Model Armor template"
  value       = google_model_armor_template.support_router_lean.template_id
}

output "template_name" {
  description = "Full resource name of the Model Armor template"
  value       = google_model_armor_template.support_router_lean.name
}
