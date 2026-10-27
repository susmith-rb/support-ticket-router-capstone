#!/usr/bin/env bash
# serving/laya/deploy.sh — Upload and deploy custom Laya serving container to Vertex AI.
#
# Follows the TDD §3.6 and user decision (2026-10-26):
# - Default machine: g2-standard-4 + 1x NVIDIA_L4 (overridable to n1-standard-4 + NVIDIA_TESLA_T4)
# - Default replicas: MIN_REPLICAS=1 (capstone default), MAX_REPLICAS=4 (overridable to 8 for load testing)
# - Autoscaling enabled with explicit CPU/GPU target utilization
#
# Flags:
#   --dry-run       Print all deployment commands without executing them
#   --help          Show help message

set -euo pipefail

# Configuration parameters with production defaults
PROJECT="${PROJECT:-susmith-fde-ticket-router}"
REGION="${REGION:-us-central1}"
IMAGE_URI="${IMAGE_URI:-${REGION}-docker.pkg.dev/${PROJECT}/serving/laya-serving:v2}"
ENDPOINT_NAME="${ENDPOINT_NAME:-router-s1-dedicated}"
MODEL_DISPLAY_NAME="${MODEL_DISPLAY_NAME:-router-s1-v2}"
MACHINE_TYPE="${MACHINE_TYPE:-g2-standard-4}"
ACCELERATOR="${ACCELERATOR:-NVIDIA_L4}"
ACCELERATOR_COUNT="${ACCELERATOR_COUNT:-1}"
MIN_REPLICAS="${MIN_REPLICAS:-1}"
MAX_REPLICAS="${MAX_REPLICAS:-4}"
AUTOSCALING_METRIC_TARGET="${AUTOSCALING_METRIC_TARGET:-60}"
ROUTER_CKPT_URI="${ROUTER_CKPT_URI:-gs://susmith-fde-ticket-router-laya/checkpoints/router-s1/v2/}"
RERANKER_CKPT_URI="${RERANKER_CKPT_URI:-gs://susmith-fde-ticket-router-laya/rerank/v2/ckpt/}"
SERVING_SA="${SERVING_SA:-ticket-router-serving@${PROJECT}.iam.gserviceaccount.com}"
ENDPOINT_ID="${ENDPOINT_ID:-}"
UNDEPLOY_PREVIOUS="${UNDEPLOY_PREVIOUS:-0}"

# Resolve version alias from container image tag (e.g. :v2 -> v2)
IMAGE_TAG=$(echo "${IMAGE_URI}" | awk -F':' '{print $NF}')
if [ -z "${IMAGE_TAG}" ] || [ "${IMAGE_TAG}" = "${IMAGE_URI}" ]; then
  IMAGE_TAG="v2"
fi
MODEL_VERSION_ALIAS="${MODEL_VERSION_ALIAS:-${IMAGE_TAG}}"

# Normalize accelerator type to lowercase-hyphen (e.g. NVIDIA_L4 -> nvidia-l4, NVIDIA_TESLA_T4 -> nvidia-tesla-t4)
ACCELERATOR_TYPE=$(echo "${ACCELERATOR}" | tr '[:upper:]_' '[:lower:]-')

DRY_RUN=false
for arg in "$@"; do
  case $arg in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --help)
      echo "Usage: ./serving/laya/deploy.sh [--dry-run]"
      exit 0
      ;;
  esac
done

echo "=========================================================================="
echo "DEPLOY LAYA ROUTER & RERANKER TO VERTEX AI"
echo "Project:               ${PROJECT}"
echo "Region:                ${REGION}"
echo "Container Image:       ${IMAGE_URI}"
echo "Endpoint Name:         ${ENDPOINT_NAME}"
echo "Model Display Name:    ${MODEL_DISPLAY_NAME}"
echo "Machine Type:          ${MACHINE_TYPE}"
echo "Accelerator:           ${ACCELERATOR} (count: ${ACCELERATOR_COUNT})"
echo "Replicas (Min/Max):    ${MIN_REPLICAS} / ${MAX_REPLICAS}"
echo "Target Utilization:    ${AUTOSCALING_METRIC_TARGET}%"
echo "Serving SA:            ${SERVING_SA}"
echo "Undeploy Previous:     ${UNDEPLOY_PREVIOUS}"
echo "Router Checkpoint:     ${ROUTER_CKPT_URI}"
echo "Reranker Checkpoint:   ${RERANKER_CKPT_URI}"
echo "Dry Run Mode:          ${DRY_RUN}"
echo "=========================================================================="

export CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false

# 1. Resolve Vertex AI Endpoint ID
if [ -z "${ENDPOINT_ID}" ]; then
  echo "Resolving endpoint ID for display name '${ENDPOINT_NAME}' in ${REGION}..."
  if [ "${DRY_RUN}" = true ]; then
    ENDPOINT_ID="mock-endpoint-12345"
    echo "[DRY-RUN] Resolved mock endpoint ID: ${ENDPOINT_ID}"
  else
    ENDPOINT_ID=$(gcloud ai endpoints list \
      --project="${PROJECT}" \
      --region="${REGION}" \
      --filter="displayName=${ENDPOINT_NAME}" \
      --format="value(name)" \
      | head -n 1)

    if [ -z "${ENDPOINT_ID}" ]; then
      echo "ERROR: Vertex AI Endpoint '${ENDPOINT_NAME}' not found in region ${REGION}."
      echo "Ensure the Terraform router-endpoint module has created the endpoint."
      exit 1
    fi
    echo "Found Endpoint ID: ${ENDPOINT_ID}"
  fi
fi

# 2. Upload Model to Vertex AI Model Registry
UPLOAD_CMD=(
  gcloud ai models upload
  --project="${PROJECT}"
  --region="${REGION}"
  --display-name="${MODEL_DISPLAY_NAME}"
  --version-aliases="${MODEL_VERSION_ALIAS}"
  --container-image-uri="${IMAGE_URI}"
  --container-health-route="/health"
  --container-predict-route="/predict"
  --container-ports="8080"
  --container-env-vars="ROUTER_CKPT_URI=${ROUTER_CKPT_URI},RERANKER_CKPT_URI=${RERANKER_CKPT_URI}"
)

echo ""
echo "Step 1: Uploading Model to Model Registry..."
if [ "${DRY_RUN}" = true ]; then
  echo "[DRY-RUN] ${UPLOAD_CMD[*]}"
  MODEL_ID="mock-model-67890"
else
  "${UPLOAD_CMD[@]}"
  echo "Model upload initiated. Resolving Model ID for display name '${MODEL_DISPLAY_NAME}'..."
  MODEL_RESOURCE=$(gcloud ai models list \
    --project="${PROJECT}" \
    --region="${REGION}" \
    --filter="displayName=${MODEL_DISPLAY_NAME}" \
    --sort-by="~createTime" \
    --limit=1 \
    --format="value(name)")
  MODEL_ID=$(echo "${MODEL_RESOURCE}" | awk -F'/' '{print $NF}')
fi

if [ -z "${MODEL_ID}" ]; then
  echo "ERROR: Failed to resolve Model ID for display name '${MODEL_DISPLAY_NAME}' in region '${REGION}'."
  echo "Check 'gcloud ai models list --project=${PROJECT} --region=${REGION}' to inspect uploaded models."
  exit 1
fi
echo "Resolved Model ID: ${MODEL_ID}"

# 3. Deploy Model to Vertex AI Endpoint
DEPLOY_CMD=(
  gcloud ai endpoints deploy-model "${ENDPOINT_ID}"
  --project="${PROJECT}"
  --region="${REGION}"
  --model="${MODEL_ID}"
  --display-name="${MODEL_DISPLAY_NAME}-deployment"
  --machine-type="${MACHINE_TYPE}"
  --accelerator="type=${ACCELERATOR_TYPE},count=${ACCELERATOR_COUNT}"
  --min-replica-count="${MIN_REPLICAS}"
  --max-replica-count="${MAX_REPLICAS}"
  --traffic-split=0=100
  --service-account="${SERVING_SA}"
  --autoscaling-metric-specs="gpu-duty-cycle=${AUTOSCALING_METRIC_TARGET}"
  --enable-access-logging
)

