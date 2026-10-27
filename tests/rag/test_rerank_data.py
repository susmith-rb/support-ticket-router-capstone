"""tests/rag/test_rerank_data.py — Unit tests for RAG reranker dataset generation (PLAN7).

Covers:
1. Leakage prevention (thread_id ∉ dev ∪ test; normalized query ∉ dev/test queries).
2. Graded mapping (primary=2, secondary=1, negatives=0).
3. Per-article balance capping for silver examples.
4. Hard-negative composition (positives + confusion pairs + same-family siblings + randoms).
5. Deterministic thread-level train/val split (zero thread overlap).
6. 100% Release 5 doc provenance ($S/faq_v2p/docs).
7. Unlabeled offline DEV candidates export.

# REQ-RAG-RETR-016
"""

import json
import random
from pathlib import Path

import pytest

from experiments.rerank_data import (
  assign_candidate_grades,
  build_candidates_for_query,
  build_docs_catalog,
  export_dev_candidates,
  load_eval_leakage_sets,
  normalize_query,
  split_train_val_by_thread,
  verify_no_leakage,
)

SCRATCH_DIR = Path(
    "/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag"
)
DOCS_DIR = SCRATCH_DIR / "faq_v2p" / "docs"
EVAL_DIR = SCRATCH_DIR / "eval"
RERANK_DIR = SCRATCH_DIR / "rerank"


def test_normalize_query():
  """Test normalization handles handles, tokens, punctuation, casing, whitespace."""
  raw = "  @AmazonHelp @user [ORDER] Where is my package?!?? [URL]  "
  norm = normalize_query(raw)
  assert norm == "where is my package"


def test_verify_no_leakage_clean():
  """Verify clean items pass without error."""
  eval_tids = {"100", "200"}
  eval_qnorms = {"where is my order", "how do i return"}

  items = [
      {"qid": "q1", "thread_id": "300", "query": "Why was my card charged?"},
      {"qid": "q2", "thread_id": "400", "query": "Cancel my membership"},
  ]
  # Should not raise
  verify_no_leakage(items, eval_tids, eval_qnorms)


def test_verify_no_leakage_flags_thread_id():
  """Verify thread ID leakage into dev/test is caught."""
  eval_tids = {"100", "200"}
  eval_qnorms = {"where is my order"}

  items = [
      {
          "qid": "q1",
          "thread_id": "100",  # Leaked!
          "query": "Different text but leaked thread",
      }
  ]
  with pytest.raises(
      ValueError, match="LEAKAGE VIOLATION: Source thread IDs found"
  ):
    verify_no_leakage(items, eval_tids, eval_qnorms)


def test_verify_no_leakage_flags_query_duplicate():
  """Verify query text duplicate matching dev/test is caught."""
  eval_tids = {"100", "200"}
  eval_qnorms = {"where is my order"}

  items = [
      {
          "qid": "q1",
          "thread_id": "999",
          "query": "Where is my order???",  # Leaked text!
      }
  ]
  with pytest.raises(
      ValueError, match="LEAKAGE VIOLATION: Query text matches query"
  ):
    verify_no_leakage(items, eval_tids, eval_qnorms)


def test_live_data_zero_leakage_invariants():
  """Enforce that generated train and val datasets have ZERO leakage against live DEV and TEST."""
  if not (EVAL_DIR / "dev.jsonl").exists() or not (RERANK_DIR / "train.jsonl").exists():
    pytest.skip("Live evaluation and rerank data files not found in scratch")

  eval_tids, eval_qnorms = load_eval_leakage_sets(
      EVAL_DIR / "dev.jsonl", EVAL_DIR / "test.jsonl"
  )

  train_items = [
      json.loads(line)
      for line in (RERANK_DIR / "train.jsonl").read_text().splitlines()
      if line.strip()
  ]
  val_items = [
      json.loads(line)
      for line in (RERANK_DIR / "val.jsonl").read_text().splitlines()
      if line.strip()
  ]

  # Invariant 1: No train item leaks into eval
  verify_no_leakage(train_items, eval_tids, eval_qnorms)

  # Invariant 2: No val item leaks into eval
  verify_no_leakage(val_items, eval_tids, eval_qnorms)

  # Invariant 3: Train and Val thread IDs are strictly disjoint
  train_tids = set(str(it["thread_id"]) for it in train_items)
  val_tids = set(str(it["thread_id"]) for it in val_items)
  overlap = train_tids & val_tids
  assert not overlap, f"Train and val share thread IDs: {overlap}"


