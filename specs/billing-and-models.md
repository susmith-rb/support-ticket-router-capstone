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
| `REQ-BILLING-005` | Agent Search Pricing Tier Optimization | `done` | `manual: ./scripts/tf_vm.sh logged-run single-project single_project_plan plan` |
| `REQ-BILL-MODELS-001` | Centralized Model Configuration Registry | `done` | `tests/test_no_hardcoded_models.py::test_model_id_resolves_all_standard_roles` |
| `REQ-BILL-MODELS-002` | Automated Hard-Coded Model Guard | `done` | `tests/test_no_hardcoded_models.py::test_no_hardcoded_model_literals_in_repo` |
| `REQ-BILL-MODELS-003` | Model Change & Upgrade Runbook | `done` | `tests/test_no_hardcoded_models.py::test_fallback_yaml_parser_accurately_parses_models_yaml` |

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
- **Evidence**: Migrated `support-kb-search` to Standard tier with no add-ons via in-place Terraform update on VM `laya-tune` (2026-10-26). Live REST verification confirms `searchEngineConfig = {"searchTier": "SEARCH_TIER_STANDARD"}`. DEV parity sweep (`dev_rows_standard.json`, n=218 MAIN) confirmed retrieval quality: recall@5 = 0.9128 (matches Enterprise 0.9128), MRR@10 = 0.7955 (vs Enterprise 0.7996), nDCG@5 = 0.7262 (vs Enterprise 0.7288, Δ = -0.0026). Zero request parameters rejected (`relevanceThreshold`, `boostSpec`, `summarySpec` all functional).
- **Status**: done
- **Owner**: billing-and-models

### REQ-BILL-MODELS-001: Centralized Model Configuration Registry
- **Statement**: All model endpoints, versions, locations, and default generation parameters (such as thinking budgets) MUST be centralized in `config/models.yaml` and loaded exclusively via `config.models.get(role)` or `config.models.model_id(role)`. The loader MUST support hierarchical configuration precedence: environment variable `MODEL_<ROLE_UPPER>` > environment-specific override block `envs[ENV]` > default role mapping, and MUST fail loudly (`KeyError`) on any unknown role lookup.
- **Rationale**: Eliminates brittle hard-coded model identifiers scattered across Python scripts, shell scripts, and evaluation pipelines, satisfying AGENTS.md Non-negotiable 2 ("model IDs live in config, never in code").
- **Acceptance**: `tests/test_no_hardcoded_models.py::test_model_id_resolves_all_standard_roles`
- **Status**: done
- **Owner**: billing-and-models

### REQ-BILL-MODELS-002: Automated Hard-Coded Model Guard
- **Statement**: The automated test suite MUST maintain a continuous verification guard (`tests/test_no_hardcoded_models.py`) that scans all executable code files (`*.py`, `*.sh`, `*.yaml`) outside allowed data, test, and config paths (`config/`, `tests/`, `rag/results/`, `rag/labels/`, `rag/corpus_v2/`, `docs/`, `.venv/`) for known model ID literals (e.g. `gemini-3.8-flash`, `claude-opus-5-5`, `semantic-ranker-512@latest`, etc.) and fails with exact `file:line` locations upon detecting any regression.
- **Rationale**: Prevents accidental re-introduction of hard-coded model strings during rapid iteration or agent multi-worker workflows.
- **Acceptance**: `tests/test_no_hardcoded_models.py::test_no_hardcoded_model_literals_in_repo`
- **Status**: done
- **Owner**: billing-and-models

### REQ-BILL-MODELS-003: Model Change & Upgrade Runbook
- **Statement**: Upgrading or swapping an active model (e.g. promoting a new Flash generation, updating an autorater, or testing an experimental ranking model) MUST adhere to a documented operational procedure: (1) update `config/models.yaml` (or set `MODEL_<ROLE>` for canary / environment overrides), (2) verify loader resolution via `python -m config.models <role>`, (3) run `tests/test_no_hardcoded_models.py` and regression test suites, (4) verify token usage and cost impact using `usage_probe.py`, and (5) record empirical quality deltas in evaluation reports before production deployment.
- **Rationale**: Standardizes model migration across multi-agent pipelines, avoiding unexpected breaking changes, cost blowups, or 404 endpoint denials across GCP locations.
- **Acceptance**: `tests/test_no_hardcoded_models.py::test_fallback_yaml_parser_accurately_parses_models_yaml`
- **Status**: done
- **Owner**: billing-and-models

---

## 4. Model Change & Upgrade Runbook

When upgrading an existing model version or testing an alternative architecture:

1. **Identify the Logical Role**:
   Check `config/models.yaml` for the relevant logical role (e.g. `answer`, `judge`, `teacher`, `evalsvc_autorater`, `ranking_api_model`).

2. **Canary or Local Testing via Environment Variables**:
   Test the change non-destructively in a local session without editing committed files:
   ```bash
   MODEL_ANSWER=gemini-next-flash python rag/clean_eval.py ...
   ```
   Or use environment overrides in `config/models.yaml` (`envs.dev`, `envs.prod`).

3. **Verify Configuration Resolution**:
   Run the CLI tool to verify resolution:
   ```bash
   python -m config.models answer
   python -m config.models --list
   ```

4. **Verify Endpoint Availability & Quota**:
   Before running large batches, verify that the project and location have access to the model:
   ```bash
   PROJECT=susmith-fde-ticket-router python training/router_s1/pipeline/usage_probe.py
   ```

5. **Run the Hard-Coded Model Guard**:
   Verify no literal model strings leaked into source code:
   ```bash
   .venv/bin/python -m pytest tests/test_no_hardcoded_models.py
   ```

6. **Execute Golden Evaluation Gate**:
   Verify retrieval and groundedness metrics meet minimum quality thresholds:
   ```bash
   python rag/ci_eval_gate.py --queries scratch/rag/eval/dev.jsonl ...
   ```

---

## 4. Open Questions / Deltas

1. **Enterprise vs. Standard Search Tier Recommendation**: The search application `support-kb-search` was initially provisioned under the Enterprise tier with the LLM add-on enabled (estimate ~$8.00 / 1k queries based on list prices cited in `rag/README.md`). Because our multi-agent architecture performs answer synthesis and grounding directly inside `FaqResolverAgent` (using Gemini 3.8 Flash), the conductor recommended evaluating a downgrade to the Standard tier ($1.50 / 1k queries list price). This remains an open operational decision pending a user decision; the retrieval impact of the tier switch has not been tested.
2. **Quota Monitoring**: The Argolis sandbox currently lacks automated BigQuery billing exports or Budget Alerts API enablement. Usage and spend must be monitored via the Google Cloud Console Reports under identity `admin@susmith.altostrat.com`.

## Decision log

- 2026-10-25 (user decision): Laya serves BOTH intent routing (router-s1) and FAQ reranking (Laya v2 choice reranker) on **GPU** (L4/T4 Vertex endpoint), sharing the replicas.
  - CPU serving was rejected based on the measurement on n1-standard-8 (rag/results/v4/cpu/benchmark.md): router p50 913–1,334 ms against the 150 ms timeout, and int8 router agreement of only 88%.
  - LLM alternatives (e.g. gpt-6-luna, Gemini Flash-Lite) are **not** part of the comparison until their routing and reranking accuracy has been measured on our labeled sets. Cost estimates for them stay in the appendix only, labeled unmeasured.
