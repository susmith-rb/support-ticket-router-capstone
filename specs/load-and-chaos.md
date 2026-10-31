# Load Testing and Chaos Engineering Specification

Specifications for synthetic load testing with Locust and resilience validation via chaos engineering fault injection.
Implements TDD §5 (Locust load test: 33 msg/s sustained for 20 min, Black Friday spike) and TDD §5 / §10.4 (Chaos tests: undeploy router-s1, inject 429s, Cloud SQL failover).

---

## 1. Overview & Operational Safety Invariants

1. **Non-Production Testing Isolation**: Load tests and chaos experiments MUST NEVER execute against live customer production traffic without explicit operator authorization.
2. **Cost and Quota Safeguards**: Sustained (33 msg/s for 20 min ≈ 39,600 requests) and spike (~3x peak ramp for 5 min ≈ 20,000 requests) load profiles MUST strictly refuse execution unless `--i-understand-cost` or `I_UNDERSTAND_COST=1` is provided. The runner MUST estimate and print the request volume prior to execution.
3. **Safe Dry-Run by Default**: All chaos scripts MUST default to `--dry-run` and require both `--execute` and `--confirm-destructive` to execute state-mutating API calls against Google Cloud infrastructure.
4. **Guaranteed Restoration in Finally**: Every chaos experiment MUST restore initial operational state inside a Python `finally:` block, guaranteeing that failures during the probe phase never leave infrastructure degraded.

---

## 2. Requirements

### REQ-LOAD-001: Locust Load Testing Profiles and Cost/Quota Safeguards
- **Statement**: Synthetic load testing MUST drive the deployed TriageRouter on Vertex AI Agent Engine (`projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296`) using session creation and streaming queries with demo customer authentication tokens:
  1. **Message Mix**: Inputs MUST be drawn from existing evaluation datasets (`evals/latency/cases_v1.jsonl`, `evals/data/agent_e2e_v1/dev.jsonl`, `evals/data/adversarial_tone_v1.jsonl`) reflecting a realistic operational distribution: FAQ policy inquiries (~40%), authenticated/unauthenticated order status lookups (~30%), escalation and angry complaints (~20%), and greetings/pleasantries/out-of-scope inquiries (~10%), with zero new PII introduced.
  2. **Profiles**:
     - `smoke`: 1 msg/s for 2 minutes (120 messages).
     - `sustained`: 33 msg/s for 20 minutes (39,600 messages).
     - `spike`: ramp to ~3x (100 msg/s) over 5 minutes (~20,000 messages).
  3. **Cost Guard**: The runner MUST refuse to start `sustained` and `spike` profiles unless `--i-understand-cost` (or env `I_UNDERSTAND_COST=1` / `CONFIRM_LOAD_COST=1`) is set, and MUST print the estimated request count.
  4. **Metrics & Export**: Every request MUST capture client latency (ms), timestamp, case category, success status, decision (`auto_rag`, `escalate`, `reject_oos`), tier (`tier0_rules`, `tier1_router_s1`, `tier2_flash`), and assigned queue. Results MUST be exported to `requests.csv` and `summary.json` (reporting p50, p90, p95, p99, min, max, mean, error rate, throughput, and decision/tier breakdown) under `evals/results/load/<run_id>/`.
- **Rationale**: Validates TriageRouter throughput and latency SLAs under TDD §5 sustained volume (33 msg/s for 20 min) and peak shopping event spikes without unintended cloud spending or quota depletion.
- **Acceptance**: `tests/load/test_load_runner.py::test_load_profiles_and_cost_guard`
- **Status**: done
- **Owner**: load-and-chaos

### REQ-CHAOS-001: Chaos Engineering Fault Injection, Probing, and Safe Restoration
- **Statement**: Resilience validation MUST execute automated chaos experiments following a strict 6-phase lifecycle: (1) baseline measurement, (2) single-fault injection, (3) degraded probe, (4) assertion of documented degraded behavior per `REQ-AGENT-011`, (5) guaranteed restoration in a `finally` block, and (6) JSON report export under `evals/results/chaos/<name>_<timestamp>.json`:
  1. **Safe Defaults**: All chaos scripts MUST default to `--dry-run` and require `--execute` plus `--confirm-destructive` to mutate cloud resources.
  2. **Fault 1: `router-s1` Endpoint Outage**:
     - Fault: Undeploy model from endpoint `router-s1-dedicated` (or scale min replicas to 0) via Vertex AI endpoint API or `gcloud ai endpoints undeploy-model`.
     - Assertion: Inbound triage requests fall back to Tier 2 `gemini-3.8-flash` with degraded flag (`degraded=True` or fallback decision) per `REQ-AGENT-011`.
     - Restoration: Redeploy model to dedicated endpoint with original traffic split and replica configuration in `finally:`.
  3. **Fault 2: Gemini 429 Rate Limiting / Resource Exhaustion**:
     - Fault: Simulates Gemini API returning HTTP 429 / `RESOURCE_EXHAUSTED`.
     - Gap & Documented Hook: Because application source code in `src/ticket_router/` has no fault-injection hook and must not be edited during load/chaos testing, the experiment operates in dry-run mode and documents the needed code hook (`SIMULATE_GEMINI_429`).
     - Assertion: Probe confirms that after retries and deadline expiration (`REQ-AGENT-025`), the turn degrades to human escalation (`human-standard`, `degraded=True`) with a calming acknowledgement.
     - Restoration: Clear simulated fault state in `finally:`.
  4. **Fault 3: Cloud SQL Failure / Restart**:
     - Fault: Executes `gcloud sql instances restart ticket-store --project=susmith-fde-ticket-router`, documenting that failover is HA-only whereas the capstone database is ZONAL per Terraform.
     - Assertion: Probe confirms `ticket-db-mcp` fails fast with HTTP 503 / connection timeout, while `TriageRouterAgent` fails open per `REQ-AGENT-027` (ticket persistence retry then non-fatal warning, turn returns decision to user without crashing).
     - Restoration: Poll and wait for Cloud SQL instance state to return to `RUNNABLE` in `finally:`.
- **Rationale**: Validates distributed circuit breakers, hedged deadlines, and fallback hierarchies under real infrastructure outages (TDD §5, §10.4; AGENTS.md rule 5).
- **Acceptance**: `tests/chaos/test_chaos_experiments.py::test_chaos_experiments_lifecycle_and_restoration`
- **Status**: done
- **Owner**: load-and-chaos

---

## 3. Open Questions / Deltas

- **Gemini 429 Code Hook Gap**: `src/ticket_router/models/tier2_gemini.py` and `src/ticket_router/retrieval/answer_client.py` do not currently check an environment variable (such as `SIMULATE_GEMINI_429`) to trigger rate limit injection in live deployments. To conduct live 429 fault injection without editing `src/ticket_router/`, an operator would need to configure upstream Cloud Run / Vertex AI quotas or add the hook in a future core application change. The chaos script provides full dry-run simulation and reports the code hook requirements.
- **Cloud SQL Zonal Restart vs. Regional Failover**: `gcloud sql instances failover` requires a high-availability regional configuration (`availability_type = "REGIONAL"`). The capstone deployment uses `availability_type = "ZONAL"` for cost efficiency. The chaos script uses `gcloud sql instances restart ticket-store` as the zonal equivalent to simulate an abrupt database outage and recovery.
