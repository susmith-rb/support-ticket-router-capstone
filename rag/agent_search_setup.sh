#!/usr/bin/env bash
# agent_search_setup.sh — create Agent Search (Discovery Engine) data stores + search app, import corpora.
#   bash agent_search_setup.sh create|import-threads|import-faq|engine|status
set -uo pipefail
export CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false
P=susmith-fde-ticket-router; PN=620485621180; BUCKET=gs://susmith-fde-ticket-router-laya
API=https://discoveryengine.googleapis.com/v1/projects/$P/locations/global/collections/default_collection
TOK=$(gcloud auth print-access-token)
call() { curl -s -X "$1" -H "Authorization: Bearer $TOK" -H "x-goog-user-project: $P" -H "Content-Type: application/json" "$2" ${3:+-d "$3"}; echo; }
case "${1:-}" in
  create)
    gcloud storage buckets add-iam-policy-binding $BUCKET --project $P \
      --member="serviceAccount:service-$PN@gcp-sa-discoveryengine.iam.gserviceaccount.com" --role=roles/storage.objectViewer >/dev/null 2>&1 \
      && echo "granted Discovery Engine SA read on bucket" || echo "SA grant failed (service agent may not exist yet; retry after first API call)"
    call POST "$API/dataStores?dataStoreId=faq-articles" \
      '{"displayName":"faq-articles","industryVertical":"GENERIC","solutionTypes":["SOLUTION_TYPE_SEARCH"],"contentConfig":"CONTENT_REQUIRED"}'
    call POST "$API/dataStores?dataStoreId=resolved-threads" \
      '{"displayName":"resolved-threads","industryVertical":"GENERIC","solutionTypes":["SOLUTION_TYPE_SEARCH"],"contentConfig":"NO_CONTENT"}' ;;
  import-threads)
    call POST "$API/dataStores/resolved-threads/branches/default_branch/documents:import" \
      "{\"gcsSource\":{\"inputUris\":[\"$BUCKET/corpus/resolved-threads/v1/resolved_threads_import.jsonl\"],\"dataSchema\":\"document\"},\"reconciliationMode\":\"FULL\"}" ;;
  import-faq)
    call POST "$API/dataStores/faq-articles/branches/default_branch/documents:import" \
      "{\"gcsSource\":{\"inputUris\":[\"$BUCKET/corpus/faq-articles/v1/faq_articles_import.jsonl\"],\"dataSchema\":\"document\"},\"reconciliationMode\":\"FULL\"}" ;;
  engine)
    call POST "$API/engines?engineId=support-kb-search" \
      '{"displayName":"Support KB search","solutionType":"SOLUTION_TYPE_SEARCH","industryVertical":"GENERIC","dataStoreIds":["faq-articles","resolved-threads"],"searchEngineConfig":{"searchTier":"SEARCH_TIER_ENTERPRISE","searchAddOns":["SEARCH_ADD_ON_LLM"]}}' ;;
  status)
    for ds in faq-articles resolved-threads; do
      echo "== $ds"; call GET "$API/dataStores/$ds/branches/default_branch/documents?pageSize=1" | head -c 300; echo
    done
    call GET "$API/engines/support-kb-search" | head -c 400 ;;
  op) call GET "https://discoveryengine.googleapis.com/v1/$2" | head -c 1500 ;;
  *) echo "usage: $0 create|import-threads|import-faq|engine|status|op <name>" ;;
esac
