# PLAN7 RAG Reranker Training Data Card

- **Date**: 2026-10-25
- **Corpus Source**: Release 5 (`faq_v2p/docs`, 50 articles)
- **Target Candidates Per Query**: 12 (Positives + Hard Negatives + Randoms)

## 1. Split & Source Breakdown

| Metric | Total | Train | Validation | Description |
| :--- | :---: | :---: | :---: | :--- |
| **Total Queries** | **1643** | **1479** | **164** | 90% / 10% thread-level split |
| **Graded Queries** | 447 | 398 | 49 | Agent-labeled disambiguation pool (primary=2, secondary=1) |
| **Silver Queries** | 1196 | 1081 | 115 | Capped customer first turns from themes_v2 (target=2, other=0) |
| **Unique Threads** | 1643 | 1479 | 164 | Disjoint thread partition |
| **Corpus Articles** | 50 | 50 | 50 | 100% Release 5 coverage |

## 2. Per-Article Primary Target Balance

- **Min Queries / Article**: 6
- **Max Queries / Article**: 132
- **Mean Queries / Article**: 32.9
- **Median Queries / Article**: 27

## 3. Candidate Composition & Label Distribution

- **Mean Candidates / Query**: 12.0 (min=12, max=12)
- **Mean Positives / Query (Grade $\ge$ 1)**: 1.24
- **Mean Negatives / Query (Grade 0)**: 10.76
- **Graded Relevance Levels**:
  - `Grade 2`: Primary relevant FAQ document
  - `Grade 1`: Secondary relevant FAQ document
  - `Grade 0`: Hard negative or non-relevant candidate document

## 4. Leakage & Split Invariant Proofs

| Invariant | Value | Status | Acceptance Criterion |
| :--- | :---: | :---: | :--- |
| **Thread Overlap with DEV/TEST** | **0** | **PASSED** | Must be strictly 0 |
| **Normalized Query Overlap with DEV/TEST** | **0** | **PASSED** | Must be strictly 0 |
| **Thread Overlap between Train and Val** | **0** | **PASSED** | Must be strictly 0 |
| **Doc Text Release 5 Conformance** | **50 / 50** | **PASSED** | 100% extracted from Release 5 `$S/faq_v2p/docs/` |
| **Offline DEV Eval File Leakage** | **0 labels** | **PASSED** | `dev_candidates.jsonl` contains zero DEV labels |

## 5. File Manifest

- `docs.json`: Formatted article catalog `{"doc_id": {"title", "also_asked", "answer_prefix", "text"}}`
- `train.jsonl`: Training queries `{"qid", "query", "source", "rels", "candidates"}`
- `val.jsonl`: Validation queries `{"qid", "query", "source", "rels", "candidates"}`
- `dev_candidates.jsonl`: Unlabeled offline DEV candidates `{"qid", "query", "top10"}`
