#!/usr/bin/env bash
# vm_setup.sh — one-time setup on the GPU Workbench VM. Idempotent.
#   BUCKET=gs://<project>-laya bash pipeline/vm_setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=/opt/micromamba/bin/python; [ -x "$PY" ] || PY=python3   # Workbench system python is PEP-668 locked
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

# serving/eval venv: same laya version the Model Garden container pins
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv; . .venv/bin/activate
  python -m pip install -q --upgrade pip
  python -m pip install -q "laya[serve]==0.3.20" google-genai pandas pyarrow huggingface_hub
  deactivate
fi
# training venv: laya-train only exists in laya >= 0.4.x
if [ ! -x .venv-train/bin/laya-train ]; then
  "$PY" -m venv .venv-train; . .venv-train/bin/activate
  python -m pip install -q --upgrade pip
  python -m pip install -q "laya==0.4.13" datasets pyarrow
  deactivate
fi
.venv/bin/python -m pip show laya | grep ^Version
.venv-train/bin/python -m pip show laya | grep ^Version
echo "setup ok"
