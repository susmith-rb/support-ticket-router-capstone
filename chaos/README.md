# Chaos Engineering & Resilience Validation Suite

# REQ-CHAOS-001: Chaos Engineering Fault Injection, Probing, and Safe Restoration
Resilience and fault-injection validation for TriageRouter and supporting microservices, implementing TDD §5 / §10.4 and verifying fallback hierarchies under `REQ-AGENT-011` and `REQ-AGENT-027`.

---

## 1. Safety Guardrails & Operational Invariants

1. **Safe Dry-Run by Default**: All scripts default to `--dry-run` simulation. No network requests or state changes occur without explicit operator intent.
2. **Mandatory Confirmation for Live Execution**: To run against Google Cloud infrastructure, the operator MUST provide BOTH `--execute` AND `--confirm-destructive`. Omitting either flag halts execution immediately before any cloud calls.
3. **Guaranteed Restoration in Finally**: Every experiment executes infrastructure restoration inside a Python `finally:` block. Even if the probe or assertion raises an unhandled exception or aborts, initial state is restored.
4. **Structured JSON Telemetry**: Every experiment run persists a structured report under `evals/results/chaos/<name>_<timestamp>.json` detailing baseline measurements, fault injection steps, probe latency/decisions, assertion outcomes, and post-restoration health.

---

## 2. Experiments & Expected Degraded Behaviors

### 1. `router_s1_outage` (router-s1 Dedicated Endpoint Outage)
- **Target**: `projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1-dedicated`
- **Fault**: Undeploys the model or scales active replicas to 0 via `gcloud ai endpoints undeploy-model`.
- **Expected Degraded Behavior (`REQ-AGENT-011`)**:
  - In-memory circuit breaker opens after 3 consecutive failures.
  - TriageRouter falls back to Tier-2 `gemini-3.8-flash` with `degraded=True` or fallback decision.
  - Customer turn succeeds without returning a 500 error.
- **Restoration**: Redeploys the model back to the dedicated endpoint with original traffic split (`0=100`) and min/max replicas.

### 2. `gemini_429_exhaustion` (Gemini API 429 Quota Exhaustion)
- **Target**: Gemini 3.8 Flash (`router_tier2`, `router_tier3`, and `answer` generation)
- **Live Fault Injection Hook (`CHAOS_SIMULATE_GEMINI_429=1`)**:
  - Call-time environment gate `check_gemini_429_hook()` (`src/ticket_router/models/tier2_gemini.py`) wired into `TriageRouterAgent` (`invoke_tier2_gemini`, `invoke_tier3_gemini`) and `FaqResolverAgent` (`GeminiAnswerClient.generate_answer`).
  - Defaults off (`0` / unset); when `CHAOS_SIMULATE_GEMINI_429=1` is set in `--execute --confirm-destructive` mode, every Gemini invocation raises `google.genai.errors.APIError(429, {"error": {"code": 429, "message": "Resource Exhausted: Quota exceeded", "status": "RESOURCE_EXHAUSTED"}})`.
- **Expected Degraded Behavior (`REQ-AGENT-011`, `REQ-AGENT-025`)**:
  - Hedged call initiates attempt 1, attempts retry/hedge, and exhausts retries/deadline.
  - TriageRouter degrades to human escalation (`human-standard`, `degraded=True`) without raising an unhandled 500 error.
  - Sends a deterministic calming acknowledgement with SLA expectations to the user.
- **Restoration**: Clears `CHAOS_SIMULATE_GEMINI_429` in `finally:` and verifies healthy post-restoration state.

### 3. `cloud_sql_restart` (Cloud SQL Zonal Database Outage)
- **Target**: `ticket-store` in `susmith-fde-ticket-router` (PostgreSQL 16)
- **HA Failover vs. Zonal Restart**:
  - `gcloud sql instances failover` is restricted by GCP to High-Availability (REGIONAL) instances.
  - The capstone database is provisioned as `availability_type = "ZONAL"` (`deploy/terraform/single-project/modules/cloud-sql/main.tf`) for cost optimization.
  - Therefore, `gcloud sql instances restart ticket-store` is used as the zonal fault equivalent.
- **Expected Degraded Behavior (`REQ-AGENT-027`)**:
  - `ticket-db-mcp` on Cloud Run fails fast with HTTP 503 / connection timeout.
  - TriageRouterAgent retries once, logs a non-fatal `ticket_upsert_failed` warning, and **fails open**, returning the decision/answer to the user without failing the turn.
- **Restoration**: Polls `gcloud sql instances describe ticket-store` until instance status returns to `RUNNABLE`.

---

## 3. Execution Commands

### Dry-Run Simulation (Safe & Hermetic)

```bash
# Run all chaos experiments in dry-run mode:
python3 chaos/run_all.py

# Run specific experiments in dry-run mode:
python3 chaos/run_all.py --experiment router_s1
python3 chaos/run_all.py --experiment gemini_429
python3 chaos/run_all.py --experiment cloud_sql
```

### Live Execution Against Google Cloud (Operator Only)

```bash
# Live router-s1 undeploy/restore test:
python3 chaos/run_all.py --experiment router_s1 --execute --confirm-destructive

# Live Cloud SQL zonal restart test:
python3 chaos/run_all.py --experiment cloud_sql --execute --confirm-destructive
```
