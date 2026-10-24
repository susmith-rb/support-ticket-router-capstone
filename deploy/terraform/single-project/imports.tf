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
