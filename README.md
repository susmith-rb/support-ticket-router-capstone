# Support Ticket Router & Assistant (FDE Capstone)

A sentiment-aware support router for **Global Retail Hub**, a simulated international e-commerce
company. Angry and urgent tickets go straight to a human queue with a calming acknowledgement.
Factual questions get grounded answers with citations from Agent Search. Ticket state persists in
a relational store, and the system keeps working through GenAI quota outages (circuit breakers on
every dependency, no dropped tickets).

The centerpiece is **System-1 / System-2 tiered routing**:

| Tier | Component | What it decides | Budget |
| --- | --- | --- | --- |
| Ingress Guard | Model Armor (`support-router-lean` + Cloud SDP) + active-ticket hold | `C4` card block (`HTTP 400`), `C3` PII tokenization, PI/JB detection, follow-up hold | ~90–363 ms |
| 0 | Deterministic rules & regex | Hard safety/legal/abuse escalations (add-only), greetings/pleasantries, `[ORDER]` extraction | < 2 ms |
| 1 | Fine-tuned **Laya** typed-decision encoder (ModernBERT-large, `router-s1-dedicated`) | `escalate`, `sentiment`, `urgency`, `intent` with calibrated probabilities ($T=1.18$) | p95 < 100 ms (`77 ms` measured) |
| 2 | Gemini 3.8 Flash (`tier2_router_v5`, structured output, hedged) | Only the `0.15 <= p_esc < 0.50` uncertainty band, `intent=other`, OOS refusal (`reject_oos`) | ~1.7–3.8 s |
| 3 | Gemini 3.1 Pro Preview (`tier3_multihop`) | Narrow low-confidence (`< 0.70`) / multihop routing fallback + offline teacher labeling | <= 2.5 s |

The concise Technical Design Doc is in **[docs/TDD.md](docs/TDD.md)**, and the full 20-section reference design is in **[docs/TDD_DETAILED.md](docs/TDD_DETAILED.md)**. Component specifications and requirement traceability live in **[specs/](specs/README.md)**.

## Repository layout

| Path | What |
| --- | --- |
| `src/ticket_router/` | Core runtime package: ADK `TriageRouterAgent`, GKE `FaqResolverAgent`, shared `HumanHandoff`, `human-queue`, `human-review-ui`, MCP tool servers (`ticket-db-mcp`, `order-lookup-mcp`), SPIFFE mTLS + KMS CCT identity, Model Armor + KMS envelope encryption, and config |
| `evals/` | End-to-end agent eval (`agent_e2e.py`), routing cascade harness & 4-arm ablation (`evals/routing/`), live trace-attributed latency profiler (`evals/latency/`), calibrated `k=3` judge (`evals/judge/`), coordinate-ascent hill-climb log (`evals/hill_climb/`), and CI smoke gates |
| `chaos/` & `loadtest/` | Live 6-phase chaos game-day runner (`gemini_429_exhaustion`, `cloud_sql_restart`, `router_s1_outage` with guaranteed `finally:` restoration) and cost-guarded Locust load profiles |
| `corpus/` | FAQ knowledge corpus curation, article synthesis, theme regrouping, and idempotent `reconciliationMode=FULL` publishing |
| `experiments/` | Offline reranker experiments (Cross-Encoder, Laya v2 choice reranker, fusion, and CPU QINT8 benchmarking) |
| `training/router_s1/` | Reproducible Laya Tier-1 fine-tuning pipeline (prepare → teacher labels → dataset → train → evaluate), with its own [runbook](training/router_s1/README.md) |
| `rag/` | FAQ corpus data releases (`corpus_v2/`), multi-annotator labels (`labels/`), and frozen benchmark runs (`results/`) |
| `cicd/` | Declarative Cloud Build pipelines (change-aware CI with >=80% branch coverage gate, human-approval parallel CD with smoke & auto-rollback, corpus reindex, model serving) |
| `deploy/terraform/` | Terraform roots (`single-project/` 16+ modules and `agent-gateway/` IAP V2 + IAM v3 UAP `ENFORCE`), managed via `scripts/tf_vm.sh` |
| [specs/](specs/) | Normative domain specifications and automated bidirectional traceability matrix ([specs/README.md](specs/README.md)) |
| [docs/TDD.md](docs/TDD.md) | Concise Technical Design Doc (team review version): Executive Summary, Architecture, Components, Security, RAG, Evals/CI/CD, Observability |
| [docs/TDD_DETAILED.md](docs/TDD_DETAILED.md) | Full 20-section reference TDD: CUJs, architecture, routing design, security, reliability, TCO, eval gates, ADRs, runbooks |
| [AGENTS.md](AGENTS.md) | Rules for coding agents (Jetski/AGY) working in this repo |
| [docs/LESSONS.md](docs/LESSONS.md) | Lessons log: agent/harness mistakes and environment gotchas, fed back into `AGENTS.md` |

## Status

- [x] Dual TDD (`docs/TDD.md` + 20-section `docs/TDD_DETAILED.md`), OpenAPI contracts, ADRs, and operational runbooks
- [x] Argolis project, custom VPC + Cloud NAT, Cloud SQL (`ticket-store` with RLS + KMS envelope encryption), Pub/Sub + DLQ, Cloud Monitoring dashboards & alerts (Terraform)
- [x] Corpus prepared & indexed in `global` Agent Search (`support-kb-search`, Standard tier): `50` curated v2 FAQ articles + `81,103` PII-redacted `AmazonHelp` threads
- [x] Teacher labels (`6,446` Gemini 3.1 Pro 3x-vote threads + `1,323` graded reranker queries)
- [x] `router-s1` v2 + `laya-reranker-v2` fine-tuned, calibrated ($T=1.18$, intent `ECE = 0.0526`), and co-located on dedicated Vertex GPU endpoint (`router-s1-dedicated`, `77 ms` p95)
- [x] 3-runtime production deployment live: `TriageRouterAgent` on Vertex AI Agent Engine (`2170218524805431296`, SPIFFE Agent Identity + Memory Bank + Agent Gateway `tr-egress` `ENFORCE` + warm Cloud Run `triage-fallback`), `FaqResolverAgent` on GKE Autopilot (`ksa-faq-agent` SPIFFE), and `human-queue` / IAP `human-review-ui` / MCP servers on Cloud Run
- [x] Live end-to-end evaluations (`534` DEV hill-climb `5/5` hard gates PASS + single pre-registered `534` TEST run), 4-arm router ablation, 160-turn latency benchmark (`100%` decision match, `0%` errors), and 3/3 live chaos experiments verified

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
