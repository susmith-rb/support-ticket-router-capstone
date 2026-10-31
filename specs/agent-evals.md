# Specification: End-to-End Agent Evaluation & Hill-Climbing System

- **Specification Name**: End-to-End Agent Evaluation & Hill-Climbing System
- **Specification File**: `specs/agent-evals.md`
- **Applicable Areas**: E2E Agent Evaluation, Deterministic Metrics, Wilson Bounds, Offline LLM Judge, Hill-Climbing, CI Gates
- **Design Reference**: `docs/TDD.md` §7.1–§7.4, §6.4, §4.5, §8.1

---

## 1. Context & Architecture

This specification defines the end-to-end evaluation harness, deterministic safety/quality metrics, offline LLM-as-a-judge scoring, disciplined coordinate-ascent hill-climbing protocol, and CI smoke gates for the multi-agent Support Ticket Router system.

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│ E2E Dataset Builder (evals/build_agent_e2e.py)                                                  │
│   ├── RAG Retrieval Queries + C2 Gold Articles (rag/labels/retrieval_v1/)                       │
│   ├── Router Golden Set (300 cases stratified 150 DEV / 150 TEST by stratum; teacher labels)    │
│   ├── Customer Identity Cases (anon vs demo 1001/1002 order lookups; BOLA/IDOR boundaries)      │
│   └── Offline Adversarial Tone Set (evals/data/adversarial_tone_v1.jsonl; 40 seeds x 4 variants)│
│       └── Split BY SEED: 20 seeds DEV (80 cases) / 20 seeds TEST (80 cases; zero leakage)       │
└───────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                │
                                                ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Dual-Target Evaluation Runner (evals/agent_e2e.py)                                              │
│   ├── Target: local (in-process agents with hermetic stubs; zero vertexai/adk CI dependency)   │
│   └── Target: deployed (Agent Engine create_session(customer_id_token) + stream_query)          │
│   └── Concurrency limits, exponential retries, cost cap ($5.00/run) with pre-run abort check    │
└───────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                │ produces per-case traces (JSONL)
                                                ▼
┌───────────────────────────────────────────────┬─────────────────────────────────────────────────┐
│ Deterministic Metrics (evals/agent_metrics.py)│ LLM-as-a-Judge Harness                          │
│   • HARD GATES:                               │   • Export packets: evals/judge/packets_*.jsonl │
│     - Strict Escalation Recall >= 0.98        │   • Offline Jetski Subagents (Gemini Flash)     │
│       (with 95% Wilson score lower bound)     │   • Anchored 5/3/1 Rubrics (TDD §7.3):          │
│     - Identity BOLA Violations = 0            │     - Groundedness (windows, fees, eligibility) │
│     - Priority-Gaming Violations = 0          │     - Helpfulness & Empathy (<=120 words, steps)│
│     - Route Latency p95: Esc <= 3s, FAQ <= 20s│     - Calming Ack Tone (ticket ID, ETA, <=60w)  │
│   • PROVISIONAL TARGETS (TDD §7.2, non-gating)│   • Ingest scores: evals/agent_judge_ingest.py  │
│     - Routing acc, false-esc <= 0.08,         │   • 20-case anchor calibration (kappa >= 0.70)  │
│       auto-res >= 0.70, citation valid >= 0.95│   • Optional GEAP Eval Service on TEST (open)   │
│     - Amortized unit cost <= $0.01/ticket     │   • Scorecard notes teacher/LLM golden labels   │
└───────────────────────────────────────────────┴─────────────────────────────────────────────────┘
                                                │
                                                ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Hill-Climbing Protocol & Config Registry (src/ticket_router/config/agent_tuning.yaml)           │
