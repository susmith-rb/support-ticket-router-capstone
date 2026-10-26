#!/usr/bin/env bash
# retrieval_sweep.sh — query-side hill-climb over Agent Search settings on the same 300 queries
R=/usr/local/google/home/susmith/src/support-ticket-router-capstone/rag
W=/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag
cd /tmp
run() { name=$1; shift; python3 $R/retrieval_eval.py --themes $W/faq/themes.json --threads $W/threads.jsonl \
          --import $W/faq/faq_articles_import.jsonl --n 300 --out $W/ret_$name.json "$@" > $W/ret_$name.log 2>&1
        }
run base_default
run low --threshold LOW
run lowest --threshold LOWEST
run lowest_clean --threshold LOWEST --clean
run lowest_clean_exp --threshold LOWEST --clean --expansion
wait
for n in base_default low lowest lowest_clean lowest_clean_exp; do
  grep -E '"(zero_result_queries|recall@1|recall@3|recall@5|recall@10|mrr@10)"' $W/ret_$n.log | tr -d ' \n' | sed "s/^/$n: /"; echo
  grep -E "HTTP [0-9]+" $W/ret_$n.log | head -1
done
