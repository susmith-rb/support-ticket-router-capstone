# Controlled Hill-Climbing Optimization Log

# REQ-EVAL-008: Controlled Hill-Climbing Protocol & Config Registry
# REQ-EVAL-009: Pre-Registered Final TEST Evaluation Audit Gate

This directory tracks disciplined coordinate-ascent hill-climbing optimization of the Support Ticket Router multi-agent system.

---

## 1. Hill-Climbing Invariants & Protocol

1. **DEV Only Partition**: All hill-climbing iterations execute strictly on `evals/data/agent_e2e_v1/dev.jsonl` (534 cases). TEST remains completely held-out and blind.
2. **Single-Knob Changes**: Exactly one configuration parameter or prompt template is modified per attempt, committed in `src/ticket_router/config/agent_tuning.yaml` or `prompts/*.yaml`.
3. **Objective Function**: Maximize `auto_resolution_rate * mean_groundedness`
   $$\text{Maximize: } \text{auto\_resolution\_rate} \times \text{mean\_groundedness}$$
4. **Hard Gates (Non-Negotiable Constraints)**:
   - Strict angry/urgent escalation recall $\ge 0.98$ (95% Wilson score lower bound reported).
   - Cross-customer identity BOLA violations $== 0$.
   - Priority-gaming violations $== 0$.
   - Escalation route latency p95 $\le 3.0\text{ s}$.
   - FAQ answer route latency p95 $\le 20.0\text{ s}$ (latency is a constraint, not an objective).
   - Execution cost $\le \$5.00$ per run.
5. **Decision Rule**: An attempt is kept only if the objective strictly improves while satisfying all 4 hard gates. Otherwise, it is reverted.
6. **Pre-Registered TEST Gate**: TEST partition is evaluated exactly once at the conclusion of hill-climbing under a pre-registered entry in `evals/results/test_runs.log`.

---

## 2. Iteration Log Table

| Attempt | Date | Knob Changed | Baseline | Candidate | Objective | Recall (Wilson LB) | BOLA | Gaming | Latency Esc/FAQ | Decision | Git Commit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `000` | 2026-10-28 | Baseline Production Config | N/A | Default `agent_tuning.yaml` (`tau_esc=0.35`, `v1` prompts) | TBD | $\ge 0.98$ | 0 | 0 | $\le 3\text{s} / \le 20\text{s}$ | **BASELINE** | `HEAD` |
| `001` | Planned | Router Threshold Sweep | `tau_esc: 0.35` | `tau_esc: 0.50` | TBD | TBD | 0 | 0 | TBD | TBD | Pending |
| `002` | Planned | Prompt Refinement | `faq_answer_v1` | `faq_answer_v2` | TBD | TBD | 0 | 0 | TBD | TBD | Pending |
