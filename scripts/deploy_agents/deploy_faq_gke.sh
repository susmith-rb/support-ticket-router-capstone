#!/usr/bin/env bash
# scripts/deploy_agents/deploy_faq_gke.sh - Deploy FaqResolverAgent to GKE Autopilot.
# Implements REQ-AGENT-018: GKE Autopilot deployment for FaqResolverAgent.
#
# Connects to the support-agents GKE Autopilot cluster and applies Kubernetes manifests:
# - Namespace: faq-resolver
# - KSA: ksa-faq (Workload Identity annotation)
# - Deployment: faq-resolver
# - Service: faq-resolver
# - HPA: faq-resolver-hpa (min 1, max 10, 60% CPU)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
K8S_DIR="${REPO_ROOT}/deploy/k8s/faq-resolver"

PROJECT_ID="${PROJECT_ID:-susmith-fde-ticket-router}"
REGION="${REGION:-us-central1}"
CLUSTER_NAME="${CLUSTER_NAME:-support-agents}"
IMAGE_URI="${IMAGE_URI:-}"
DRY_RUN=false
WAIT=false

usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Deploy FaqResolverAgent Kubernetes manifests to GKE Autopilot.

Options:
  --project PROJECT_ID     GCP Project ID (default: ${PROJECT_ID})
  --region REGION          GCP Region for GKE cluster (default: ${REGION})
  --cluster CLUSTER        GKE Cluster Name (default: ${CLUSTER_NAME})
  --image IMAGE_URI        Docker image URI to deploy
  --dry-run                Validate and perform client-side dry-run apply
  --wait                   Wait for deployment rollout to complete
  --help                   Display this help message
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)
            PROJECT_ID="$2"
            shift 2
            ;;
        --region)
            REGION="$2"
            shift 2
            ;;
        --cluster)
            CLUSTER_NAME="$2"
            shift 2
            ;;
        --image)
            IMAGE_URI="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --wait)
            WAIT=true
            shift
            ;;
        --help|-h)
            usage
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            ;;
    esac
done

echo "================================================================="
echo "GKE Autopilot Deployer: FaqResolverAgent"
echo "Project:    ${PROJECT_ID}"
echo "Region:     ${REGION}"
echo "Cluster:    ${CLUSTER_NAME}"
echo "Manifests:  ${K8S_DIR}"
echo "Dry Run:    ${DRY_RUN}"
echo "================================================================="

if [[ ! -d "${K8S_DIR}" ]]; then
    echo "Error: Manifest directory not found: ${K8S_DIR}" >&2
    exit 1
fi

APPLY_FLAGS=()
if [[ "${DRY_RUN}" == "true" ]]; then
    APPLY_FLAGS+=(--dry-run=client)
    echo "Running in dry-run mode (client-side validation only)."
else
    echo "Acquiring GKE cluster credentials..."
    gcloud container clusters get-credentials "${CLUSTER_NAME}" \
        --region "${REGION}" \
        --project "${PROJECT_ID}"
fi

echo "Applying Kubernetes manifests..."
kubectl apply "${APPLY_FLAGS[@]}" -f "${K8S_DIR}/namespace.yaml"
kubectl apply "${APPLY_FLAGS[@]}" -f "${K8S_DIR}/ksa.yaml"

if [[ -n "${IMAGE_URI}" ]]; then
    echo "Rendering deployment with image: ${IMAGE_URI}"
    sed "s|image: .*|image: ${IMAGE_URI}|" "${K8S_DIR}/deployment.yaml" | kubectl apply "${APPLY_FLAGS[@]}" -f -
else
    kubectl apply "${APPLY_FLAGS[@]}" -f "${K8S_DIR}/deployment.yaml"
fi

kubectl apply "${APPLY_FLAGS[@]}" -f "${K8S_DIR}/service.yaml"
kubectl apply "${APPLY_FLAGS[@]}" -f "${K8S_DIR}/hpa.yaml"

if [[ "${DRY_RUN}" == "false" && "${WAIT}" == "true" ]]; then
    echo "Waiting for rollout of deployment/faq-resolver in namespace faq-resolver..."
    kubectl -n faq-resolver rollout status deployment/faq-resolver --timeout=120s
fi

echo "Deployment step completed successfully."
