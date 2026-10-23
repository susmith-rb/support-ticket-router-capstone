# RAG corpus: Agent Search (`faq-articles` + `resolved-threads`)

Builds the retrieval layer the FaqResolverAgent uses (TDD §6). Everything runs from a machine with
`gcloud` signed in to the project (no VM needed).

```
labels.jsonl (teacher topic per message) ─┐
threads.jsonl (81k redacted threads) ─────┼─ build_faq_corpus.py ──▶ themes.json + 53 FAQ articles (.txt) + import JSONL
                                          └─ build_threads_corpus.py ▶ 81,103 thread documents (JSONL, structData)
agent_search_setup.sh create | import-threads | import-faq | engine   ──▶ data stores + search app `support-kb-search`
search_test.py "query" ...    ──▶ top-5 FAQ results (+ summary with citations) and similar threads
retrieval_eval.py / retrieval_sweep.sh  ──▶ recall@k / MRR on 300 held-out real customer messages
```

## What's in Agent Search (project `susmith-fde-ticket-router`, location `global`)

| Data store | Content | Role |
| --- | --- | --- |
| `faq-articles` | 53 help-center articles for the simulated retailer *Global Retail Hub* (unstructured `.txt`; metadata: title, intent_family, theme, version, simulated_policy, reviewed) | **The only citable source.** One article = one chunk (200–600 tokens), so the question and its policy stay together |
| `resolved-threads` | 81,103 AmazonHelp customer↔agent threads, one thread per document (thread-aware; tweets are short, nothing is split mid-conversation), teacher labels as metadata where available | Secondary evidence (how similar cases were handled); never cited alone |

Search app: `support-kb-search` (Enterprise tier + LLM add-on) over both stores. Queries pass
`dataStoreSpecs` to target one store.

## How the FAQ articles were made

1. The teacher's free-text `topic` (on 4,446 labeled messages) was clustered per intent family into 56 themes by Gemini 3.1 Pro.
2. For each theme, Gemini wrote one article from up to 15 real threads on that theme (customer language and agents' troubleshooting steps), using **one canonical simulated policy block** (`SIMULATED_POLICY` in `build_faq_corpus.py`) so articles never contradict each other on windows, fees or timelines.
3. Three "positive feedback" themes produced nothing answerable and were dropped (`drop_articles.sh`) → **53 articles**.
4. All articles carry `reviewed: false` until a human review pass.

Cost: ≈ 163k input / 81k output (incl. thinking) tokens ≈ \$1.30 (est., Gemini 3.1 Pro preview list prices).

## Retrieval quality

Silver-standard eval: 300 real customer first messages whose topic was clustered into a theme that became an article (relevant doc = that article). Messages used as writing examples are excluded. See `results/` for per-config JSON.

Query-side sweep (same 300 queries, `retrieval_sweep.sh`, 2026-10-23):

| Config | Zero-result queries | recall@1 | recall@3 | recall@5 | recall@10 | MRR@10 |
| --- | --- | --- | --- | --- | --- | --- |
| API default (= `relevanceThreshold: LOW`) | 113 | 0.327 | 0.427 | 0.467 | 0.480 | 0.384 |
| `LOWEST` | 9 | 0.417 | 0.623 | 0.750 | 0.847 | 0.551 |
| **`LOWEST` + cleaned query** (strip @mentions, redaction tokens, `#`) | **9** | **0.430** | **0.653** | **0.760** | **0.840** | **0.562** |
| `LOWEST` + cleaned + query expansion / spell correction | 9 | 0.430 | 0.650 | 0.757 | 0.837 | 0.561 |

recall@5 by intent family (best config): account_payment 0.78 · order_status 0.82 · returns_refunds 0.86 · shipping_policy 0.78 · **other 0.59**.

Takeaways:
- The default relevance threshold silently dropped 38% of real (noisy, short) customer queries. `LOWEST` is the single biggest win; the FaqResolver's citation validator and grounding check are the safety net for weaker matches.
- Cleaning helps a little; query expansion adds nothing on this corpus.
- Below the TDD G5 target (recall@5 ≥ 0.90). The labels are silver (one relevant article per query) and several themes overlap (e.g. late delivery vs. late Hub Plus delivery vs. order not arrived), so this underestimates real usefulness — but the `other` family (0.59) is a real gap, the same taxonomy problem the router has. Next (index side): merge overlapping articles, split `other` by topic, boost by the router's intent family, then re-run this eval.

Gotchas:
- `dataStoreSpecs[].dataStore` must be the resource name (`projects/…/dataStores/faq-articles`), not a URL.
- New projects have a low `search_requests_regional` quota; `retrieval_eval.py --sleep` throttles and retries 429s.
- `gcloud auth print-access-token` tokens are CBA-bound on corp machines; the Python scripts use ADC tokens.
- The Discovery Engine service agent (`service-<num>@gcp-sa-discoveryengine…`) only exists after the first API call; grant it `storage.objectViewer` on the bucket after creating the stores.