echo ""
echo "Step 2: Deploying Model to Endpoint with 100% Traffic Split..."
if [ "${DRY_RUN}" = true ]; then
  echo "[DRY-RUN] ${DEPLOY_CMD[*]}"
else
  "${DEPLOY_CMD[@]}"
  echo "Successfully deployed model ${MODEL_ID} to endpoint ${ENDPOINT_ID}."
fi

# 4. Undeploy Previous Models (Optional)
if [ "${UNDEPLOY_PREVIOUS}" = "1" ] || [ "${UNDEPLOY_PREVIOUS}" = "true" ]; then
  echo ""
  echo "Step 3: Checking for previous deployed models to undeploy (UNDEPLOY_PREVIOUS=1)..."
  if [ "${DRY_RUN}" = true ]; then
    echo "[DRY-RUN] gcloud ai endpoints describe ${ENDPOINT_ID} --project=${PROJECT} --region=${REGION} --format=json"
    echo "[DRY-RUN] gcloud ai endpoints update ${ENDPOINT_ID} --project=${PROJECT} --region=${REGION} --traffic-split=mock-deployed-new=100"
    echo "[DRY-RUN] gcloud ai endpoints undeploy-model ${ENDPOINT_ID} --project=${PROJECT} --region=${REGION} --deployed-model-id=mock-deployed-old"
  else
    ENDPOINT_JSON=$(gcloud ai endpoints describe "${ENDPOINT_ID}" \
      --project="${PROJECT}" \
      --region="${REGION}" \
      --format="json")

    NEW_DEPLOYED_MODEL_ID=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read())
    matches = [m.get('id') for m in data.get('deployedModels', []) if m.get('model', '').endswith('/' + '${MODEL_ID}')]
    print(matches[-1] if matches else (data.get('deployedModels', [{}])[-1].get('id', '')))
except Exception:
    pass
" <<< "${ENDPOINT_JSON}")

    PREVIOUS_MODEL_IDS=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read())
    new_id = '${NEW_DEPLOYED_MODEL_ID}'
    others = [m.get('id') for m in data.get('deployedModels', []) if m.get('id') and m.get('id') != new_id]
    print(' '.join(others))
except Exception:
    pass
" <<< "${ENDPOINT_JSON}")

    if [ -n "${NEW_DEPLOYED_MODEL_ID}" ] && [ -n "${PREVIOUS_MODEL_IDS}" ]; then
      echo "New deployed model ID: ${NEW_DEPLOYED_MODEL_ID}"
      echo "Previous deployed model IDs to remove: ${PREVIOUS_MODEL_IDS}"
      echo "Updating endpoint traffic-split strictly to ${NEW_DEPLOYED_MODEL_ID}=100..."
      gcloud ai endpoints update "${ENDPOINT_ID}" \
        --project="${PROJECT}" \
        --region="${REGION}" \
        --traffic-split="${NEW_DEPLOYED_MODEL_ID}=100"

      for prev_id in ${PREVIOUS_MODEL_IDS}; do
        echo "Undeploying previous deployed model ${prev_id}..."
        gcloud ai endpoints undeploy-model "${ENDPOINT_ID}" \
          --project="${PROJECT}" \
          --region="${REGION}" \
          --deployed-model-id="${prev_id}"
      done
      echo "Successfully undeployed previous models."
    else
      echo "No previous deployed models found to undeploy."
    fi
  fi
fi

echo ""
echo "Final Endpoint Deployed Models and Traffic Split:"
if [ "${DRY_RUN}" = true ]; then
  echo "[DRY-RUN] gcloud ai endpoints describe ${ENDPOINT_ID} --project=${PROJECT} --region=${REGION} --format=\"yaml(deployedModels,trafficSplit)\""
else
  gcloud ai endpoints describe "${ENDPOINT_ID}" \
    --project="${PROJECT}" \
    --region="${REGION}" \
    --format="yaml(deployedModels,trafficSplit)"
fi

echo ""
echo "Deployment complete."
