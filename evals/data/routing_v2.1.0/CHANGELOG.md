# Changelog for routing_v2.1.0

Release Date: 2026-10-28
Base Version: `routing_v2.0.0`
Specification: `specs/routing-eval.md` (§REQ-REVAL-005, §REQ-REVAL-006, §REQ-REVAL-009)

---

## Overview

`routing_v2.1.0` is a backward-compatible minor dataset release following `routing_v2.0.0`. It incorporates out-of-scope (OOS / OOD) label refinements under Rule 2a, multi-pass annotation directory precedence, and meaning-preserving child decision inheritance.

Split assignments and ticket ID compositions remain 100% identical to `routing_v2.0.0`, ensuring exact comparability across evaluation baselines and candidates while resolving label noise on retail boundary cases.

---

## Key Changes

### 1. Rule 2a Out-of-Domain Refusal Re-annotation
- **Context**: In `routing_v2.0.0`, all out-of-scope inquiries with `intent="other"` defaulted to `decision="escalate"`, conflating customer retail care inquiries lacking a self-service FAQ (which require agent escalation) with inquiries completely outside retail commerce (which require refusal).
- **Rule 2a Application**: Re-annotated 84 OOD tickets across 3 passes (`.cache/routing_eval/work/v1/annotations_oos_v21`):
  - Inquiries entirely outside retail customer care (e.g. weather, sports trivia, third-party transit directions, B2B software sales pitches, homework help, corporate job applications) are assigned `decision="reject_oos"`, `escalate=False`, `queue="none"`.
  - Customer retail inquiries lacking self-service FAQs (e.g. submitting store feedback, email newsletter subscription, ordering via support chat) remain `decision="escalate"`, `queue="human-standard"`.
  - Safety and medical emergencies remain `decision="escalate"`, `queue="human-urgent"`.
- **Distribution of `reject_oos`**:
  - `dev`: 12 cases
  - `heldout`: 31 cases
  - `regression`: 0 cases
  - `pool`: 1 case
  - Total: 44 cases

### 2. Multi-Pass Directory Precedence in Stage 3
- Fixed file discovery order in `pipeline_v1.py` `stage3` to respect the explicit sequence of paths in `annotations_dir`.
- Replaced alphabetical sorting of resolved glob paths so override directories (e.g. `annotations_oos_v21`) cleanly supersede earlier passes per `(ticket_id, pass_index)` without creating duplicate passes or inflating consensus denominators.

### 3. Meaning-Preserving Child Decision Inheritance
- Meaning-preserving perturbations (`typos`, `case`, `punctuation_strip`, `whitespace_noise`, `emoji_slang`, `long_padding`, `prompt_injection`) and `paraphrase` variants now strictly inherit their parent's updated routing decision (`decision`, `escalate`, `queue`, `sentiment`, `urgency`), guaranteeing **0 parent-child decision divergence**.
- Semantic-altering perturbations (`truncate`, `conflicting_info`) continue to derive independent labels according to guideline conflict and missing-info rules.

### 4. Report Version Resolution
- Updated `evals/routing/report.py` (`_find_highest_version_dataset_dir`) to parse numeric components across `routing_v*` directories, resolving `routing_v2.1.0 > routing_v2.0.0 > routing_v1.2.0 > routing_v1.0.0`.

---

## Dataset Invariants & Split Verification

| Metric | routing_v2.0.0 | routing_v2.1.0 | Status |
| :--- | :--- | :--- | :--- |
| **Dev Split Count** | 519 | 519 | Identical |
| **Heldout Split Count** | 400 | 400 | Identical |
| **Regression Split Count** | 150 | 150 | Identical |
| **Pool Split Count** | 1,199 | 1,199 | Identical |
| **Total Cases** | 2,268 | 2,268 | Identical |
| **English Invariant (Dev/Heldout/Reg)** | 100.0% | 100.0% | Verified |
| **Heldout Laya Collisions** | 0 | 0 | Verified |
| **Parent-Child Decision Mismatches** | 39 | 0 | **Fixed (0 mismatches)** |
| **Total `reject_oos` Cases** | 0 | 44 | **Updated (Rule 2a applied)** |

---

## Files in Version Directory

- `dev.jsonl`: 519 cases
- `heldout.jsonl`: 400 cases
- `regression.jsonl`: 150 cases
- `pool.jsonl`: 1,199 cases
- `manifest.json`: SHA-256 hashes and file counts for immutable verification
- `CHANGELOG.md`: This release note