def test_graded_mapping_2_1_0():
  """Test graded mapping assigns 2 to primary, 1 to secondary, and 0 to negatives."""
  candidates = ["doc_primary", "doc_sec1", "doc_sec2", "doc_neg1", "doc_neg2"]
  primary = "doc_primary"
  secondaries = ["doc_sec1", "doc_sec2"]

  grades = assign_candidate_grades(candidates, primary, secondaries)

  assert grades["doc_primary"] == 2
  assert grades["doc_sec1"] == 1
  assert grades["doc_sec2"] == 1
  assert grades["doc_neg1"] == 0
  assert grades["doc_neg2"] == 0
  assert len(grades) == len(candidates)


def test_per_article_balance_cap(tmp_path):
  """Verify per-article cap limits silver items per document."""
  from experiments.rerank_data import load_silver_data

  docs_catalog = {
      "faq-order-status-late-order": {
          "title": "Late Order",
          "text": "Late Order FAQ",
      },
      "faq-returns-refunds-damaged": {
          "title": "Damaged Item",
          "text": "Damaged Item FAQ",
      },
  }

  themes_json = [
      {
          "theme_id": "late_order",
          "intent_family": "order_status",
          "thread_ids": [f"10{i:02d}" for i in range(20)],
      },
      {
          "theme_id": "damaged",
          "intent_family": "returns_refunds",
          "thread_ids": [f"20{i:02d}" for i in range(10)],
      },
  ]
  themes_file = tmp_path / "themes.json"
  themes_file.write_text(json.dumps(themes_json))

  # Create threads file with 20 turns for late_order, 10 for damaged
  threads_file = tmp_path / "threads.jsonl"
  with open(threads_file, "w") as f:
    for i in range(20):
      f.write(
          json.dumps({
              "thread_id": f"10{i:02d}",
              "turns": [{
                  "role": "customer",
                  "text": f"My late delivery order inquiry #{i}",
              }],
          })
          + "\n"
      )
    for i in range(10):
      f.write(
          json.dumps({
              "thread_id": f"20{i:02d}",
              "turns": [{
                  "role": "customer",
                  "text": f"My damaged package delivered broken #{i}",
              }],
          })
          + "\n"
      )

  # Test with cap = 5
  silver = load_silver_data(
      themes_path=themes_file,
      threads_path=threads_file,
      eval_tids=set(),
      eval_qnorms=set(),
      graded_tids=set(),
      docs_catalog=docs_catalog,
      per_article_cap=5,
      seed=42,
  )

  counts = {}
  for s in silver:
    doc = s["primary"]
    counts[doc] = counts.get(doc, 0) + 1

  assert counts["faq-order-status-late-order"] == 5
  assert counts["faq-returns-refunds-damaged"] == 5
  assert len(silver) == 10


def test_hard_negative_composition():
  """Test that build_candidates_for_query composes positives, confusion pairs, same-family siblings, and randoms."""
  all_docs = [f"doc_{i}" for i in range(20)]
  intent_map = {
      f"doc_{i}": "family_A" if i < 10 else "family_B" for i in range(20)
  }
  confusion_map = {
      "doc_0": ["doc_15"],  # Cross-family confusion pair
  }

  rng = random.Random(42)
  positives = ["doc_0"]  # In family_A

  candidates = build_candidates_for_query(
      positives=positives,
      all_doc_ids=all_docs,
      intent_map=intent_map,
      confusion_map=confusion_map,
      rng=rng,
      target_k=12,
  )

  # 1. Target length
  assert len(candidates) == 12
  # 2. No duplicates
  assert len(set(candidates)) == 12
  # 3. Positive included
  assert "doc_0" in candidates
  # 4. Confusion partner included
  assert "doc_15" in candidates
  # 5. Same family siblings included
  family_a_cands = [c for c in candidates if intent_map[c] == "family_A"]
  assert len(family_a_cands) >= 3


