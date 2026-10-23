# Support Ticket Router & Assistant (FDE Capstone)

A sentiment-aware support router for **Global Retail Hub**, a simulated international e-commerce
company. Angry and urgent tickets go straight to a human queue with a calming acknowledgement.
Factual questions get grounded answers with citations from Agent Search. Ticket state persists in
a relational store, and the system keeps working through GenAI quota outages (circuit breakers on
every dependency, no dropped tickets).

The centerpiece is **System-1 / System-2 tiered routing**:

| Tier | Component | What it decides | Budget |
| --- | --- | --- | --- |
| 0 | Model Armor (Agent Gateway) + deterministic rules | PII redaction, injection screening, hard escalations (add-only) | ~ms |
| 1 | Fine-tuned **Laya** typed-decision encoder (ModernBERT-large, `router-s1`) | `escalate`, `sentiment`, `urgency`, `intent` with calibrated probabilities | p95 < 100 ms |
| 2 | Gemini Flash (structured output) | Only the uncertainty band, `intent=other`, entity gaps | ~1–2 s |
| 3 | Gemini Flash with thinking / Pro (hill-climb candidate) | Multi-hop answer synthesis only | seconds |

The full design is in the spec: **[docs/TDD.md](docs/TDD.md)**.

## Repository layout

| Path | What |
| --- | --- |
| [docs/TDD.md](docs/TDD.md) | Technical Design Doc (the spec): CUJs, architecture, routing design, security, reliability, eval gates, ADRs, runbooks |
| [AGENTS.md](AGENTS.md) | Rules for coding agents (Jetski/AGY) working in this repo |
| [docs/LESSONS.md](docs/LESSONS.md) | Lessons log: agent/harness mistakes and environment gotchas, fed back into `AGENTS.md` |
| [training/router_s1/](training/router_s1/) | Reproducible Laya Tier-1 fine-tuning pipeline (prepare → teacher labels → dataset → train → evaluate), with its own [runbook](training/router_s1/README.md) |
| `agents/`, `mcp/`, `deploy/terraform/` | *Planned:* ADK agents (Agent Runtime + Cloud Run fallback), MCP tool servers, Terraform modules (TDD §5, §7, §12.7) |

## Status

- [x] TDD drafted (`docs/TDD.md`)
- [x] Argolis project, VPC/NAT, GPU Workbench VM (org-policy compliant)
- [x] Corpus prepared: Kaggle *Customer Support on Twitter*, AmazonHelp slice = **81,103** customer-opened threads, PII-redacted
- [ ] Teacher labels (Gemini 3.1 Pro, 2k pilot running)
- [ ] `router-s1` v1 fine-tune + eval
- [ ] Agents, MCP tools, Agent Search corpus, Terraform, CI/CD

## Data

Training data comes only from the public Kaggle dataset
[Customer Support on Twitter](https://www.kaggle.com/datasets/thoughtvector/customer-support-on-twitter)
(customers are pseudonymized numeric IDs; residual PII is redacted by `prepare_twcs.py`).
No raw or processed data is committed. It lives in the project's GCS bucket (see the training runbook).

## Running tests

Run the full offline unit test suite:

```bash
# via pytest directly in .venv
.venv/bin/python -m pytest

# or via the helper script
./scripts/test.sh

# or via make
make test
```

All unit tests run offline without network access or cloud credentials.
