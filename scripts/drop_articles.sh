#!/usr/bin/env bash
# drop_articles.sh — remove non-answerable articles from the import manifest and the data store
set -uo pipefail
export CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false
P=susmith-fde-ticket-router; W="$1"; shift
TOK=$(gcloud auth application-default print-access-token)
API=https://discoveryengine.googleapis.com/v1/projects/$P/locations/global/collections/default_collection/dataStores/faq-articles/branches/default_branch/documents
for id in "$@"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $TOK" -H "x-goog-user-project: $P" "$API/$id")
  echo "delete $id -> $code"
  grep -v "\"id\": \"$id\"" "$W/faq_articles_import.jsonl" > "$W/tmp.jsonl" && mv "$W/tmp.jsonl" "$W/faq_articles_import.jsonl"
  mkdir -p "$W/dropped" && mv "$W/docs/$id.txt" "$W/dropped/" 2>/dev/null
done
wc -l "$W/faq_articles_import.jsonl"
gcloud storage cp "$W/faq_articles_import.jsonl" gs://susmith-fde-ticket-router-laya/corpus/faq-articles/v1/ --project $P 2>&1 | tail -1
for id in "$@"; do gcloud storage rm "gs://susmith-fde-ticket-router-laya/corpus/faq-articles/v1/docs/$id.txt" --project $P 2>&1 | tail -1; done
