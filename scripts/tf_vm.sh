#!/usr/bin/env bash
# scripts/tf_vm.sh - Run Terraform operations safely on the Argolis VM laya-tune.
#
# HARD INVARIANT: Terraform must NEVER be installed or executed on the cloudtop.
# All Terraform commands, provider downloads, and state operations execute on
# Workbench VM laya-tune over IAP SSH using susmith@gcp.altostrat.com credentials.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_NAME="laya-tune"
VM_ZONE="us-central1-b"
PROJECT_ID="susmith-fde-ticket-router"
ACCOUNT="susmith@gcp.altostrat.com"
GCS_BUCKET="susmith-fde-ticket-router-laya"
SCRATCH_DIR="${REPO_ROOT}/.scratch_tf"

# Allow override of scratch directory
if [ -n "${TF_SCRATCH_DIR:-}" ]; then
  SCRATCH_DIR="${TF_SCRATCH_DIR}"
elif [ -d "/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/tf" ]; then
  SCRATCH_DIR="/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/tf"
fi
mkdir -p "${SCRATCH_DIR}"

run_ssh() {
  local cmd="$1"
  # Forward the caller's short-lived OAuth token over stdin (never argv/disk) so Terraform on the VM
  # (google provider + GCS backend honor GOOGLE_OAUTH_ACCESS_TOKEN) does not depend on the VM's own
  # ADC login, which expires with invalid_rapt. Disable with TF_VM_FORWARD_TOKEN=0.
  local tok=""
  if [ "${TF_VM_FORWARD_TOKEN:-1}" = "1" ]; then
    tok="$(CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false gcloud auth print-access-token 2>/dev/null || true)"
    cmd="read -r GOOGLE_OAUTH_ACCESS_TOKEN; if [ -n \"\$GOOGLE_OAUTH_ACCESS_TOKEN\" ]; then export GOOGLE_OAUTH_ACCESS_TOKEN; else unset GOOGLE_OAUTH_ACCESS_TOKEN; fi; ${cmd}"
  fi
  printf '%s\n' "${tok}" | CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false \
    gcloud compute ssh "${VM_NAME}" \
      --zone "${VM_ZONE}" \
      --project "${PROJECT_ID}" \
      --account "${ACCOUNT}" \
      --tunnel-through-iap \
      --command "export PATH=\"\$HOME/bin:\$PATH\"; export CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false; ${cmd}"
}

sync_code() {
  echo "==> Packaging deploy/terraform from ${REPO_ROOT}..."
  local tmp_tar
  tmp_tar="$(mktemp /tmp/capstone-tf-XXXXXX.tar.gz)"
  tar -C "${REPO_ROOT}" \
    --exclude=".terraform" \
    --exclude="*.tfstate" \
    --exclude="*.tfstate.*" \
    -czf "${tmp_tar}" deploy/terraform

  echo "==> Uploading bundle to gs://${GCS_BUCKET}/tf-src/terraform.tar.gz..."
  CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false \
    gcloud storage cp "${tmp_tar}" "gs://${GCS_BUCKET}/tf-src/terraform.tar.gz"
  rm -f "${tmp_tar}"

  echo "==> Unpacking bundle on VM ${VM_NAME}..."
  run_ssh "
    mkdir -p ~/capstone-tf
    gcloud storage cp gs://${GCS_BUCKET}/tf-src/terraform.tar.gz /tmp/terraform.tar.gz
    tar -xzf /tmp/terraform.tar.gz -C ~/capstone-tf
    rm -f /tmp/terraform.tar.gz
    echo 'Synced deploy/terraform to ~/capstone-tf/deploy/terraform'
  "
}

pull_locks() {
  echo "==> Fetching .terraform.lock.hcl files from VM..."
  for target in bootstrap single-project agent-gateway; do
    local lock_content
    lock_content="$(run_ssh "cat ~/capstone-tf/deploy/terraform/${target}/.terraform.lock.hcl 2>/dev/null || true")"
    if [ -n "${lock_content}" ]; then
      echo "${lock_content}" > "${REPO_ROOT}/deploy/terraform/${target}/.terraform.lock.hcl"
      echo "  Updated deploy/terraform/${target}/.terraform.lock.hcl"
    fi
  done
}

run_logged() {
  local log_name="$1"
  shift
  local log_file="${SCRATCH_DIR}/${log_name}.log"
  echo "==> Running command (logging to ${log_file})..."
  set +e
  run_ssh "$@" 2>&1 | tee "${log_file}"
  local exit_code="${PIPESTATUS[0]}"
  set -e
  if [ "${exit_code}" -ne 0 ]; then
    echo "Command failed with exit code ${exit_code} (see ${log_file})" >&2
    return "${exit_code}"
  fi
}

cmd="${1:-help}"
shift || true

case "${cmd}" in
  sync)
    sync_code
    ;;

  pull-locks)
    pull_locks
    ;;

  fmt-check)
    run_logged "terraform_fmt_check" "cd ~/capstone-tf/deploy/terraform && terraform fmt -check -recursive"
    ;;

  fmt)
    run_ssh "cd ~/capstone-tf/deploy/terraform && terraform fmt -recursive"
    ;;

  tflint)
    run_logged "tflint" "cd ~/capstone-tf/deploy/terraform && tflint --recursive"
    ;;

  validate)
    target="${1:-single-project}"
    run_logged "terraform_validate_${target}" "cd ~/capstone-tf/deploy/terraform/${target} && terraform validate"
    ;;

  test)
    target="${1:-single-project}"
    run_logged "terraform_test_${target}" "cd ~/capstone-tf/deploy/terraform/${target} && terraform test"
    ;;

  run)
    target="$1"
    shift
    run_ssh "cd ~/capstone-tf/deploy/terraform/${target} && terraform $*"
    ;;

  logged-run)
    target="$1"
    log_name="$2"
    shift 2
    run_logged "${log_name}" "cd ~/capstone-tf/deploy/terraform/${target} && terraform $*"
    ;;

  ssh)
    run_ssh "$*"
    ;;

  help|*)
    cat << 'EOF'
Usage: scripts/tf_vm.sh <command> [args...]

Commands:
  sync                     Package and upload deploy/terraform to the VM
  pull-locks               Pull updated .terraform.lock.hcl files from the VM
  fmt-check                Run terraform fmt -check -recursive on the VM
  fmt                      Run terraform fmt -recursive on the VM
  tflint                   Run tflint --recursive on the VM
  validate <target>        Run terraform validate in deploy/terraform/<target>
  test <target>            Run terraform test in deploy/terraform/<target>
  run <target> <args...>   Run arbitrary terraform command in deploy/terraform/<target>
  logged-run <target> <log_name> <args...>  Run terraform and tee output to scratch/tf/<log_name>.log
  ssh <cmd...>             Run arbitrary command on the VM with environment set

Targets:
  bootstrap       (deploy/terraform/bootstrap)
  single-project  (deploy/terraform/single-project)
  agent-gateway   (deploy/terraform/agent-gateway)
EOF
    ;;
esac
