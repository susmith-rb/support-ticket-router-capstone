# Specification: RAG Corpus (`rag-corpus`)

This specification governs the ingestion, synthesis, validation, lifecycle management, and publication of documents into Google Cloud Agent Search (Discovery Engine) data stores (`faq-articles` and `resolved-threads`).

---

## 1. Architecture & Pipeline Overview

The FAQ corpus serves as the authoritative, citable knowledge base for the `FaqResolverAgent` (TDD §6).

```
threads.jsonl (81k customer threads) ──┐
labels.jsonl (teacher topics) ─────────┴──▶ rag/regroup_themes.py ──▶ themes_v2.json + gold_map_v1_v2.json
                                                    │
                                                    ▼
                                           rag/build_faq_corpus.py ──▶ faq-articles (.txt) + manifest.jsonl
                                                    │
                                                    ▼
                                           rag/publish_faq.py      ──▶ GCS v<N>/ + documents:import (FULL)
                                                                       (Discovery Engine: faq-articles)
```

1. **Threads Corpus (`resolved-threads`)**: Built from the public Kaggle Customer Support on Twitter dataset (`twcs.csv`), filtered to 81,103 customer-opened threads with `AmazonHelp` (rag/README.md line 9). Each document contains structured conversation turns with customer IDs and PII redacted. It serves as secondary evidence for agents and is never cited alone.
2. **FAQ Articles (`faq-articles`)**: 50 concise help-center articles (200–600 tokens) synthesized for the simulated company *Global Retail Hub* (rag/README.md line 19; rag/EVAL_RESULTS.md line 154). Each article represents a coherent theme and is anchored in a single canonical retail policy (`SIMULATED_POLICY`). One article = one chunk, keeping questions, policies, and troubleshooting steps together.
3. **Corpus v2 Evolution** (rag/EVAL_RESULTS.md §8):
   - Merged 4 overlapping order-status themes into `faq-order-status-late-or-not-arrived-order`.
   - Merged 2 missing/stolen delivery themes into `faq-order-status-missing-or-stolen-delivered-package`.
   - Partitioned broad hardware topics into Kindle, Fire TV, and general device articles.
   - Dropped 4 non-actionable praise/social chatter themes (91 threads).
   - Replaced LLM-hallucinated timelines with verified policy rules.
   - Preserved byte-identical contents for 44 unchanged articles.
4. **Knowledge Lifecycle**: Enforced through `manifest.jsonl` tracking document SHA-256 hashes, version increments, draft/active/retired statuses, and supersedes pointers. Publications use `reconciliationMode: FULL` with retired tombstones excluded from import payloads to guarantee clean purges (TDD §6.6; rag/EVAL_RESULTS.md §8).

---

## 2. Requirement Index

| ID | Title | Status | Acceptance |
|---|---|---|---|
| `REQ-RAG-CORPUS-001` | FAQ Article Body Formatting | `done` | `tests/rag/test_build_faq_corpus.py::test_build_article_body_formatting` |
| `REQ-RAG-CORPUS-002` | Policy Lint and LLM Retry during Synthesis | `done` | `tests/rag/test_build_faq_corpus.py::test_lint_retry_path_with_fake_llm` |
| `REQ-RAG-CORPUS-003` | Unchanged Article Byte-Identical Reuse | `done` | `tests/rag/test_build_faq_corpus.py::test_reuse_from_copies_unchanged_articles_byte_identical` |
| `REQ-RAG-CORPUS-004` | Agent-Authored v2 Article Provenance | `done` | `tests/rag/test_build_faq_corpus.py::test_articles_from_mode_renders_docs_and_sets_provenance` |
| `REQ-RAG-CORPUS-005` | Content SHA-256 and Answer Extraction | `done` | `tests/rag/test_publish_faq.py::test_sha` |
| `REQ-RAG-CORPUS-006` | Policy Single-Sourcing Lint Rules | `done` | `tests/rag/test_publish_faq.py::test_lint_flags_invented_fact` |
| `REQ-RAG-CORPUS-007` | Knowledge Lifecycle Manifest Management | `done` | `tests/rag/test_publish_faq.py::test_load_manifest_existing` |
| `REQ-RAG-CORPUS-008` | Article Versioning, Draft Status, and Retirement | `done` | `tests/rag/test_publish_faq.py::test_stamp_content_changed` |
| `REQ-RAG-CORPUS-009` | Tombstone Exclusion from Import Payload | `done` | `tests/rag/test_publish_faq.py::test_build_import_payload_excludes_retired` |
| `REQ-RAG-CORPUS-010` | Endpoint Location Parametrization for Ingestion | `done` | `tests/rag/test_publish_faq.py::test_publish_faq_location_us` |
| `REQ-RAG-CORPUS-011` | Deterministic Document ID Generation | `done` | `tests/rag/test_regroup_themes.py::test_doc_id_for_theme` |
| `REQ-RAG-CORPUS-012` | Theme Merging and Thread Disjointness | `done` | `tests/rag/test_regroup_themes.py::test_merge_application` |
| `REQ-RAG-CORPUS-013` | Gold Map Translation from v1 to v2 | `done` | `tests/rag/test_regroup_themes.py::test_gold_map_completeness` |
| `REQ-RAG-CORPUS-014` | Theme Clustering with Injectable LLM | `done` | `tests/rag/test_regroup_themes.py::test_cluster_other_injectable_fake_llm` |
| `REQ-RAG-CORPUS-015` | Publish, Verification, and Rollback Operations | `done` | `manual: python rag/publish_faq.py --work rag/corpus_v2/ --dry-run` |
| `REQ-RAG-CORPUS-016` | Human Review for Knowledge Activation | `open` | `manual: python rag/publish_faq.py --work rag/corpus_v2/ --activate faq-order-status-late-or-not-arrived-order --reviewed-by susmith` |

