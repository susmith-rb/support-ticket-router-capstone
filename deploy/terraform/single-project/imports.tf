# Import blocks for existing global Agent Search resources into Terraform state (TF >= 1.5)

import {
  to = module.agent_search.google_discovery_engine_data_store.faq_articles
  id = "projects/susmith-fde-ticket-router/locations/global/collections/default_collection/dataStores/faq-articles"
}

import {
  to = module.agent_search.google_discovery_engine_data_store.resolved_threads
  id = "projects/susmith-fde-ticket-router/locations/global/collections/default_collection/dataStores/resolved-threads"
}

import {
  to = module.agent_search.google_discovery_engine_search_engine.support_kb_search
  id = "projects/susmith-fde-ticket-router/locations/global/collections/default_collection/engines/support-kb-search"
}

# Import block for existing 2nd-gen Cloud Build GitHub connection
import {
  to = module.cloudbuild.google_cloudbuildv2_connection.github[0]
  id = "projects/susmith-fde-ticket-router/locations/us-central1/connections/github-connection"
}

# Import block for existing dedicated router-s1 Vertex AI endpoint
import {
  to = module.router_endpoint.google_vertex_ai_endpoint.router_s1_dedicated
  id = "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1-dedicated"
}

# Import block for existing enabled Model Armor API service
import {
  to = module.project_services.google_project_service.services["modelarmor.googleapis.com"]
  id = "susmith-fde-ticket-router/modelarmor.googleapis.com"
}


