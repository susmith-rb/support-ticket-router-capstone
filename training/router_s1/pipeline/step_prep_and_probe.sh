#!/usr/bin/env bash
# step_prep_and_probe.sh — sync code, re-run prepare with the fixed redaction, probe the teacher model
set -euo pipefail
export BUCKET=gs://susmith-fde-ticket-router-laya PROJECT=susmith-fde-ticket-router
mkdir -p ~/ticket-router-laya && gsutil -m -q rsync -r "$BUCKET/pipeline" ~/ticket-router-laya
cd ~/ticket-router-laya
[ -e .venv ] || ln -s ~/laya-work/.venv .venv
[ -e .venv-train ] || ln -s ~/laya-work/.venv-train .venv-train
.venv/bin/python -m pip install -q google-genai
(cd pipeline && ../.venv/bin/python test_redact.py)
mkdir -p data/raw && [ -f data/raw/twcs/twcs.csv ] || cp -r ~/laya-work/raw/twcs data/raw/
.venv/bin/python pipeline/prepare_twcs.py --csv data/raw/twcs/twcs.csv --brand AmazonHelp --out data/processed/ | tail -4
gsutil -m -q cp data/processed/* "$BUCKET/data/processed/"
echo "== teacher probe (5 msgs)"
cd pipeline
PROBE_MODELS="${PROBE_MODELS:-$(python3 -m config.models probe_model 2>/dev/null || echo 'probe_model') $(python3 -m config.models teacher 2>/dev/null || echo 'teacher')}"
for M in $PROBE_MODELS; do
  for L in global us-central1; do
    rm -f /tmp/probe.jsonl
    if timeout 300 ../.venv/bin/python label_teacher.py --inp ../data/processed/inbound_first_turn.parquet --out /tmp/probe.jsonl \
        --n 5 --samples 3 --model "$M" --location "$L" --concurrency 5 2>&1 | tail -4 && [ -s /tmp/probe.jsonl ]; then
      echo "WORKS: model=$M location=$L"; head -c 900 /tmp/probe.jsonl; echo; echo "$M $L" > /tmp/teacher_ok; exit 0
    fi
  done
done
echo "no teacher config worked"; exit 1
