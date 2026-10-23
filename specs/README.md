# Spec-Driven Development Process

This repository enforces **Spec-Driven Development (SDD)**. Specifications live directly in the codebase under `specs/`. They are the single source of truth for architectural requirements, functional invariants, and verification criteria. All human engineers and AI coding agents read, implement, and maintain these specifications.

---

## 1. Core Principles

1. **Spec-First Lifecycle**: Every change begins by updating the relevant specification in `specs/`. No code or test changes are accepted without a corresponding spec update. The spec, tests, and implementation MUST be updated in the **same commit**.
2. **Deterministic Traceability**: Every requirement has a unique identifier (e.g. `REQ-RAG-CORPUS-001`). Automated tests explicitly cite requirement IDs in code comments (`# REQ-...`), and an offline test suite (`tests/test_spec_traceability.py`) enforces 100% traceability for all `done` requirements.
3. **Executable Acceptance**: Requirements define unambiguous, executable acceptance checks: either an exact `pytest` test node ID (e.g. `tests/rag/test_publish_faq.py::test_sha`) or an executable CLI command. Non-automated operational checks must be explicitly prefixed with `manual:`.
4. **Honest Deltas**: When code, environment, or the design doc (`docs/TDD.md`) disagree, agents MUST NOT paper over the divergence or unilaterally change design decisions. The discrepancy MUST be recorded in the spec under **Open Questions / Deltas**.

---

## 2. Requirement Structure

Every requirement block in `specs/<area>.md` follows this exact structure:

```markdown
### REQ-<AREA>-<NUMBER>: <Short Title>
- **Statement**: <Normative MUST / SHOULD specification>
- **Rationale**: <1-line justification linking to TDD section>
- **Acceptance**: <Executable pytest node ID OR CLI command (prefixed with manual: if manual)>
- **Status**: <done | partial | open | deferred>
- **Owner**: <area-name>
```

### Definitions of Done & Status Lifecycle

| Status | Meaning | Promotion Criteria |
|---|---|---|
| `open` | Scoped and approved requirement; work not started or tests not yet implemented. | Requirement accepted into specification. |
| `partial` | Partially implemented or partially passing; or blocked by operational dependencies. | Implementation in progress; initial tests or benchmarks recorded. |
| `done` | Fully implemented, passing all verification gates, and covered by automated tests or verified manual runbook. | All criteria met; automated `# REQ-...` test passes in offline suite OR `manual:` runbook verified. |
| `deferred` | Intentionally postponed to a future milestone (e.g. Phase 2 or Phase 3 roadmap). | Documented as out of scope for the current capstone milestone. |

---

## 3. Specification Index

| Area | Spec File | Description | REQ Prefix |
|---|---|---|---|
| **RAG Corpus** | [`specs/rag-corpus.md`](rag-corpus.md) | FAQ synthesis, policy single-sourcing, linting, agent provenance, theme regrouping, knowledge lifecycle, versioning, and atomic rollback. | `REQ-RAG-CORPUS-` |
| **RAG Retrieval** | [`specs/rag-retrieval.md`](rag-retrieval.md) | Agent Search query cleaning, threshold configuration, multi-tier evaluation methodology, citation verification, regression gates, and performance targets. | `REQ-RAG-RETR-` |
| **Infra & Search** | [`specs/infra-agent-search.md`](infra-agent-search.md) | Terraform modular architecture, VM-only execution, remote state bucket, location placement, content configs, IAM policies, and soft-delete handling. | `REQ-INFRA-SEARCH-` |
| **Router S1** | [`specs/router-s1.md`](router-s1.md) | System-1 typed-decision taxonomy, PII redaction, teacher labeling, dataset stratification, model calibration, threshold sweeping, and routing invariants. | `REQ-ROUTER-S1-` |
| **Billing & Models** | [`specs/billing-and-models.md`](billing-and-models.md) | Guardrails restricting offline LLM workloads to Jetski agents, allowed runtime Argolis API calls, and token-based cost accounting rules. | `REQ-BILLING-` |

---

## 4. Agent Workflow

When making changes in this repository, every AI agent MUST follow this four-step workflow:

```
┌─────────────────┐     ┌────────────────────────┐     ┌───────────────────────┐     ┌───────────────────────┐
│ 1. Read Spec    │ ──▶ │ 2. Declare REQ IDs     │ ──▶ │ 3. Implement & Test   │ ──▶ │ 4. Conductor Review   │
│ Read relevant   │     │ State touched REQ IDs  │     │ Add # REQ-... comments│     │ Conductor reviews diff│
│ specs/<area>.md │     │ in plan & response     │     │ Verify green pytest   │     │ against spec & commits│
└─────────────────┘     └────────────────────────┘     └───────────────────────┘     └───────────────────────┘
```

1. **Read the relevant spec**: Before writing or modifying any code, read the corresponding spec in `specs/`.
2. **Declare REQ IDs**: Explicitly state which requirement IDs your task implements, modifies, or references.
3. **Reference in tests**: Add a `# REQ-...` comment line above or inside every test that validates the requirement.
4. **Enforce traceability**: Run `.venv/bin/python -m pytest tests/test_spec_traceability.py` to assert that:
   - All requirement IDs are unique and well-formed.
   - Every `done` requirement has a test reference in `tests/**` or an acceptance command marked `manual:`.
   - All REQ IDs referenced in tests exist in the specs.
5. **Conductor Review**: The conductor diffs changes against the spec. Any code change lacking spec alignment will be rejected.

## Plan references

`PLAN.md` / `PLAN2.md` cited in specs are the conductor work plans, kept in [docs/plans/](../docs/plans/) (copied from the orchestration scratch area; frozen at the time each workstream ran).
