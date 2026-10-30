#!/usr/bin/env bash
# deploy/terraform/agent-gateway/bind_uap.sh
# REQ-AUTH-007: bind the Terraform-managed access policy `tr-egress-uap` to the project.
#
# Why a script: google/google-beta 7.46 expose `google_iam_projects_policy_binding.target`
# with `principal_set` only; ACCESS-kind bindings need `target.resource`, which the provider
# does not support yet ("Policy Binding target must be a resource for Access Policy").
# Replace this with the Terraform resource once the provider adds `target.resource`.
#
# Usage: deploy/terraform/agent-gateway/bind_uap.sh [create|describe|delete]
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-susmith-fde-ticket-router}"
POLICY_ID="${POLICY_ID:-tr-egress-uap}"
BINDING_ID="${BINDING_ID:-tr-egress-uap-binding}"
BASE="https://iam.googleapis.com/v3beta/projects/${PROJECT_ID}/locations/global/policyBindings"
TOKEN="$(CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false gcloud auth print-access-token)"
H=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" -H "x-goog-user-project: ${PROJECT_ID}")

case "${1:-create}" in
  create)
    curl -sS -X POST "${BASE}?policyBindingId=${BINDING_ID}" "${H[@]}" -d @- << EOF
{
  "policyKind": "ACCESS",
  "policy": "projects/${PROJECT_ID}/locations/global/accessPolicies/${POLICY_ID}",
  "target": {"resource": "//cloudresourcemanager.googleapis.com/projects/${PROJECT_ID}"}
}
EOF
    ;;
  describe)
    curl -sS "${BASE}/${BINDING_ID}" "${H[@]}"
    ;;
  delete)
    curl -sS -X DELETE "${BASE}/${BINDING_ID}" "${H[@]}"
    ;;
  *)
    echo "usage: $0 [create|describe|delete]" >&2
    exit 2
    ;;
esac
echo
