# Specification: Billing, Quota & Model Guardrails (`billing-and-models`)

This specification defines the operational guardrails governing LLM execution, Vertex AI API usage within the Argolis sandbox project (`susmith-fde-ticket-router`), permitted runtime calls, and mandatory token-based cost accounting.

---

## 1. Overview & Policy Rationale

The Argolis project operates under strict sandbox quota and billing allocations. Uncontrolled batch generation or offline dataset operations against Vertex AI endpoints can quickly deplete project budgets or trigger hard quota denials (HTTP 429).

### Hard Boundary: Offline vs. Runtime Execution

```
┌────────────────────────────────────────────────────────┐     ┌────────────────────────────────────────────────────────┐
│               OFFLINE DEVELOPMENT WORK                 │     │                 RUNTIME OPERATIONS                     │
│               (Jetski Agents ONLY)                     │     │                 (Argolis Sandbox)                      │
├────────────────────────────────────────────────────────┤     ├────────────────────────────────────────────────────────┤
│ • FAQ article synthesis & rewriting                    │     │ • Agent Search retrieval queries                       │
│ • Theme regrouping & clustering                        │     │ • FaqResolverAgent live answer synthesis (Flash)       │
│ • Evaluation question generation (Sonnet/Flash)        │     │ • Live Tier-1 Laya endpoint predictions (:rawPredict)  │
│ • LLM judging (Opus claim verification / autorater)    │     │ • Live Tier-2 Flash fallback on uncertainty band       │
│ ❌ NEVER billed to Argolis project                      │     │ ✅ Permitted under monitored runtime quota              │
└────────────────────────────────────────────────────────┘     └────────────────────────────────────────────────────────┘
```

1. **Offline Work**: All offline experimentation, article generation, theme regrouping, question authoring, and evaluation judging MUST execute on Jetski agent models (e.g. Gemini 3.8 Flash high thinking), using **zero Vertex AI tokens** in the Argolis project (AGENTS.md Non-negotiable 11; PLAN.md §Billing decision). Agent-generated artifacts are persisted to disk as deterministic JSON or markdown data files so downstream pipelines remain reproducible without repeated model invocations.
2. **Runtime Operations**: The Argolis project quota is reserved exclusively for live system demonstrations and verified benchmarks: Agent Search queries, live answer generation by `FaqResolverAgent`, and endpoint serving.
3. **Token-Based Accounting**: Cost estimations and reported figures MUST be derived strictly from metered `usage_metadata` token counts (input tokens, output tokens, and thinking tokens). Thinking tokens are billed at full output token rates (docs/LESSONS.md L10). Uncalibrated heuristics (e.g. assuming $5 when real usage was $34) are strictly prohibited.
4. **Hermetic Test Isolation**: All automated tests in `tests/**` MUST execute offline without network, Vertex AI, or cloud credentials. Tests use mocks for `urllib.request.urlopen` and CLI flags (`--skip-judge`, `--reuse-questions`) to guarantee zero token consumption during development loops.

---

## 2. Requirement Index

| ID | Title | Status | Acceptance |
|---|---|---|---|
| `REQ-BILLING-001` | Offline LLM Operations on Jetski Agents Only | `done` | `tests/rag/test_clean_eval.py::test_clean_eval_skip_judge_flag` |
| `REQ-BILLING-002` | Reuse of Offline Evaluation Artifacts | `done` | `tests/rag/test_clean_eval.py::test_reuse_questions_file_loading` |
| `REQ-BILLING-003` | Token-Based Cost Measurement and Probe Requirement | `done` | `manual: cd training/router_s1 && .venv/bin/python pipeline/usage_probe.py` |
| `REQ-BILLING-004` | Permitted Runtime Argolis API Calls | `done` | `manual: python rag/search_test.py "Where is my order"` |
| `REQ-BILLING-005` | Agent Search Pricing Tier Optimization | `open` | `manual: ./scripts/tf_vm.sh logged-run single-project single_project_plan plan` |

---

## 3. Detailed Requirements

### REQ-BILLING-001: Offline LLM Operations on Jetski Agents Only
- **Statement**: All offline LLM development workloads—including FAQ article synthesis, theme re-clustering, synthetic test question generation, and LLM-as-a-judge evaluation—MUST run on Jetski agent instances, NEVER as Vertex AI API calls billed to the Argolis project.
- **Rationale**: Preserves Argolis project credits for live serving and prevents unexpected quota exhaustion during offline development (AGENTS.md Non-negotiable 11; PLAN.md §Billing decision).
- **Acceptance**: `tests/rag/test_clean_eval.py::test_clean_eval_skip_judge_flag`
- **Status**: done
- **Owner**: billing-and-models

