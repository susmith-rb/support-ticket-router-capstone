#!/usr/bin/env bash
# step_system_sim.sh — dump v2 predictions on val+test and run the end-to-end router simulation
set -euo pipefail
export BUCKET=gs://susmith-fde-ticket-router-laya
RUN="${RUN:-v2}"; DS="${DS:-v2}"
gsutil -m -q rsync -r "$BUCKET/pipeline" ~/ticket-router-laya
cd ~/ticket-router-laya && source .venv/bin/activate
python -m pip install -q langdetect
for split in val test; do
  python pipeline/evaluate.py --ckpt runs/$RUN/ckpt --data data/dataset/$DS/$split.jsonl \
     --out runs/$RUN/eval_$split.json --dump runs/$RUN/pred_$split.jsonl > /dev/null
done
python pipeline/simulate_system.py --val runs/$RUN/pred_val.jsonl --test runs/$RUN/pred_test.jsonl --out runs/$RUN/system_sim.json
gsutil -m -q cp runs/$RUN/system_sim.json runs/$RUN/eval_val.json "$BUCKET/runs/$RUN/"
