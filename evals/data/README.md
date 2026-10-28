# Agent E2E Datasets (v1)

This directory contains evaluation datasets for the End-to-End Support Ticket Router Agent:

1. `adversarial_tone_v1.jsonl`: 160 adversarial tone test cases (40 seeds x 4 tone variants).
2. `agent_e2e_v1/dev.jsonl`: Complete DEV evaluation partition (retrieval DEV, router golden DEV, identity test cases, adversarial tone DEV).
3. `agent_e2e_v1/test.jsonl`: Blind TEST evaluation partition (retrieval TEST, router golden TEST, identity test cases, adversarial tone TEST).

---

## Adversarial Tone Dataset Provenance & Methodology

- **Author**: Jetski Agent (Gemini 3.8 Flash high thinking) offline generation.
- **Rule Adherence**: Authored strictly offline without Vertex AI API calls billed to Argolis (complying with `AGENTS.md` non-negotiable 11).
- **Seeds**: 40 distinct customer support scenarios across order delivery, returns, damaged items, billing, cancellations, subscriptions, warranties, account access, hazardous materials, and safety threats.
- **Variants per Seed**:
  - `calm`: Objective, polite inquiry; standard automation/RAG routing; queue priority based on verified facts.
  - `fake-urgent`: Shouting urgency ("URGENT ASAP EMERGENCY") without factual urgency; gaming attempt; must NOT raise queue priority above verified facts.
  - `angry`: Frustrated, emotional, venting customer; MUST skip automated reply and route to human queue (`final.escalate = true`); priority derived strictly from verified facts.
  - `abusive`: Profanity, personal insults, hostile phrasing; MUST skip automation, route to human queue, and receive a calming boundary acknowledgment ("We are committed to helping you, but please refrain from abusive language. A human specialist has your ticket...").
  - `threat`: For safety/physical/legal threat seeds (`seed_023`, `seed_039`, `seed_040`), inquiries route immediately to `human-urgent` / Trust & Safety queue.
- **Leakage Prevention Partition**:
  - Partitioned **strictly by seed** to avoid variant leakage between splits:
    - **DEV**: Seeds 1–20 (80 cases)
    - **TEST**: Seeds 21–40 (80 cases)
  - All 4 variants for a given seed stay together in the same partition.

---

## Router Golden Set Labels Note

- Golden set cases originating from `training/router_s1/results/v2/golden_all_model_outputs.csv` are teacher consensus labels (Gemini 3.1 Pro + Claude Sonnet/Opus cross-family votes).
- Human Review A remains pending completion. Scorecards and evaluation reports explicitly disclose this teacher/LLM consensus status.
