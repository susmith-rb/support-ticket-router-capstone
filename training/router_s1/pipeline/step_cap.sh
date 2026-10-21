#!/usr/bin/env bash
# step_cap.sh — stop the running v2 chain cleanly and relaunch it with a lower label cap (N)
set -uo pipefail
N="${N:-6000}"
cd ~/ticket-router-laya
pkill -f "pipeline/run_v2.sh" && echo "chain stopped"
pkill -INT -f "label_teacher.py" && echo "labeler SIGINT sent"
for i in $(seq 1 30); do pgrep -f label_teacher.py >/dev/null || break; sleep 2; done
pgrep -f label_teacher.py >/dev/null && { pkill -TERM -f label_teacher.py; sleep 3; }
sudo shutdown -c 2>/dev/null || true
# drop a trailing partial line if the kill landed mid-write
cat > /tmp/fix_jsonl.py <<'EOF'
import json, sys
p = sys.argv[1]; good = []
for line in open(p):
    try: json.loads(line); good.append(line)
    except Exception: pass
open(p, "w").writelines(good); print("valid labels:", len(good))
EOF
.venv/bin/python /tmp/fix_jsonl.py data/labels/labels.jsonl
gsutil -q cp data/labels/labels.jsonl gs://susmith-fde-ticket-router-laya/data/labels/labels.jsonl
echo "$(date -u +%FT%TZ) capped: relaunching with N=$N" >> logs/v2_status.txt
(N=$N setsid nohup bash pipeline/run_v2.sh >> logs/run_v2.log 2>&1 < /dev/null &)
sleep 60; tail -4 logs/v2_status.txt; tail -2 logs/label_v2.log
