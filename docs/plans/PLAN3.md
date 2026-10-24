# Conductor plan 3: TDD §6.3 filters, §6.4 experiments, recall toward 0.90 (2026-10-24)

The user asked for items 1, 2 and 3. Spec-driven: read specs/README.md and specs/rag-retrieval.md first.
Update the specs in the same change. Tests must carry `# REQ-...` comments, and
`tests/test_spec_traceability.py` must pass. Workers never commit, and they paste only real terminal output;
the conductor re-runs every receipt. Billing: Agent Search queries and runtime-path Vertex calls are
allowed. Offline LLM work (writing paraphrases or articles, judging) happens INSIDE the Jetski agent, never
through Vertex. Search serves from `global`.

Repo: /usr/local/google/home/susmith/src/support-ticket-router-capstone. Tests: `.venv/bin/python -m pytest` (122 pass).
Scratch: /usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag (S)
Live facts:
- faq-articles (global) default_schema auto-detected. `intent_family`, `status`, `theme` and `title` are
  indexable strings; `effective_from` is a datetime and indexable. Verify with a GET on
  .../dataStores/faq-articles/schemas/default_schema.
- All 50 articles have status=draft; none are reviewed yet.
- The manifest is rag/corpus_v2/manifest.jsonl; the live release is v3 in gs://…-laya/corpus/faq-articles/v3/.
- The test set is the 300 queries currently produced by retrieval_eval.py (seed 13). Rows are in
  S/ret_v2b_lowest_clean.json. Baseline on slice b (n=287): recall@5 0.780, MRR 0.567.

## Decisions (conductor)
D1 Status and effective-window filter (TDD §6.3, REQ-RAG-RETR-009)
- Filter: `status: ANY(<allowed>)` AND effective_from <= now AND (no effective_to OR effective_to > now).
- `allowed` comes from config `FAQ_ALLOWED_STATUSES`. The default is ["active","draft"] until human review
  activates articles; after that it becomes ["active"]. This is a config flip, not a code change (AGENTS
  non-negotiable 2).
- Verify the exact filter syntax against the live API, including datetime comparison and how documents
  missing a field behave. If a needed field is missing from docs or the schema, backfill it through
  publish_faq (metadata-only republish; FULL import; verify).
- A probe test must show the filter actually excludes. Example: publish nothing new; query with
  allowed=["active"] and assert zero results; query with ["active","draft"] and assert parity with
  unfiltered.

D2 Intent filter with fallback (TDD §6.3, REQ-RAG-RETR-010)
- `intent_family: ANY(<family>)` comes from the router's intent, mapped to the article intent families.
  If fewer than 2 results come back, retry unfiltered (second round).
- Offline evaluation uses the teacher label as the intent ("oracle"). Also run with the label randomly
  corrupted at the router-s1 intent error rate (1 − 0.86 accuracy, per training/router_s1/README.md) to
  estimate real-world benefit.
- Report filtered vs unfiltered recall@5. The TDD gate is filtered ≥ unfiltered − 0.02.

D3 Dev/test discipline (anti-overfitting)
- The existing 300 queries become the frozen TEST set. Never tune on it.
- Build a DEV set of 300 queries from held-out threads with the same silver-label method. It must be
  disjoint from TEST and from each theme's article-example threads (first 15 with an agent turn), and it
  uses a different seed.
- All ablations and tuning run on DEV. TEST is run once per finalist config.
- Paraphrases or articles added to the index must be derived only from threads in NEITHER dev nor test,
  and this must be asserted by a test.

D4 Metrics (TDD §6.4)
- Add nDCG@5 (binary relevance; single gold).
- Add client-side latency p50/p95 per query. Measure the HTTP call only and exclude throttle sleeps.
  Report it as "client-observed from cloudtop, includes network".
- Add filtered vs unfiltered delta.
- Keep recall@1/3/5/10 and MRR@10.

D5 Ablations on DEV (TDD §6.4; REQ-RAG-RETR-011)
Use the Agent Search knobs that actually exist. Verify each against the API docs and a live probe; if a
knob is unsupported on this engine/tier, record that instead of faking it.
- relevanceThreshold LOWEST/LOW
- default hybrid vs semantic-heavy vs keyword-heavy, via `rankingExpression` (e.g.
  semantic_similarity_score / keyword_similarity_score blends, with rankingExpressionBackend if required)
- managed re-ranking: retrieve top_k=20, then the Discovery Engine Ranking API (rankingConfigs:rank,
  semantic-ranker model), then take 5; vs no rerank
- query cleaning on/off
- the D2 intent filter
Report a table on DEV and pick finalists.

D6 Recall improvements toward 0.90 (REQ-RAG-RETR-011)
- In order:
  (a) best D5 config
  (b) intent filter with fallback
  (c) index-side: add real-user paraphrases to the article "Also asked as" lists and the title metadata,
      written by the Jetski agent from threads outside dev and test, for the weakest families first
      (other, order_status); republish through publish_faq with the regression gate
  (d) optional runtime query rewrite with Gemini Flash. It's a runtime-path call, so measure its cost and
      latency, and only keep it if the gain is worth it.
- Evaluate on DEV, then TEST once for the final config. Report honestly even if 0.90 isn't reached.

## Phase A (parallel)
Worker L, search client + filters:
- Files: rag/search_client.py (new; one place that builds search requests: filter, threshold,
  rankingExpression, pageSize, location; returns ids + latency_ms), rag/retrieval_eval.py (use the client;
  add `--queries <jsonl>`, `--filter-status`, `--intent-filter oracle|noisy|off`, metrics from
  rag/eval_metrics.py), rag/search_test.py, rag/publish_faq.py (only if a metadata backfill is needed),
  tests/rag/test_search_client.py, tests/rag/test_retrieval_eval.py, specs/rag-retrieval.md
  (REQ-009, REQ-010).
Worker B, eval splits + metrics:
- Files: rag/build_eval_splits.py (writes S/eval/test.jsonl from the existing 300 rows and S/eval/dev.jsonl,
  schema {qid, query, relevant, intent, split, thread_id}; asserts disjointness), rag/eval_metrics.py (pure:
  recall_at_k, mrr_at_k, ndcg_at_k, latency_percentiles, slice helpers for dropped/merged golds),
  tests/rag/test_build_eval_splits.py, tests/rag/test_eval_metrics.py, specs/rag-retrieval.md (the
  D3/D4 sections only; coordinate by appending a new REQ, e.g. REQ-RAG-RETR-013 dev/test discipline).
- Worker L imports eval_metrics once worker B delivers it.

## Phase B (after A): worker B, ablations (rag/ablation_eval.py + tests), DEV table, finalists → TEST once.
## Phase C: recall improvements per D6, with gate; EVAL_RESULTS.md §9; specs updated.
