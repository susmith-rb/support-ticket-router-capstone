# TriageRouter Synthetic Load Testing with Locust

# REQ-LOAD-001: Locust Load Testing Profiles and Cost/Quota Safeguards
Validates TriageRouter on Vertex AI Agent Engine under sustained traffic (33 msg/s for 20 min) and peak shopping event spikes (Black Friday).

---

## 1. Profiles & Operational Safeguards

| Profile | Target Throughput | Duration | Total Requests | Quota / Cost Guard | Description |
|---|---|---|---|---|---|
| `smoke` | 1.0 msg/s | 2 min (120 s) | ~120 | Automatic (Allowed) | Lightweight validation of connectivity, session creation, and routing latency. |
| `sustained` | 33.0 msg/s | 20 min (1,200 s) | ~39,600 | **Required** (`--i-understand-cost`) | Validates TDD §5 steady production target throughput across Agent Engine, Cloud Run, and GKE. |
| `spike` | 33 $\to$ 100 msg/s | 5 min (300 s) | ~20,000 | **Required** (`--i-understand-cost`) | Black Friday traffic simulation ramping to ~3x baseline (100 msg/s peak) for 2 minutes before recovery. |

### Cost & Quota Guard
Executing `sustained` or `spike` sends tens of thousands of requests to Vertex AI Reasoning Engine, Cloud Run, and Gemini Enterprise. To prevent unintended quota exhaustion or cloud billing spikes, the runner **strictly refuses to run** unless explicit confirmation is provided:
- CLI Flag: `--i-understand-cost`
- Environment Variable: `export I_UNDERSTAND_COST=1`

If invoked without confirmation, the runner aborts before dialing any network endpoints and prints the estimated request count.

---

## 2. Realistic Message Mix (Zero New PII)

Traffic is synthesized using weighted sampling across existing repository evaluation datasets (`evals/latency/cases_v1.jsonl`, `evals/data/agent_e2e_v1/dev.jsonl`, `evals/data/adversarial_tone_v1.jsonl`):
- **Policy FAQ (40%)**: Return policies, shipping times, warranty, and account questions.
- **Order Inquiries (30%)**:
  - Authenticated (20%): Demo customer accounts (1001/1002) with ID tokens minted via `scripts/seed_identity_test_users.py`.
  - Unauthenticated (10%): Anonymous order inquiries prompting sign-in.
- **Escalation & Complaints (20%)**: Angry tone, late delivery complaints, and human agent requests.
- **Conversational & Out of Scope (10%)**: Greetings ("hey", "hello there") and out-of-scope inquiries.

---

## 3. Quickstart & Execution Commands

### Standalone Python Runner (Recommended)

```bash
# 1. Run quick dry-run smoke test (no cloud calls, fast verification):
python3 loadtest/runner.py --profile smoke --dry-run

# 2. Run live smoke test against deployed Agent Engine:
python3 loadtest/runner.py --profile smoke

# 3. Run sustained 33 msg/s load test (requires cost confirmation):
python3 loadtest/runner.py --profile sustained --i-understand-cost

# 4. Run Black Friday spike test (requires cost confirmation):
python3 loadtest/runner.py --profile spike --i-understand-cost
```

### Locust CLI Execution

Install locust (`pip install -e ".[load]"` or `pip install locust>=2.24.0`):

```bash
# Headless smoke test:
locust -f loadtest/locustfile.py --headless --profile=smoke

# Headless sustained test with cost acknowledgment:
locust -f loadtest/locustfile.py --headless --profile=sustained --i-understand-cost

# Interactive Web UI (open http://localhost:8089):
locust -f loadtest/locustfile.py
```

---

## 4. Output Artifacts

Each run outputs monotonic artifact directories under `evals/results/load/<run_id>/`:
1. `requests.csv`: Per-request record with columns:
   `timestamp_iso,request_idx,case_id,category,session_id,latency_ms,success,decision,tier,queue,error`
2. `summary.json`: Comprehensive performance metrics:
   - Request counts and error rates (`total_requests`, `successful_requests`, `failed_requests`, `error_rate_pct`)
   - Actual achieved throughput (`actual_qps`)
   - Latency percentiles (`p50`, `p90`, `p95`, `p99`, `min`, `max`, `mean`, `stddev`)
   - Routing decision distribution (`auto_rag`, `escalate`, `reject_oos`)
   - Execution tier breakdown (`tier0_rules`, `tier1_router_s1`, `tier2_flash`)
   - Category-level p50/p95 latency attribution
