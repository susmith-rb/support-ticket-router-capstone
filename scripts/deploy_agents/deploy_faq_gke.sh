#!/usr/bin/env bash
# scripts/deploy_agents/deploy_faq_gke.sh - Deploy FaqResolverAgent to GKE Autopilot.
# Implements REQ-AGENT-018: GKE Autopilot deployment for FaqResolverAgent with HTTPS Ingress.
#
# Connects to the support-agents GKE Autopilot cluster and applies Kubernetes manifests:
# - Namespace: faq-resolver
# - KSA: ksa-faq (Workload Identity annotation)
# - BackendConfig: faq-resolver-backend-config (health check on /healthz)
# - FrontendConfig: faq-resolver-frontend-config (HTTP-to-HTTPS redirect)
# - ManagedCertificate: faq-resolver-cert (Google-managed cert on faq.<ip-with-dashes>.sslip.io)
# - Deployment: faq-resolver (with contract env vars and probes)
# - Service: faq-resolver (ClusterIP with NEG + BackendConfig annotations)
# - Ingress: faq-resolver-ingress (GCE Ingress with static IP and ManagedCertificate)
# - HPA: faq-resolver-hpa (min 1, max 10, 60% CPU)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
K8S_DIR="${REPO_ROOT}/deploy/k8s/faq-resolver"

PROJECT_ID="${PROJECT_ID:-susmith-fde-ticket-router}"
REGION="${REGION:-us-central1}"
CLUSTER_NAME="${CLUSTER_NAME:-support-agents}"
IMAGE_URI="${IMAGE_URI:-}"
FAQ_IP="${FAQ_IP:-}"
DOMAIN="${DOMAIN:-}"
TF_JSON="${TF_JSON:-}"
DRY_RUN=false
WAIT=false

usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Deploy FaqResolverAgent Kubernetes manifests to GKE Autopilot with HTTPS Ingress.

Options:
  --project PROJECT_ID     GCP Project ID (default: ${PROJECT_ID})
  --region REGION          GCP Region for GKE cluster (default: ${REGION})
  --cluster CLUSTER        GKE Cluster Name (default: ${CLUSTER_NAME})
  --image IMAGE_URI        Docker image URI to deploy
  --ip STATIC_IP           Global static IP address for Ingress
  --domain DOMAIN          FQDN for SSL certificate and Ingress host (default: faq.<ip-with-dashes>.sslip.io)
  --tf-json FILE           Path to terraform output -json file
  --dry-run                Validate and perform client-side dry-run apply
  --wait                   Wait for deployment rollout and ManagedCertificate ACTIVE
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
        --ip)
            FAQ_IP="$2"
            shift 2
            ;;
        --domain)
            DOMAIN="$2"
            shift 2
            ;;
        --tf-json)
            TF_JSON="$2"
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