### REQ-BILLING-002: Reuse of Offline Evaluation Artifacts
- **Statement**: Evaluation pipelines MUST provide flags (such as `--reuse-questions` and `--articles-from`) to ingest previously generated questions and articles from disk, bypassing repeated LLM generation calls.
- **Rationale**: Eliminates redundant token costs and ensures reproducible, deterministic benchmarking across runs (PLAN.md §Workstream A; rag/EVAL_RESULTS.md §8).
- **Acceptance**: `tests/rag/test_clean_eval.py::test_reuse_questions_file_loading`
- **Status**: done
- **Owner**: billing-and-models

### REQ-BILLING-003: Token-Based Cost Measurement and Probe Requirement
- **Statement**: Cost quotations and budget projections MUST be calculated directly from metered token usage extracted from API `usage_metadata` (input tokens, output tokens, and thinking tokens), treating thinking tokens as billed output tokens. Before initiating any batch labeling or generation run, an agent MUST execute an empirical probe (`usage_probe.py`) over ~5 real calls to measure exact token distributions.
- **Rationale**: Avoids severe 4–5× cost underestimation caused by unmetered thinking tokens (e.g. ~345 thinking tokens accounting for 85% of billed output) (AGENTS.md; docs/LESSONS.md L10; training/router_s1/README.md).
- **Acceptance**: `manual: cd training/router_s1 && .venv/bin/python pipeline/usage_probe.py`
- **Status**: done
- **Owner**: billing-and-models

### REQ-BILLING-004: Permitted Runtime Argolis API Calls
- **Statement**: API calls against the Argolis project `susmith-fde-ticket-router` MUST be restricted to runtime operations: Agent Search queries against `support-kb-search`, runtime answer synthesis by `FaqResolverAgent`, and live prediction requests against deployed router endpoints.
- **Rationale**: Defines the boundary between offline agent assistance and live cloud demo infrastructure (AGENTS.md Non-negotiable 11; PLAN.md §Billing decision).
- **Acceptance**: `manual: python rag/search_test.py "Where is my order"`
- **Status**: done
- **Owner**: billing-and-models

### REQ-BILLING-005: Agent Search Pricing Tier Optimization
- **Statement**: The Agent Search application `support-kb-search` SHOULD be evaluated for migration to the Standard search tier ($1.50 per 1,000 queries list price) without the Enterprise LLM add-on ($4.00 base + $4.00 add-on per 1,000 queries list price), subject to user decision and empirical retrieval verification.
- **Rationale**: Potential cost reduction opportunity identified by conductor, pending user decision and retrieval parity measurement (PLAN.md; rag/EVAL_RESULTS.md §7; list prices in rag/README.md).
- **Acceptance**: `manual: ./scripts/tf_vm.sh logged-run single-project single_project_plan plan`
- **Status**: open
- **Owner**: billing-and-models

---

## 4. Open Questions / Deltas

1. **Enterprise vs. Standard Search Tier Recommendation**: The search application `support-kb-search` was initially provisioned under the Enterprise tier with the LLM add-on enabled (estimate ~$8.00 / 1k queries based on list prices cited in `rag/README.md`). Because our multi-agent architecture performs answer synthesis and grounding directly inside `FaqResolverAgent` (using Gemini 3.8 Flash), the conductor recommended evaluating a downgrade to the Standard tier ($1.50 / 1k queries list price). This remains an open operational decision pending a user decision; the retrieval impact of the tier switch has not been tested.
2. **Quota Monitoring**: The Argolis sandbox currently lacks automated BigQuery billing exports or Budget Alerts API enablement. Usage and spend must be monitored via the Google Cloud Console Reports under identity `admin@susmith.altostrat.com`.

## Decision log

- 2026-10-25 (user decision): Laya serves BOTH intent routing (router-s1) and FAQ reranking (Laya v2 choice reranker) on **GPU** (L4/T4 Vertex endpoint), sharing the replicas.
  - CPU serving was rejected based on the measurement on n1-standard-8 (rag/results/v4/cpu/benchmark.md): router p50 913–1,334 ms against the 150 ms timeout, and int8 router agreement of only 88%.
  - LLM alternatives (e.g. gpt-6-luna, Gemini Flash-Lite) are **not** part of the comparison until their routing and reranking accuracy has been measured on our labeled sets. Cost estimates for them stay in the appendix only, labeled unmeasured.
