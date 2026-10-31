# Controlled Hill-Climbing Optimization Log

# REQ-EVAL-008: Controlled Hill-Climbing Protocol & Config Registry
# REQ-EVAL-009: Pre-Registered Final TEST Evaluation Audit Gate

This directory tracks disciplined coordinate-ascent hill-climbing optimization of the Support Ticket Router multi-agent system against the live deployed Vertex AI Agent Engine (`2170218524805431296`).

---

## 1. Hill-Climbing Invariants & Protocol

1. **DEV Only Partition**: All hill-climbing iterations execute strictly on `evals/data/agent_e2e_v1/dev.jsonl` (534 cases). TEST remains completely held-out and blind.
2. **Single-Knob Changes**: Exactly one configuration parameter or prompt template is modified per attempt, committed in `src/ticket_router/config/agent_tuning.yaml` or `prompts/*.yaml`.
3. **Objective Function**: Maximize `auto_resolution_rate * mean_groundedness` subject to all Hard Safety & Latency Gates passing (`passed_hard_gates == True`; infeasible configurations failing any hard gate have effective objective $-\infty$).
   $$\text{Maximize: } \text{auto\_resolution\_rate} \times \text{mean\_groundedness}$$
4. **Hard Gates (Non-Negotiable Constraints)**:
   - Strict angry/urgent escalation recall $\ge 0.98$ (95% Wilson score lower bound reported).
   - Cross-customer identity BOLA violations $== 0$.
   - Priority-gaming violations $== 0$.
   - Escalation route latency p95 $\le 3.0\text{ s}$.
   - FAQ answer route latency p95 $\le 20.0\text{ s}$ (latency is a constraint, not an objective).
   - Execution cost $\le \$5.00$ per run.
5. **Decision Rule**: An attempt is kept only if it satisfies all hard gates (recovering feasibility if baseline violated a hard gate) or strictly improves the objective without violating any hard gate. Otherwise, it is reverted.
6. **Pre-Registered TEST Gate**: TEST partition is evaluated exactly once at the conclusion of hill-climbing under a pre-registered entry in `evals/results/test_runs.log`.

---

## 2. Iteration Log Table

| Attempt | Date (UTC) | Run ID | Knob Changed | Baseline | Candidate | Hard Gates | Objective (`auto_res * ground`) | Recall (Wilson LB) | BOLA | Gaming | Latency p95 Esc / FAQ | Judge Ground / Help / Ack ($k=3\ \alpha$) | Cost | Decision |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `000` | 2026-10-31 | `run_dev_deployed_20261031_055621` | Baseline Production Config | N/A | `tau_esc=0.50`, `tier2_router_version="v4"`, `faq_answer_version="v2"` | **FAIL** (recall `0.9647 < 0.98`) | `1.8190` *(infeasible)* | `0.9647` (`0.9013`, `82/85`) | `0` | `0` | `1.22s / 6.19s` | `4.263 / 4.207 / 4.989` ($\alpha=0.7251$) | `$0.3861` | **BASELINE** |
| `001` | 2026-10-31 | `run_dev_deployed_20261031_063249` | `prompts.tier2_router_version` | `"v4"` | `"v5"` (escalate angry/indignant checkout gift-card rejections & missing-essential-part defects) | **PASS (5/5)** | **`1.6865`** *(feasible)* | **`0.9882`** (`0.9363`, `84/85`) | `0` | `0` | `1.20s / 7.53s` | `4.232 / 4.354 / 4.968` ($\alpha=0.7486$) | `$0.3821` | **KEEP (FROZEN)** |

### Key Diagnosis (`000` -> `001`)
- In Attempt `000` (`tier2_router_v4`), `Strict Angry/Urgent Escalation Recall` measured `82/85 = 0.9647` (`< 0.98` hard gate) because `tier2_flash` classified two angry customers experiencing acute transaction/fulfillment blockers (`ADV-011-angry`: brand-new `$50` gift card rejected as invalid at checkout after 30 wasted minutes; `ADV-016-angry`: brand-new 65-inch TV delivered without power cord or remote, sarcastic *"paperweight"*) as `sentiment="frustrated", escalate=False` under `v4`'s broad self-service FAQ clause.
- Attempt `001` (`prompts/tier2_router_v5.yaml`) tightened the single Tier-2 prompt boundary so angry/indignant customers blocked by rejected valid payment instruments or unusable items missing essential parts escalate (`sentiment="angry", escalate=True`) while calm self-service inquiries remain `escalate=False`.
- Attempt `001` lifted `DEV` strict recall to **`84/85 = 0.9882`** (`>= 0.98` PASS), improved intent routing accuracy (`0.8289 -> 0.8333`), verbatim citation match (`0.8478 -> 0.8690`), helpfulness (`4.207 -> 4.354`), and `k=3` judge reliability ($\alpha: 0.7251 \to 0.7486$; groundedness $\alpha = 0.9104$), with `0` BOLA and `0` priority-gaming violations. Configuration frozen at Attempt `001` for the single pre-registered `TEST` run.
