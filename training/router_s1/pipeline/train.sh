#!/usr/bin/env bash
# train.sh — fine-tune Laya on a dataset version and evaluate it (run ON the GPU VM).
#   RUN=v1 DATA=data/dataset/v1 BASE=english bash pipeline/train.sh
# Uses two venvs: .venv-train (laya>=0.4.13, has laya-train) and .venv (laya==0.3.20, the
# version the Model Garden serving container pins) so evaluation also proves servability.
set -euo pipefail
cd "$(dirname "$0")/.."
RUN="${RUN:?set RUN}"; DATA="${DATA:?set DATA}"; BASE="${BASE:-english}"
LOSS="${LOSS:-soft-ce}"; SEED="${SEED:-0}"; TARGET_UPDATES="${TARGET_UPDATES:-300}"
OUT="runs/$RUN"; mkdir -p "$OUT"

# size the run in optimizer updates (laya docs: small runs < ~100 updates can sit at chance)
N=$(wc -l < "$DATA/train.jsonl"); ITEMS=$(( N * 4 * 9 / 10 ))          # 4 questions, 10% calibration slice
PER_EPOCH=$(( (ITEMS + 63) / 64 ))
EPOCHS="${EPOCHS:-$(( (TARGET_UPDATES + PER_EPOCH - 1) / PER_EPOCH ))}"
(( EPOCHS < 3 )) && EPOCHS=3; (( EPOCHS > 12 )) && EPOCHS=12
echo "run=$RUN data=$DATA base=$BASE loss=$LOSS rows=$N items~$ITEMS epochs=$EPOCHS (~$(( EPOCHS * PER_EPOCH )) updates)"

source .venv-train/bin/activate
laya-train --data "$DATA/train.jsonl" --base "$BASE" --dry-run | tee "$OUT/dry_run.txt"
START=$(date +%s)
laya-train --data "$DATA/train.jsonl" --eval "$DATA/val.jsonl" --base "$BASE" --out "$OUT/ckpt" \
  --loss "$LOSS" --shuffle-options --epochs "$EPOCHS" --micro-batch 4 --grad-accum 16 --seed "$SEED" \
  2>&1 | tr '\r' '\n' | grep -v -E 'it/s\]|s/it\]' | tee "$OUT/train.log"
echo "train_seconds=$(( $(date +%s) - START ))" | tee -a "$OUT/train.log"
deactivate

source .venv/bin/activate
python pipeline/evaluate.py --ckpt "$OUT/ckpt" --data "$DATA/test.jsonl" --out "$OUT/eval_test.json" | tee "$OUT/eval_test.txt"
python - "$OUT" "$DATA" "$BASE" "$LOSS" "$EPOCHS" "$SEED" <<'EOF'
import json, sys, hashlib, pathlib
out, data, base, loss, epochs, seed = sys.argv[1:]
h = hashlib.sha256(pathlib.Path(out, "ckpt", "model.safetensors").read_bytes()).hexdigest()[:16]
json.dump({"run": pathlib.Path(out).name, "data": data, "base": base, "loss": loss, "epochs": int(epochs),
           "seed": int(seed), "ckpt_sha256_16": h}, open(pathlib.Path(out, "manifest.json"), "w"), indent=1)
print("manifest:", pathlib.Path(out, "manifest.json").read_text())
EOF
# zero-shot baselines on the same test split (laya.load needs the Hub id; `english` is the repo root)
DS=$(basename "$DATA")
[ -f "runs/baseline_english_$DS.json" ] || python pipeline/evaluate.py --ckpt convaiinnovations/laya \
  --data "$DATA/test.jsonl" --out "runs/baseline_english_$DS.json" > "runs/baseline_english_$DS.txt" 2>&1 || echo "baseline english failed"
[ -f "runs/baseline_typed-decisions_$DS.json" ] || python pipeline/evaluate.py --ckpt convaiinnovations/laya --subfolder typed-decisions \
  --data "$DATA/test.jsonl" --out "runs/baseline_typed-decisions_$DS.json" > "runs/baseline_typed-decisions_$DS.txt" 2>&1 || echo "baseline typed-decisions failed"
if [ -n "${BUCKET:-}" ]; then gsutil -m -q rsync -r "$OUT" "$BUCKET/runs/$RUN"; gsutil -q cp runs/baseline_* "$BUCKET/runs/" || true; fi
echo "DONE-TRAIN $RUN"
