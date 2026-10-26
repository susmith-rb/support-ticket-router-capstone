#!/usr/bin/env bash
# step_eval.sh — (re)evaluate an existing run + zero-shot baselines, write manifest, sync to GCS
set -euo pipefail
export BUCKET=gs://susmith-fde-ticket-router-laya
RUN="${RUN:-v1}"; DS="${DS:-v1}"
gsutil -m -q rsync -r "$BUCKET/pipeline" ~/ticket-router-laya
cd ~/ticket-router-laya && source .venv/bin/activate
OUT=runs/$RUN; DATA=data/dataset/$DS
python pipeline/evaluate.py --ckpt "$OUT/ckpt" --data "$DATA/test.jsonl" --out "$OUT/eval_test.json" > "$OUT/eval_test.txt"
python pipeline/evaluate.py --ckpt convaiinnovations/laya --data "$DATA/test.jsonl" --out runs/baseline_english_$DS.json > runs/baseline_english_$DS.txt 2>&1 || echo "baseline english failed: $(tail -2 runs/baseline_english_$DS.txt)"
python pipeline/evaluate.py --ckpt convaiinnovations/laya --subfolder typed-decisions --data "$DATA/test.jsonl" --out runs/baseline_typed-decisions_$DS.json > runs/baseline_typed-decisions_$DS.txt 2>&1 || echo "baseline typed-decisions failed: $(tail -2 runs/baseline_typed-decisions_$DS.txt)"
SHA=$(sha256sum $OUT/ckpt/model.safetensors | cut -c1-16)
EPOCHS=$(grep -oE 'epochs=[0-9]+' logs/train_$RUN.log | head -1 | cut -d= -f2)
TS=$(grep -oE 'train_seconds=[0-9]+' logs/train_$RUN.log | tail -1 | cut -d= -f2)
TEACHER_NAME="${TEACHER_NAME:-$(python3 -m config.models teacher 2>/dev/null || echo 'teacher')} x3"
printf '{"run":"%s","data":"%s","base":"english","loss":"soft-ce","shuffle_options":true,"epochs":%s,"seed":0,"train_seconds":%s,"gpu":"T4","teacher":"%s","ckpt_sha256_16":"%s"}\n' "$RUN" "$DATA" "${EPOCHS:-4}" "${TS:-0}" "$TEACHER_NAME" "$SHA" > $OUT/manifest.json
cp logs/train_$RUN.log $OUT/train_full.log 2>/dev/null || true
gsutil -m -q rsync -r $OUT "$BUCKET/runs/$RUN"; gsutil -q cp runs/baseline_* "$BUCKET/runs/"
gsutil -m -q rsync -r $OUT/ckpt "$BUCKET/checkpoints/router-s1/$RUN"
echo "=== $RUN"; cat $OUT/eval_test.txt
for f in runs/baseline_english_$DS.txt runs/baseline_typed-decisions_$DS.txt; do echo "=== $f"; head -60 $f; done
cat $OUT/manifest.json