def test_deterministic_split_by_thread():
  """Verify split_train_val_by_thread splits deterministically by thread with zero thread leakage."""
  items = [
      {"qid": f"q_{i}", "thread_id": f"t_{i // 3}", "query": f"Query {i}"}
      for i in range(90)
  ]  # 30 threads, 3 queries each

  train_1, val_1 = split_train_val_by_thread(items, val_ratio=0.10, seed=42)
  train_2, val_2 = split_train_val_by_thread(items, val_ratio=0.10, seed=42)

  # Check determinism
  assert [r["qid"] for r in train_1] == [r["qid"] for r in train_2]
  assert [r["qid"] for r in val_1] == [r["qid"] for r in val_2]

  # Check thread disjointness
  train_tids = set(r["thread_id"] for r in train_1)
  val_tids = set(r["thread_id"] for r in val_1)
  assert train_tids & val_tids == set()

  # Check val ratio on threads
  assert len(val_tids) == 3  # 10% of 30 threads
  assert len(train_tids) == 27


def test_every_doc_text_comes_from_release_5():
  """Verify that build_docs_catalog parses exactly 50 docs from Release 5 with all required sections."""
  if not DOCS_DIR.exists():
    pytest.skip("Docs directory not found")

  catalog = build_docs_catalog(DOCS_DIR)
  assert len(catalog) == 50

  for doc_id, doc in catalog.items():
    assert doc["doc_id"] == doc_id
    assert doc["title"].strip() != ""
    assert isinstance(doc["also_asked"], list)
    assert len(doc["also_asked"]) > 0
    assert doc["answer_prefix"].strip() != ""
    assert doc["text"].startswith("Title: ")
    assert "Also asked as:" in doc["text"]
    assert "Answer: " in doc["text"]


def test_dev_candidates_unlabeled_and_matches_dev_qids(tmp_path):
  """Verify dev_candidates.jsonl contains 300 rows matching DEV qids and NO labels."""
  dev_rows_file = SCRATCH_DIR / "ablation" / "dev_rows_phaseD.json"
  if not dev_rows_file.exists():
    pytest.skip("dev_rows_phaseD.json not found")

  out_file = tmp_path / "dev_candidates.jsonl"
  count = export_dev_candidates(
      dev_rows_file, out_file, config_name="intent_noisy_0.2"
  )
  assert count == 300

  lines = out_file.read_text().splitlines()
  assert len(lines) == 300
  for l in lines:
    row = json.loads(l)
    assert "qid" in row
    assert row["qid"].startswith("d")
    assert "query" in row
    assert "top10" in row
    assert isinstance(row["top10"], list)
    # Strictly NO labels
    assert "gold" not in row
    assert "relevant" not in row
    assert "original_gold" not in row
    assert "intent" not in row
    assert "rels" not in row


def test_build_candidates_with_hard_negatives():
  """Verify hard_negatives are included in candidate list and receive grade 0."""
  positives = ["doc_primary"]
  all_docs = ["doc_primary", "doc_sec1", "doc_hn1", "doc_other1", "doc_other2"]
  intent_map = {d: "intent_a" for d in all_docs}
  confusion_map = {}
  rng = random.Random(42)

  candidates = build_candidates_for_query(
      positives=positives,
      all_doc_ids=all_docs,
      intent_map=intent_map,
      confusion_map=confusion_map,
      rng=rng,
      target_k=4,
      hard_negatives=["doc_hn1"],
  )
  assert "doc_hn1" in candidates
  assert "doc_primary" in candidates

  grades = assign_candidate_grades(candidates, "doc_primary", [])
  assert grades["doc_primary"] == 2
  assert grades["doc_hn1"] == 0


