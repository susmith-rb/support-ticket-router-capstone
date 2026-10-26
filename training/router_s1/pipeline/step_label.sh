#!/usr/bin/env bash
# step_label.sh — sync code and run the teacher-labeling pilot in the background (resumable)
set -euo pipefail
export BUCKET=gs://susmith-fde-ticket-router-laya PROJECT=susmith-fde-ticket-router
N="${N:-2000}"
gsutil -m -q rsync -r "$BUCKET/pipeline" ~/ticket-router-laya
cd ~/ticket-router-laya/pipeline
mkdir -p ../data/labels ../logs
nohup bash -c "../.venv/bin/python label_teacher.py --inp ../data/processed/inbound_first_turn.parquet \
   --out ../data/labels/labels.jsonl --n $N --samples 3 --concurrency 32 \
   && gsutil -q cp ../data/labels/labels.jsonl $BUCKET/data/labels/labels.jsonl && echo LABEL-DONE" \
   > ../logs/label.log 2>&1 &
sleep 60; tail -5 ../logs/label.log