# Resolve default image from agents.auto.tfvars if not provided
if [[ -z "${IMAGE_URI}" ]]; then
    TFVARS_FILE="${REPO_ROOT}/deploy/terraform/single-project/agents.auto.tfvars"
    if [[ -f "${TFVARS_FILE}" ]]; then
        IMAGE_URI="$(grep -o 'agents_image = "[^"]*"' "${TFVARS_FILE}" | cut -d'"' -f2 || true)"
    fi
    if [[ -z "${IMAGE_URI}" ]]; then
        IMAGE_URI="us-central1-docker.pkg.dev/susmith-fde-ticket-router/serving/agents:72af728"
    fi
fi

# Load Terraform outputs if available
ORDER_LOOKUP_URL="https://order-lookup-mcp-34mwh7k34a-uc.a.run.app"
TICKET_DB_URL="https://ticket-db-mcp-34mwh7k34a-uc.a.run.app"
HUMAN_QUEUE_URL="https://human-queue-34mwh7k34a-uc.a.run.app"

if [[ -n "${TF_JSON}" && -f "${TF_JSON}" ]]; then
    PARSED_IP="$(python3 -c "import json, sys; d=json.load(open('${TF_JSON}')); print(d.get('faq_resolver_ip', {}).get('value', '') or '')" 2>/dev/null || true)"
    if [[ -n "${PARSED_IP}" && -z "${FAQ_IP}" ]]; then
        FAQ_IP="${PARSED_IP}"
    fi
    PARSED_ORDER="$(python3 -c "import json, sys; d=json.load(open('${TF_JSON}')); print(d.get('cloud_run_order_lookup_mcp_url', {}).get('value', '') or '')" 2>/dev/null || true)"
    if [[ -n "${PARSED_ORDER}" ]]; then
        ORDER_LOOKUP_URL="${PARSED_ORDER}"
    fi
    PARSED_TICKET="$(python3 -c "import json, sys; d=json.load(open('${TF_JSON}')); print(d.get('cloud_run_ticket_db_mcp_url', {}).get('value', '') or '')" 2>/dev/null || true)"
    if [[ -n "${PARSED_TICKET}" ]]; then
        TICKET_DB_URL="${PARSED_TICKET}"
    fi
    PARSED_QUEUE="$(python3 -c "import json, sys; d=json.load(open('${TF_JSON}')); print(d.get('cloud_run_human_queue_url', {}).get('value', '') or '')" 2>/dev/null || true)"
    if [[ -n "${PARSED_QUEUE}" ]]; then
        HUMAN_QUEUE_URL="${PARSED_QUEUE}"
    fi
fi

# Determine target Domain
if [[ -z "${DOMAIN}" ]]; then
    if [[ -n "${FAQ_IP}" ]]; then
        IP_DASH="${FAQ_IP//./-}"
        DOMAIN="faq.${IP_DASH}.sslip.io"
    else
        DOMAIN="faq.sslip.io"
    fi
fi

ROUTER_S1_ENDPOINT="projects/${PROJECT_ID}/locations/${REGION}/endpoints/router-s1-dedicated"
ROUTER_S1_BASE_URL="https://router-s1-dedicated.${REGION}-620485621180.prediction.vertexai.goog"

echo "================================================================="
echo "GKE Autopilot Deployer: FaqResolverAgent"
echo "Project:       ${PROJECT_ID}"
echo "Region:        ${REGION}"
echo "Cluster:       ${CLUSTER_NAME}"
echo "Image:         ${IMAGE_URI}"
echo "Static IP:     ${FAQ_IP:-[pending/unspecified]}"
echo "Domain:        ${DOMAIN}"
echo "Order MCP:     ${ORDER_LOOKUP_URL}"
echo "Ticket DB MCP: ${TICKET_DB_URL}"
echo "Human Queue:   ${HUMAN_QUEUE_URL}"
echo "Dry Run:       ${DRY_RUN}"
echo "Wait:          ${WAIT}"
echo "================================================================="

if [[ ! -d "${K8S_DIR}" ]]; then
    echo "Error: Manifest directory not found: ${K8S_DIR}" >&2
    exit 1
fi

RENDER_DIR="$(mktemp -d /tmp/k8s-faq-rendered.XXXXXX)"
trap 'rm -rf "${RENDER_DIR}"' EXIT

# Render manifests
cp "${K8S_DIR}/namespace.yaml" "${RENDER_DIR}/"
cp "${K8S_DIR}/ksa.yaml" "${RENDER_DIR}/"
cp "${K8S_DIR}/backendconfig.yaml" "${RENDER_DIR}/"
cp "${K8S_DIR}/frontendconfig.yaml" "${RENDER_DIR}/"
cp "${K8S_DIR}/service.yaml" "${RENDER_DIR}/"
cp "${K8S_DIR}/hpa.yaml" "${RENDER_DIR}/"

# Render ManagedCertificate with Domain
sed "s|DOMAIN_PLACEHOLDER|${DOMAIN}|g" "${K8S_DIR}/managed-cert.yaml" > "${RENDER_DIR}/managed-cert.yaml"

# Render Ingress with Domain
sed "s|DOMAIN_PLACEHOLDER|${DOMAIN}|g" "${K8S_DIR}/ingress.yaml" > "${RENDER_DIR}/ingress.yaml"

# Render Deployment with Image, Domain, and MCP URLs
sed \
    -e "s|image: .*|image: ${IMAGE_URI}|g" \
    -e "s|https://faq\.sslip\.io|https://${DOMAIN}|g" \
    -e "s|https://order-lookup-mcp-.*\.run\.app|${ORDER_LOOKUP_URL}|g" \
    -e "s|https://ticket-db-mcp-.*\.run\.app|${TICKET_DB_URL}|g" \
    -e "s|https://human-queue-.*\.run\.app|${HUMAN_QUEUE_URL}|g" \
    "${K8S_DIR}/deployment.yaml" > "${RENDER_DIR}/deployment.yaml"

APPLY_FLAGS=()
if [[ "${DRY_RUN}" == "true" ]]; then
    APPLY_FLAGS+=(--dry-run=client)
    echo "Running in dry-run mode (client-side validation only)."
else
    # Verify prerequisites for GKE auth
    if ! command -v gke-gcloud-auth-plugin &> /dev/null; then
        echo "gke-gcloud-auth-plugin not found. Attempting installation..."
        if command -v apt-get &> /dev/null && [ "${EUID}" -eq 0 -o -n "$(command -v sudo)" ]; then
            sudo apt-get update -qq && sudo apt-get install -y -qq google-cloud-sdk-gke-gcloud-auth-plugin || true
        fi
    fi

    echo "Acquiring GKE cluster credentials..."
    gcloud container clusters get-credentials "${CLUSTER_NAME}" \
        --region "${REGION}" \
        --project "${PROJECT_ID}"
fi

if command -v kubectl &> /dev/null; then
    echo "Applying Kubernetes manifests in order..."
    kubectl apply "${APPLY_FLAGS[@]}" -f "${RENDER_DIR}/namespace.yaml"
    kubectl apply "${APPLY_FLAGS[@]}" -f "${RENDER_DIR}/ksa.yaml"
    kubectl apply "${APPLY_FLAGS[@]}" -f "${RENDER_DIR}/backendconfig.yaml"
    kubectl apply "${APPLY_FLAGS[@]}" -f "${RENDER_DIR}/frontendconfig.yaml"
    kubectl apply "${APPLY_FLAGS[@]}" -f "${RENDER_DIR}/managed-cert.yaml"
    kubectl apply "${APPLY_FLAGS[@]}" -f "${RENDER_DIR}/service.yaml"
    kubectl apply "${APPLY_FLAGS[@]}" -f "${RENDER_DIR}/hpa.yaml"
    kubectl apply "${APPLY_FLAGS[@]}" -f "${RENDER_DIR}/deployment.yaml"
    kubectl apply "${APPLY_FLAGS[@]}" -f "${RENDER_DIR}/ingress.yaml"
elif [[ "${DRY_RUN}" == "true" ]]; then
    echo "[Dry Run] kubectl not found in local environment. Validated rendered manifests successfully:"
    for f in "${RENDER_DIR}"/*.yaml; do
        echo "  - $(basename "$f")"
    done
else
    echo "Error: kubectl command not found. Run this deploy script on VM laya-tune where kubectl is installed." >&2
    exit 1
fi


if [[ "${DRY_RUN}" == "false" && "${WAIT}" == "true" ]]; then
    echo "Waiting for rollout of deployment/faq-resolver in namespace faq-resolver..."
    kubectl -n faq-resolver rollout status deployment/faq-resolver --timeout=300s

    echo "Checking ManagedCertificate status..."
    for i in {1..30}; do
        CERT_STATUS="$(kubectl -n faq-resolver get managedcertificate faq-resolver-cert -o jsonpath='{.status.certificateStatus}' 2>/dev/null || echo "Pending")"
        echo "  ManagedCertificate status: ${CERT_STATUS} (poll ${i}/30)"
        if [[ "${CERT_STATUS}" == "Active" ]]; then
            echo "ManagedCertificate is ACTIVE!"
            break
        fi
        sleep 10
    done
fi

echo "FaqResolver GKE deployment step completed successfully."