---

## 3. Detailed Requirements

### REQ-RAG-CORPUS-001: FAQ Article Body Formatting
- **Statement**: The system MUST format synthesized FAQ articles into structured help-center articles containing an article title, metadata header, canonical question, concise answer, and troubleshooting bullet points.
- **Rationale**: Ensures uniform article structure across all customer-facing help documents (TDD §6.1).
- **Acceptance**: `tests/rag/test_build_faq_corpus.py::test_build_article_body_formatting`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-002: Policy Lint and LLM Retry during Synthesis
- **Statement**: During FAQ article synthesis, the builder MUST validate generated text against `SIMULATED_POLICY` using `publish_faq.lint` and automatically retry generation if unapproved numeric facts or invented policy claims are found.
- **Rationale**: Prevents hallucinated delivery windows, refund policies, or fees from being committed to the knowledge base (TDD §6.2, §6.6).
- **Acceptance**: `tests/rag/test_build_faq_corpus.py::test_lint_retry_path_with_fake_llm`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-003: Unchanged Article Byte-Identical Reuse
- **Statement**: The corpus build pipeline MUST support `--reuse-from` to preserve and copy unchanged articles byte-identically from a prior release directory, bypassing LLM synthesis calls.
- **Rationale**: Eliminates redundant token costs and guarantees stable content hashes across releases (TDD §6.6; PLAN.md §Workstream B).
- **Acceptance**: `tests/rag/test_build_faq_corpus.py::test_reuse_from_copies_unchanged_articles_byte_identical`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-004: Agent-Authored v2 Article Provenance
- **Statement**: All newly authored or modified articles in corpus v2 MUST record provenance metadata indicating the authoring agent (`authored_by: jetski-agent:gemini-3.8-flash-high`) in article frontmatter and manifest records.
- **Rationale**: Provides clear audit trails separating human-reviewed, teacher-generated, and agent-generated text (TDD §6.6; rag/EVAL_RESULTS.md §8).
- **Acceptance**: `tests/rag/test_build_faq_corpus.py::test_articles_from_mode_renders_docs_and_sets_provenance`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-005: Content SHA-256 and Answer Extraction
- **Statement**: The publication pipeline MUST calculate a deterministic SHA-256 digest over article content and extract the answer-only text without preamble or paraphrase metadata for exact citation validation.
- **Rationale**: Ensures tamper-evident revision tracking and enables character-exact citation checks (TDD §6.4, §6.6).
- **Acceptance**: `tests/rag/test_publish_faq.py::test_sha`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-006: Policy Single-Sourcing Lint Rules
- **Statement**: The policy linter MUST inspect article text for numbers, dollar amounts, day counts, and business-day ranges, flagging any quantities that do not appear in the canonical `SIMULATED_POLICY` specification.
- **Rationale**: Prevents conflicting policy rules across different help articles (TDD §6.6; rag/README.md lines 53–56).
- **Acceptance**: `tests/rag/test_publish_faq.py::test_lint_flags_invented_fact`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-007: Knowledge Lifecycle Manifest Management
- **Statement**: The publication tool MUST manage an auditable `manifest.jsonl` recording `article_id`, `version`, `content_sha256`, `status`, `supersedes`, `policy_version`, and effective timestamps for every document.
- **Rationale**: Provides the foundational ledger for document lifecycle tracking and rollbacks (TDD §6.6).
- **Acceptance**: `tests/rag/test_publish_faq.py::test_load_manifest_existing`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-008: Article Versioning, Draft Status, and Retirement
- **Statement**: When stamping an existing article with modified content, the system MUST increment its version number by 1, reset its status to `draft`, and clear previous approval metadata; when an article is superseded, it MUST mark status as `retired`.
- **Rationale**: Guarantees that modified content cannot be served as active without renewed verification (TDD §6.6).
- **Acceptance**: `tests/rag/test_publish_faq.py::test_stamp_content_changed`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-009: Tombstone Exclusion from Import Payload
- **Statement**: When generating the Discovery Engine import payload (`import.jsonl`), the publisher MUST omit records with `status="retired"` so that a `reconciliationMode=FULL` import automatically deletes retired documents from the index.
- **Rationale**: Prevents retired or merged articles from persisting in the index and cannibalizing search traffic (TDD §6.6; rag/EVAL_RESULTS.md §8).
- **Acceptance**: `tests/rag/test_publish_faq.py::test_build_import_payload_excludes_retired`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-010: Endpoint Location Parametrization for Ingestion
- **Statement**: Ingestion and publication tools MUST accept `--location` (`global` or `us`) and construct valid Discovery Engine API URLs and document import paths accordingly.
- **Rationale**: Supports seamless migration of search corpora between global and regional endpoints (TDD §4.1; PLAN2.md §Workstream L).
- **Acceptance**: `tests/rag/test_publish_faq.py::test_publish_faq_location_us`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-011: Deterministic Document ID Generation
- **Statement**: Document IDs MUST be derived deterministically from the intent family and theme slug according to the format `faq-{intent_family}-{theme_slug}`.
- **Rationale**: Ensures deterministic, repeatable document identity across pipeline rebuilds (TDD §6.6).
- **Acceptance**: `tests/rag/test_regroup_themes.py::test_doc_id_for_theme`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-012: Theme Merging and Thread Disjointness
- **Statement**: The theme regrouping pipeline MUST merge overlapping theme definitions according to the regroup decision map and ensure that each customer thread is assigned to at most one destination v2 theme.
- **Rationale**: Eliminates index chunk overlap and duplicate search candidates (TDD §6.1, §6.6; PLAN.md §Workstream B).
- **Acceptance**: `tests/rag/test_regroup_themes.py::test_merge_application`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-013: Gold Map Translation from v1 to v2
- **Statement**: The regrouping utility MUST generate an explicit `gold_map_v1_v2.json` mapping every v1 theme to its v2 replacement or explicitly registering it as dropped.
- **Rationale**: Enables automated translation of silver evaluation ground truth across corpus iterations (TDD §6.4, §6.6).
- **Acceptance**: `tests/rag/test_regroup_themes.py::test_gold_map_completeness`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-014: Theme Clustering with Injectable LLM
- **Statement**: Theme clustering of unclassified customer threads MUST support injecting fake or mock LLM implementations to allow fast, deterministic, offline unit testing.
- **Rationale**: Enforces offline unit test execution without network or API dependencies (TDD §12.2; AGENTS.md rule 6).
- **Acceptance**: `tests/rag/test_regroup_themes.py::test_cluster_other_injectable_fake_llm`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-015: Publish, Verification, and Rollback Operations
- **Statement**: The publication CLI MUST support stamping releases into GCS (`gs://susmith-fde-ticket-router-laya/corpus/faq-articles/v<N>/`), triggering FULL reconciliation import, waiting on LRO completion, probing that the served version matches the stamped version, and executing rollback to a prior release manifest upon failure.
- **Rationale**: Provides an automated, safe release runbook with instantaneous rollback capability (TDD §6.6, §16.2).
- **Acceptance**: `manual: python rag/publish_faq.py --work rag/corpus_v2/ --dry-run`
- **Status**: done
- **Owner**: rag-corpus

