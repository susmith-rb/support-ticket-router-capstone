# Agent Evaluation Scorecard: ❌ FAIL

> NOTE: Router golden labels are teacher/LLM consensus labels (human Review A pending).

## 1. Hard Safety & Quality Gates

| Gate / Invariant | Threshold | Measured Value | Gate Verdict |
|---|---|---|---|
| Strict Angry/Urgent Escalation Recall | >= 0.98 | **0.9368** (95% Wilson LB: 0.8690, n=95) | FAIL |
| Cross-Customer Identity BOLA Violations | == 0 | **0** | PASS |
| Priority-Gaming Violations | == 0 | **0** | PASS |
| Escalation Path Latency p95 | <= 3.0 s | **1.145 s** (p50: 0.873 s, max: 1.672 s, n=267) | PASS |
| FAQ Answer Path Latency p95 | <= 20.0 s | **7.058 s** (p50: 2.787 s, max: 19.814 s, n=267) | PASS |

### Hard Gate Failures:
- ⚠️ Escalation recall 0.937 < 0.98 (Wilson LB: 0.869)

## 2. Provisional Quality Targets (TDD §7.2, Reported)

| Metric | Target (TDD §7.2) | Measured | Notes |
|---|---|---|---|
| Intent Routing Accuracy | >= 0.85 Macro-F1 | **0.7733** | Exact intent match vs gold (alias-normalized) |
| Escalation Precision | reported | **0.3708** | True escalations / predicted escalations |
| Neutral False-Escalation Rate | <= 0.08 | **0.6427** | Neutral factual queries escalated |
| Factual Auto-Resolution Rate | >= 0.70 | **0.3573** | Factual queries auto-answered |
| Citation Validity Rate | >= 0.95 | **0.8955** | Answers with verbatim chunk quotes |
| Cited Article Hit@1 | reported | **0.5385** | Top cited doc matches C2 gold |
| Cited Article Hit@5 | reported | **0.7436** | Top-5 cited docs match C2 gold |
| Amortized Cost per Ticket | <= $0.0100 | **$0.00071** | Total run: $0.3792 (534 cases) |
