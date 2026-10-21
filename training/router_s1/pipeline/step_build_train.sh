#!/usr/bin/env bash
# step_build_train.sh — after labeling: build dataset v1, print stats, launch train.sh RUN=v1 in background
set -euo pipefail
export BUCKET=gs://susmith-fde-ticket-router-laya PROJECT=susmith-fde-ticket-router
DS="${DS:-v1}"; RUN="${RUN:-v1}"
gsutil -m -q rsync -r "$BUCKET/pipeline" ~/ticket-router-laya
cd ~/ticket-router-laya
tail -2 logs/label.log
grep -q LABEL-DONE logs/label.log || { echo "labeling not finished yet: $(wc -l < data/labels/labels.jsonl) rows"; exit 2; }
(cd pipeline && ../.venv/bin/python build_dataset.py --labels ../data/labels/labels.jsonl --out ../data/dataset/$DS)
gsutil -m -q rsync -r data/dataset/$DS "$BUCKET/data/dataset/$DS"
mkdir -p logs
nohup env BUCKET=$BUCKET RUN=$RUN DATA=data/dataset/$DS BASE=english bash pipeline/train.sh > logs/train_$RUN.log 2>&1 &
sleep 45; tail -5 logs/train_$RUN.log
