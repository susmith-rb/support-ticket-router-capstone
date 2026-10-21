#!/usr/bin/env bash
# run_v2.sh — unattended v2 chain on the GPU VM:
#   probe -> label to N (resumable, partial uploads every 10 min) -> build dataset v2 -> train v2 + eval
#   -> sync everything to GCS -> write STATUS -> stop the VM (saves GPU cost; data is in GCS).
# Launch detached:  setsid nohup bash pipeline/run_v2.sh > logs/run_v2.log 2>&1 < /dev/null &
set -uo pipefail
export BUCKET=gs://susmith-fde-ticket-router-laya PROJECT=susmith-fde-ticket-router
N="${N:-10000}"; CONC="${CONC:-48}"; DS=v2; RUN=v2; STOP_VM="${STOP_VM:-1}"
cd ~/ticket-router-laya
status() { echo "$(date -u +%FT%TZ) $*" | tee -a logs/v2_status.txt; gsutil -q cp logs/v2_status.txt "$BUCKET/runs/$RUN/STATUS.txt" || true; }
finish() { gsutil -q cp logs/run_v2.log "$BUCKET/runs/$RUN/run_v2.log" || true
           if [ "$STOP_VM" = "1" ]; then status "stopping VM"; sudo shutdown -h +2; fi; exit "${1:-0}"; }
mkdir -p logs data/labels
status "start N=$N concurrency=$CONC"

# 1) probe the schema change (topic field) on 3 messages
(cd pipeline && ../.venv/bin/python label_teacher.py --inp ../data/processed/inbound_first_turn.parquet \
   --out /tmp/probe_v2.jsonl --n 3 --samples 1 --concurrency 3) >/dev/null 2>&1
grep -q '"topic"' /tmp/probe_v2.jsonl 2>/dev/null || { status "FAILED: teacher probe (no topic field)"; finish 1; }
rm -f /tmp/probe_v2.jsonl

# 2) label (resumes after the 2,000 v1 labels; same seed => same first 2,000 messages)
( while sleep 600; do gsutil -q cp data/labels/labels.jsonl "$BUCKET/data/labels/labels.jsonl" 2>/dev/null; \
    status "labels so far: $(wc -l < data/labels/labels.jsonl)"; done ) & UPLOADER=$!
for attempt in 1 2 3; do
  (cd pipeline && ../.venv/bin/python label_teacher.py --inp ../data/processed/inbound_first_turn.parquet \
     --out ../data/labels/labels.jsonl --n "$N" --samples 3 --concurrency "$CONC") >> logs/label_v2.log 2>&1 && break
  status "labeler exited non-zero (attempt $attempt), retrying in 60s"; sleep 60
done
kill $UPLOADER 2>/dev/null
gsutil -q cp data/labels/labels.jsonl "$BUCKET/data/labels/labels.jsonl"
NL=$(wc -l < data/labels/labels.jsonl); status "labeling done: $NL labels"
(( NL >= N * 8 / 10 )) || { status "FAILED: only $NL labels (< 80% of $N)"; finish 1; }

# 3) dataset v2
(cd pipeline && ../.venv/bin/python build_dataset.py --labels ../data/labels/labels.jsonl --out ../data/dataset/$DS) > logs/build_v2.log 2>&1 \
  || { status "FAILED: build_dataset"; finish 1; }
gsutil -m -q rsync -r data/dataset/$DS "$BUCKET/data/dataset/$DS"
status "dataset v2: $(head -c 300 data/dataset/$DS/stats.json | tr '\n' ' ')"

# 4) train + eval (train.sh syncs runs/v2 and baselines to GCS)
env BUCKET=$BUCKET RUN=$RUN DATA=data/dataset/$DS BASE=english bash pipeline/train.sh > logs/train_$RUN.log 2>&1 \
  || { status "FAILED: train.sh (see logs/train_$RUN.log)"; gsutil -q cp logs/train_$RUN.log "$BUCKET/runs/$RUN/"; finish 1; }
cp logs/train_$RUN.log runs/$RUN/train_full.log; gsutil -m -q rsync -r runs/$RUN "$BUCKET/runs/$RUN"
gsutil -m -q rsync -r runs/$RUN/ckpt "$BUCKET/checkpoints/router-s1/$RUN"
status "DONE v2: $(grep -A3 '"sentiment"' runs/$RUN/eval_test.txt | tr -s ' \n' ' ' | head -c 200)"
finish 0