def test_load_pool_graded_data(tmp_path):
  """Verify load_pool_graded_data filters non-support, non-English, primary=None, and leakage."""
  from experiments.rerank_data import load_pool_graded_data

  docs_catalog = {
      "faq-returns-refunds-return-pickup-issues": {"title": "Pickup Issues"},
      "faq-order-status-driver-and-courier-issues": {"title": "Driver Issues"},
      "faq-other-delivery-and-logistics-complaints": {"title": "Logistics Complaints"},
  }
  eval_tids = {"9999"}
  eval_qnorms = {"where is my package"}

  # Write pool.jsonl
  pool_file = tmp_path / "pool.jsonl"
  pool_data = [
      {"pid": "s101", "query": "Pickup delayed for days", "thread_id": "101"},
      {"pid": "s102", "query": "Package never arrived", "thread_id": "102"},
      {"pid": "s103", "query": "Where is my package?", "thread_id": "103"},  # Leaked query!
      {"pid": "s104", "query": "Leaked thread query", "thread_id": "9999"},  # Leaked thread!
      {"pid": "s105", "query": "Non-support chatter", "thread_id": "105"},
      {"pid": "s106", "query": "French query bonjour", "thread_id": "106"},
  ]
  with open(pool_file, "w") as f:
    for p in pool_data:
      f.write(json.dumps(p) + "\n")

  # Write labels_pool_0.jsonl
  labels_file = tmp_path / "labels_pool_0.jsonl"
  labels_data = [
      {
          "qid": "s101",
          "support_request": True,
          "language": "en",
          "primary": "faq-returns-refunds-return-pickup-issues",
          "secondary": ["faq-other-delivery-and-logistics-complaints"],
          "hard_negative": "faq-order-status-driver-and-courier-issues",
      },
      {
          "qid": "s102",
          "support_request": True,
          "language": "en",
          "primary": None,  # Excluded!
          "secondary": [],
          "hard_negative": None,
      },
      {
          "qid": "s103",  # Leaked query text!
          "support_request": True,
          "language": "en",
          "primary": "faq-returns-refunds-return-pickup-issues",
          "secondary": [],
          "hard_negative": None,
      },
      {
          "qid": "s104",  # Leaked thread ID!
          "support_request": True,
          "language": "en",
          "primary": "faq-returns-refunds-return-pickup-issues",
          "secondary": [],
          "hard_negative": None,
      },
      {
          "qid": "s105",
          "support_request": False,  # Excluded!
          "language": "en",
          "primary": "faq-returns-refunds-return-pickup-issues",
          "secondary": [],
          "hard_negative": None,
      },
      {
          "qid": "s106",
          "support_request": True,
          "language": "fr",  # Excluded!
          "primary": "faq-returns-refunds-return-pickup-issues",
          "secondary": [],
          "hard_negative": None,
      },
  ]
  with open(labels_file, "w") as f:
    for l in labels_data:
      f.write(json.dumps(l) + "\n")

  items = load_pool_graded_data(tmp_path, eval_tids, eval_qnorms, docs_catalog)
  assert len(items) == 1
  it = items[0]
  assert it["qid"] == "s101"
  assert it["primary"] == "faq-returns-refunds-return-pickup-issues"
  assert it["secondary"] == ["faq-other-delivery-and-logistics-complaints"]
  assert it["hard_negative"] == "faq-order-status-driver-and-courier-issues"
  assert it["source"] == "graded_pool"


def test_live_v2_data_zero_leakage_invariants():
  """Enforce that generated v2 train and val datasets have ZERO leakage against live DEV and TEST."""
  v2_dir = SCRATCH_DIR / "rerank" / "v2"
  if not (EVAL_DIR / "dev.jsonl").exists() or not (v2_dir / "train.jsonl").exists():
    pytest.skip("Live evaluation and rerank v2 data files not found in scratch")

  eval_tids, eval_qnorms = load_eval_leakage_sets(
      EVAL_DIR / "dev.jsonl", EVAL_DIR / "test.jsonl"
  )

  train_items = [
      json.loads(line)
      for line in (v2_dir / "train.jsonl").read_text().splitlines()
      if line.strip()
  ]
  val_items = [
      json.loads(line)
      for line in (v2_dir / "val.jsonl").read_text().splitlines()
      if line.strip()
  ]

  # Invariant 1: No train item leaks into eval
  verify_no_leakage(train_items, eval_tids, eval_qnorms)

  # Invariant 2: No val item leaks into eval
  verify_no_leakage(val_items, eval_tids, eval_qnorms)

  # Invariant 3: Train and Val thread IDs are strictly disjoint
  train_tids = set(str(it["thread_id"]) for it in train_items)
  val_tids = set(str(it["thread_id"]) for it in val_items)
  overlap = train_tids & val_tids
  assert not overlap, f"Train and val share thread IDs: {overlap}"