│   • Knobs: router thresholds (tau_esc, tau_low, tau_intent), prompts/*.yaml, search & rerank    │
│   • Live agents read tuning config directly; defaults = current behavior (zero behavior change) │
│   • DEV only; single-knob coordinate ascent; objective: max auto_resolution x mean_groundedness │
│   • Subject to HARD GATES; latency reported per route (not an objective unless budget violated) │
│   • Audit log: evals/hill_climb/log.jsonl + README table; TEST evaluated ONCE under logged gate │
│   • CI Smoke Gate: 20-case deterministic subset behind change detection (evals/smoke_gate.py)   │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Requirements

### REQ-EVAL-001: Deterministic Multi-Source Dataset Construction & Stratification
- **Statement**: `evals/build_agent_e2e.py` MUST deterministically construct stratified evaluation datasets `evals/data/agent_e2e_v1/{dev,test}.jsonl` merging 4 verified sources: (1) RAG retrieval queries paired with C2 consensus gold articles (`rag/labels/retrieval_v1/labels_C2.jsonl`), (2) router golden set cases (`training/router_s1/results/v2/golden_all_model_outputs.csv`) stratified into 150 DEV and 150 TEST by pre-existing strata (`teacher-split / strict angry-urgent / rest-by-intent`) using deterministic seed 42 with teacher/LLM labels explicitly documented, (3) customer identity test cases (anonymous vs authenticated demo users 1001 and 1002 order queries), and (4) adversarial tone test cases.
- **Rationale**: TDD §7.1 and §7.2 mandate end-to-end evaluation covering routing, retrieval, deflection, customer identity, and safety.
- **Acceptance**: `tests/evals/test_agent_e2e_datasets.py::test_e2e_dataset_build_deterministic`
- **Status**: done
- **Owner**: agent-evals

### REQ-EVAL-002: Offline-Authored Adversarial Tone & Priority Gaming Set
- **Statement**: The evaluation suite MUST include `evals/data/adversarial_tone_v1.jsonl` comprising 40 core support scenario seeds expanded into 4 systematic tone variants: `calm`, `fake-urgent`, `angry`, and `abusive` (160 cases total). The dataset MUST be authored offline by Jetski agents (Gemini Flash high thinking, never Vertex AI billed to Argolis) with documented provenance in `evals/data/README.md`. Seeds MUST be partitioned strictly by seed into 20 seeds DEV (80 cases) and 20 seeds TEST (80 cases) where all 4 variants of a seed stay together. Anger and abuse MUST skip automation to human queues without raising internal priority; abusive queries MUST receive a calm boundary acknowledgment; safety threats MUST route to urgent human queue.
- **Rationale**: TDD §4.5, §7.1 CUJ 6, and AGENTS.md non-negotiable 11 require offline-authored gaming-resistant safety evaluation.
- **Acceptance**: `tests/evals/test_agent_e2e_datasets.py::test_adversarial_tone_set_properties`
- **Status**: done
- **Owner**: agent-evals

### REQ-EVAL-003: Leakage Prevention & DEV/TEST Partition Discipline
- **Statement**: Evaluation datasets MUST maintain zero leakage between DEV and TEST partitions: 0 thread_id overlap, 0 normalized query text collision, grouped adversarial seeds (all 4 variants of a seed in the same partition), and strict separation of retrieval queries and router golden sets. TEST sets MUST remain strictly held-out and blind during development and hill-climbing; evaluating against TEST is permitted only once under a pre-registered gate.
- **Rationale**: TDD §7.1 and specs/rag-retrieval.md require strict train/dev/test isolation to prevent Goodhart overfitting.
- **Acceptance**: `tests/evals/test_agent_e2e_datasets.py::test_dev_test_strict_isolation_no_leakage`
- **Status**: done
- **Owner**: agent-evals

### REQ-EVAL-004: Dual-Target Agent Evaluation Runner
- **Statement**: `evals/agent_e2e.py` MUST support dual execution targets: `local` (hermetic in-process agents with stubs/live local dependencies requiring zero vertexai/adk packages in CI [dev]) and `deployed` (remote Agent Engine using the canonical `create_session(state={"customer_id_token": ...})` + `stream_query` path). The runner MUST enforce configurable concurrency, exponential backoff retries for transient errors, a pre-run cost estimation printing `cases x est_cost`, and a configurable cost cap (default $5.00 per DEV run) with fail-safe pre-execution abort if the estimate exceeds the cap.
- **Rationale**: TDD §7.1 and REQ-AGENT-017 require validating identical behavior between local development and deployed Vertex Agent Engine runtimes with cost guardrails.
- **Acceptance**: `tests/evals/test_agent_e2e_runner.py::test_runner_dual_targets_and_cost_cap`
- **Status**: done
- **Owner**: agent-evals

### REQ-EVAL-005: Per-Case Trace Telemetry Schema
- **Statement**: Every evaluation execution MUST record structured per-case traces in JSONL format capturing: `case_id`, `input_message`, `route_decision`, `tiers_used` (`t0_rules`, `t1_router_s1`, `t2_gemini_flash`), `escalate` (bool), `priority` (`P1`-`P4`), `assigned_queue`, `ticket_id`, `answer_text`, `citations` (chunk IDs and titles), `customer_verified` (bool), per-stage latencies (routing ms, search ms, answer ms, total ms), route classification, and token usage by model mapped to dollar cost.
- **Rationale**: TDD §7.2, §8.1, and REQ-AGENT-014 require granular trace observability, stage breakdown, and unit cost tracking.
- **Acceptance**: `tests/evals/test_agent_e2e_runner.py::test_trace_telemetry_schema_and_cost`
- **Status**: done
- **Owner**: agent-evals

### REQ-EVAL-006: Deterministic Safety & Quality Metrics Engine
- **Statement**: `evals/agent_metrics.py` MUST compute automated evaluation metrics over trace runs and produce scorecard JSON and Markdown summaries. HARD GATES MUST enforce: (1) strict angry/urgent escalation recall >= 0.98 with reported 95% Wilson score lower bound, (2) identity BOLA violations = 0, (3) priority-gaming violations = 0, and (4) per-route latency budgets: escalation/human-ack path p95 <= 3.0 s and FAQ answer path p95 <= 20.0 s (REQ-AGENT-012). PROVISIONAL TARGETS (TDD §7.2, reported, non-gating) MUST measure: routing accuracy, neutral false-escalation rate (target <= 0.08), factual auto-resolution rate (target >= 0.70), citation validity rate (target >= 0.95), and amortized cost per ticket (target <= $0.01). The scorecard MUST explicitly disclose that router golden labels are teacher/LLM labels with human Review A pending.
- **Rationale**: TDD §7.2 and REQ-AGENT-012 establish quantitative gating criteria for safety, deflection, citation integrity, and route-specific latency budgets.
- **Acceptance**: `tests/evals/test_agent_metrics.py::test_metrics_calculation_and_wilson_score`
- **Status**: done
- **Owner**: agent-evals

### REQ-EVAL-007: Anchored 5/3/1 LLM Judge Rubric & Offline Subagent Evaluation
- **Statement**: Qualitative output assessment MUST implement the anchored 5/3/1 rubrics from TDD §7.3 for Groundedness, Helpfulness/Empathy, and Calming Ack Tone. The evaluation harness MUST export judge packets (`evals/judge/packets_<run_id>.jsonl`) for offline scoring by Jetski subagents (Gemini Flash, never Vertex AI billed to Argolis) and ingest scores via `evals/agent_judge_ingest.py`. The judge harness MUST support calibration against 20 pre-annotated anchor cases (calibration status reports 'pending' until independent judge subagent scores are ingested; calibration passes when Cohen's kappa >= 0.70). An optional one-time GEAP/Vertex Gen AI Evaluation Service run on final TEST is reserved as an open item requiring explicit user cost approval.
- **Rationale**: TDD §7.3 and AGENTS.md non-negotiable 11 require rigorous multi-dimensional qualitative grading without consuming Argolis Vertex quota.
- **Acceptance**: `tests/evals/test_agent_judge.py::test_judge_packet_export_and_ingest`
- **Status**: done
- **Owner**: agent-evals

### REQ-EVAL-008: Controlled Hill-Climbing Protocol & Config Registry
- **Statement**: System optimization MUST follow a controlled hill-climbing protocol using a centralized tuning configuration `src/ticket_router/config/agent_tuning.yaml` and versioned prompt templates in `prompts/*.yaml` (supporting both `template` and optional `system_instruction`). TriageRouter and FaqResolver agents MUST read tuning parameters from this registry (tau_esc=0.50, tau_low=0.15, tau_intent=0.70, `tier2_router_version: "v5"` frozen after DEV coordinate-ascent Attempt `001` from `"v4"` for Tier-2 scope-boundary refusal & angry fulfillment/checkout blocker escalation per `REQ-AGENT-022`, `faq_answer_version: "v2"`, standard retrieval kwargs; legacy `v1` templates preserved byte-for-byte in `prompts/*_v1.yaml`). Optimization runs MUST operate on DEV only, modifying exactly one knob per attempt (thresholds, prompt versions, search kwargs, reranker on/off, top-k, citation strictness). Attempts MUST seek to maximize auto_resolution x mean_groundedness subject to HARD GATES (recall >= 0.98, BOLA = 0, gaming = 0, route latencies <= budgets, cost <= $5.00). Per-route latency is reported as a constraint check, not an optimization objective. Every attempt MUST append to `evals/hill_climb/log.jsonl` and update `evals/hill_climb/README.md`.
- **Rationale**: TDD §6.4 and §7.2 require disciplined coordinate-ascent tuning with transparent audit trails and live agent configuration coupling.
- **Acceptance**: `tests/evals/test_agent_tuning_config.py::test_agent_tuning_config_and_hill_climb_invariants`
- **Status**: done
- **Owner**: agent-evals

### REQ-EVAL-009: Pre-Registered Final TEST Evaluation Audit Gate
- **Statement**: The TEST dataset MUST be evaluated exactly once after hill-climbing freezes. The evaluation run MUST be pre-registered by appending an immutable declaration entry to `evals/results/test_runs.log` adhering to the audit format in `rag/results/v4/test_runs.log` (timestamp, commit SHA, frozen tuning config hash, dataset path, sample size, operator). Unregistered runs or multiple TEST attempts are strictly prohibited.
- **Rationale**: TDD §7.1 and specs/rag-retrieval.md audit discipline guarantee that reported capstone test results are unbiased and uncompromised by post-hoc cherry-picking.
- **Acceptance**: `tests/evals/test_agent_tuning_config.py::test_pre_registered_test_gate_format`
- **Status**: done
- **Owner**: agent-evals

### REQ-EVAL-010: Hermetic CI Smoke Evaluation Gate
- **Statement**: Continuous integration MUST execute a deterministic 20-case smoke evaluation gate (`evals/smoke_gate.py`) triggered on changes touching router policies, retrieval configs, prompts, or evaluation logic. The smoke gate MUST execute locally in under 15 seconds with zero external network or GCP API calls, asserting 100% routing invariant preservation, 0 BOLA violations, and 0 priority-gaming regressions.
- **Rationale**: TDD §7.4 and specs/ci-cd.md mandate fast, hermetic PR blocking gates preventing safety regressions from merging to main.
- **Acceptance**: `tests/evals/test_agent_smoke_gate.py::test_smoke_subset_deterministic_gate`
- **Status**: done
- **Owner**: agent-evals

### REQ-EVAL-011: Live Per-Path Latency Benchmark with Trace-Level Step Attribution
- **Statement**: The repository MUST provide a live latency benchmark (`python -m evals.latency`) that measures the deployed TriageRouter Agent Engine end to end over the exact Gemini Enterprise wire protocol and attributes latency to individual pipeline steps:
  1. **Case set** (`evals/latency/cases_v1.jsonl`, ~30 cases): each case has `case_id`, `path` (one of `greeting`, `pleasantry`, `policy_faq`, `order_list_auth`, `order_id_auth`, `order_anon`, `out_of_scope`, `escalation`), `message`, `oauth` (bool), and `expected_decision`; at least 3 cases per path.
  2. **Execution**: for every request, create a fresh ADK session (`class_method: "async_create_session"` with a unique opaque `latbench-*` user id), then call `:streamQuery` with `class_method: "streaming_agent_run_with_events"` and `request_json` carrying `message`, `user_id`, `session_id`, and (when `oauth=true`) `authorizations.support-customer-oauth.access_token` set to the operator's short-lived gcloud access token. Requests run sequentially (`--reps`, default 5) with configurable inter-request spacing; the first request of each path is flagged `maybe_cold=true`. A `--dry-run` mode prints the plan and request count with zero network calls, and `--max-requests` (default 200) aborts before exceeding the cap. Client metrics per request: time-to-first-event (`ttfb_ms`), total wall time (`client_total_ms`), HTTP status, streamed error `code`/`errorMessage` if any, decision text length, and a SHA-256 of the answer (no raw answer text is persisted beyond a 160-character preview).
  3. **Trace attribution**: after the run, correlate each `session_id` to the TriageRouter structured decision log in Cloud Logging (`resource.type="aiplatform.googleapis.com/ReasoningEngine"`, JSON embedded in `textPayload`) to obtain `traceparent`, `tier`, `decision`, `predicted_intent`, and server `latency_ms`; then fetch the trace by ID (`GET cloudtrace v1 projects/{p}/traces/{traceId}`, never bulk `traces.list`, with exponential backoff on HTTP 429). Step durations MUST be derived for `triage_router_turn`, `mcp.ticket_db.resolve_customer`, `mcp.ticket_db.upsert_ticket`, routing (`upsert_ticket.start - turn.start - resolve_customer`), `a2a.handoff.faq_resolver`, `faq_resolver.resolve`, `faq_resolver.list_orders` / `mcp.order_lookup.*`, `faq_resolver.search`, `faq_resolver.rerank`, `faq_resolver.generate_answer`, plus derived `agent_engine_overhead_ms = client_total_ms - turn_ms` and `a2a_overhead_ms = a2a.handoff - faq_resolver.resolve`.
  4. **Outputs**: an immutable run directory `evals/results/latency/run_NNN[_label]/` containing `requests.jsonl` (one row per request, PII-free), `summary.json`, and `report.md` with per-path p50/p95/max for client total, TTFB, server turn, and every step; tier and decision distribution per path; decision-match rate vs `expected_decision`; error rate with error codes; and cold (`maybe_cold`) vs warm splits. Tokens MUST never be logged or written to disk.
  5. **Testing**: all logic (planning, wire payload construction, stream parsing incl. error events, log parsing, span aggregation, percentile math, report rendering) MUST be covered by hermetic unit tests using injected fake transports with zero network access.
- **Rationale**: Hill-climbing latency knobs requires a trustworthy per-path, per-step baseline first; trace-level attribution distinguishes model time (Tier 2, answer synthesis) from serial I/O, cold starts, and platform overhead, so each optimization can be measured before/after with the same instrument (decision 2026-10-29).
- **Acceptance**: `tests/evals/test_latency_bench.py::test_latency_bench_attribution_and_report`
- **Status**: done
- **Owner**: agent-evals

---

## 3. Open Questions / Deltas

1. **Vertex Gen AI Evaluation Service on Final TEST**: An optional one-time run of the managed Vertex Gen AI Evaluation Service on the final TEST evaluation report is specified as an open item requiring explicit user cost approval before execution (to preserve Argolis budget).
2. **Golden Set Teacher vs Human Labels**: The router golden set currently uses verified cross-family model consensus (Gemini 3.1 Pro + Claude Sonnet/Opus) as teacher labels in `golden_all_model_outputs.csv`; human Review A completion is pending.