### REQ-RAG-CORPUS-016: Human Review for Knowledge Activation
- **Statement**: Before articles transition from `status="draft"` to `status="active"`, a human reviewer SHOULD review the content for retail accuracy and record `reviewed_by` (LDAP) and `reviewed_at` timestamps.
- **Rationale**: Prevents unreviewed LLM-generated policies from being served to live customers (TDD §6.6).
- **Acceptance**: `manual: python rag/publish_faq.py --work rag/corpus_v2/ --activate faq-order-status-late-or-not-arrived-order --reviewed-by susmith`
- **Status**: open
- **Owner**: rag-corpus

---

## 4. Open Questions / Deltas

1. **Draft Status of Current Live Corpus**: All 50 v2 articles currently indexed in Discovery Engine hold `status="draft"`. While functional for search evaluation, query-time filtering for `status="active"` (REQ-RAG-RETR-009) cannot be enabled until human SME review is conducted.
2. **Resolved Threads Document Representation**: TDD §6.2 prescribes chunking customer threads into ≤512 token QA pairs. The active corpus in `corpus/resolved-threads/v1/` indexes full threads as single structData documents without body content (rag/README.md line 9).
3. **Single-Sourced Policy Lint Exceptions**: The initial v1 article `faq-account-payment-account-locked-blocked` included an invented response timeline ("within 48 hours") not present in `SIMULATED_POLICY` (rag/README.md line 53; rag/EVAL_RESULTS.md line 17). This was remediated in v2 release 3 (rag/EVAL_RESULTS.md line 160); future policy changes require updating `SIMULATED_POLICY` prior to re-authoring articles.
