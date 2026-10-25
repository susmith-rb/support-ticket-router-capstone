#!/usr/bin/env python3
"""rag/rerank_data.py — Dataset build and candidate generation for RAG reranker training.

Pure functions and CLI to assemble training and validation data for fine-tuning
cross-encoder and Laya choice rerankers (PLAN7). Enforces strict split discipline
(zero thread or normalized query leakage into DEV or TEST sets).

Outputs to $S/rerank/:
  - docs.json: catalog of Release 5 articles (title, also_asked, answer_prefix, text)
  - train.jsonl: 90% of queries split by thread
  - val.jsonl: 10% of queries split by thread
  - data_card.md: counts, candidate statistics, and leakage verification proofs
  - dev_candidates.jsonl: unlabeled top10 candidates for offline DEV evaluation
"""

import argparse
import hashlib
import json
from pathlib import Path
import random
import re
from typing import Any, Dict, List, Optional, Set, Tuple

CONFUSION_PAIRS = [
    # Shipping vs Order Status
    (
        "faq-shipping-policy-prime-delivery-speed-delays",
        "faq-order-status-late-or-not-arrived-order",
    ),
    (
        "faq-shipping-policy-prime-delivery-speed-delays",
        "faq-shipping-policy-weekend-and-same-day-delivery",
    ),
    (
        "faq-shipping-policy-carrier-and-courier-issues",
        "faq-order-status-driver-and-courier-issues",
    ),
    # Damaged vs Wrong vs Missing triad
    (
        "faq-returns-refunds-damaged-or-defective-items",
        "faq-returns-refunds-wrong-item-received",
    ),
    (
        "faq-returns-refunds-damaged-or-defective-items",
        "faq-order-status-missing-items-in-package",
    ),
    (
        "faq-returns-refunds-wrong-item-received",
        "faq-returns-refunds-missing-or-used-items",
    ),
    (
        "faq-returns-refunds-missing-or-used-items",
        "faq-order-status-missing-items-in-package",
    ),
    # Return Pickup vs Process
    (
        "faq-returns-refunds-return-pickup-issues",
        "faq-returns-refunds-return-policy-and-process",
    ),
    # Complaints
    (
        "faq-other-customer-service-complaints",
        "faq-other-general-complaints-and-feedback",
    ),
    (
        "faq-other-delivery-and-logistics-complaints",
        "faq-other-general-complaints-and-feedback",
    ),
    # Tracking vs Delivery vs Attempt
    (
        "faq-order-status-tracking-and-status",
        "faq-order-status-late-or-not-arrived-order",
    ),
    (
        "faq-order-status-tracking-and-status",
        "faq-order-status-missing-or-stolen-delivered-package",
    ),
    (
        "faq-order-status-false-delivery-attempts",
        "faq-order-status-tracking-and-status",
    ),
    (
        "faq-order-status-false-delivery-attempts",
        "faq-order-status-driver-and-courier-issues",
    ),
    # Tech / Devices / Apps
    (
        "faq-other-app-and-website-issues",
        "faq-account-payment-checkout-payment-errors",
    ),
    (
        "faq-other-device-and-hardware-issues",
        "faq-other-kindle-and-e-readers",
    ),
    (
        "faq-other-device-and-hardware-issues",
        "faq-other-fire-tv-and-streaming-devices",
    ),
    # Account login vs lock
    (
        "faq-account-payment-account-locked-blocked",
        "faq-account-payment-account-login-issues",
    ),
    (
        "faq-account-payment-unauthorized-charges",
        "faq-account-payment-double-multiple-charges",
    ),
]


def normalize_query(text: str) -> str:
  """Normalize query text for leakage checks: strip handles, tokens, punctuation, lowercase, collapse whitespace."""
  t = re.sub(r"@\w+", "", text)
  t = re.sub(r"\[(URL|ORDER|CARD|EMAIL|PHONE|NUM)\]", "", t)
  t = re.sub(r"[^\w\s]", " ", t.lower())
  return " ".join(t.split())


def load_eval_leakage_sets(
    dev_path: Path, test_path: Path
) -> Tuple[Set[str], Set[str]]:
  """Load thread_ids and normalized query strings from dev and test sets."""
  eval_thread_ids = set()
  eval_queries_norm = set()

  for path in (dev_path, test_path):
    if not path.exists():
      continue
    with open(path, "r", encoding="utf-8") as f:
      for line in f:
        if not line.strip():
          continue
        d = json.loads(line)
        if "thread_id" in d and d["thread_id"]:
          eval_thread_ids.add(str(d["thread_id"]))
        if "query" in d and d["query"]:
          eval_queries_norm.add(normalize_query(d["query"]))

  return eval_thread_ids, eval_queries_norm


def verify_no_leakage(
    items: List[Dict[str, Any]],
    eval_thread_ids: Set[str],
    eval_queries_norm: Set[str],
) -> None:
  """Enforce provenance and leakage invariants:

  1. No item's thread_id belongs to dev.jsonl or test.jsonl.
  2. No item's normalized query string matches any query in dev.jsonl or
  test.jsonl.
  """
  leaked_tids = []
  leaked_queries = []

  for it in items:
    tid = str(it.get("thread_id", ""))
    if tid and tid in eval_thread_ids:
      leaked_tids.append((it.get("qid", ""), tid))
    q = it.get("query", "")
    if q and normalize_query(q) in eval_queries_norm:
      leaked_queries.append((it.get("qid", ""), q))

  if leaked_tids:
    raise ValueError(
        f"LEAKAGE VIOLATION: Source thread IDs found in dev/test sets:"
        f" {leaked_tids[:5]}"
    )
  if leaked_queries:
    raise ValueError(
        f"LEAKAGE VIOLATION: Query text matches query in dev/test sets:"
        f" {leaked_queries[:5]}"
    )


def parse_article_text(raw_text: str, doc_id: str) -> Dict[str, Any]:
  """Parse release-5 doc into title, also_asked, answer_prefix (first ~600 chars), and formatted text."""
  lines = raw_text.splitlines()
  title = ""
  also_asked = []
  answer_lines = []

  in_also = False
  for idx, line in enumerate(lines):
    if line.startswith("# ") and not title:
      title = line.lstrip("# ").strip()
      continue
    if line.strip().lower().startswith("also asked as:"):
      in_also = True
      continue
    if in_also:
      if line.strip().startswith("- "):
        also_asked.append(line.strip()[2:].strip())
        continue
      elif not line.strip():
        # Lookahead: check if next non-empty line continues list
        next_is_item = False
        for k in range(idx + 1, len(lines)):
          if lines[k].strip():
            next_is_item = lines[k].strip().startswith("- ")
            break
        if next_is_item:
          continue
        else:
          in_also = False
      else:
        in_also = False
    answer_lines.append(line)

  ans_text = "\n".join(answer_lines).strip()
  prefix = ans_text[:600]

  parts = [f"Title: {title}"]
  if also_asked:
    parts.append("Also asked as:\n" + "\n".join(f"- {q}" for q in also_asked))
  if prefix:
    parts.append(f"Answer: {prefix}")
  full_text = "\n\n".join(parts)

  return {
      "doc_id": doc_id,
      "title": title,
      "also_asked": also_asked,
      "answer_prefix": prefix,
      "text": full_text,
  }


def build_docs_catalog(docs_dir: Path) -> Dict[str, Dict[str, Any]]:
  """Load and parse all FAQ articles from release-5 docs directory."""
  if not docs_dir.exists():
    raise FileNotFoundError(f"Docs directory not found: {docs_dir}")

  catalog = {}
  for p in sorted(docs_dir.glob("*.txt")):
    doc_id = p.stem
    raw = p.read_text(encoding="utf-8")
    parsed = parse_article_text(raw, doc_id)
    if not parsed["title"]:
      raise ValueError(f"Document {doc_id} has empty title")
    catalog[doc_id] = parsed

  if len(catalog) != 50:
    raise ValueError(f"Expected 50 documents in catalog, found {len(catalog)}")
  return catalog


def build_intent_family_map(docs_catalog: Dict[str, Any]) -> Dict[str, str]:
  """Infer intent family from doc_id prefix."""
  intent_map = {}
  known_families = [
      "account_payment",
      "order_status",
      "returns_refunds",
      "shipping_policy",
      "other",
  ]
  for doc_id in docs_catalog:
    matched = None
    for f in known_families:
      prefix = f"faq-{f.replace('_', '-')}-"
      if doc_id.startswith(prefix):
        matched = f
        break
    if not matched:
      # Fallback split
      parts = doc_id.replace("faq-", "").split("-")
      matched = parts[0]
    intent_map[doc_id] = matched
  return intent_map


def get_confusion_pairs() -> List[Tuple[str, str]]:
  """Return known confusion pairs from DEV analysis."""
  return list(CONFUSION_PAIRS)


def build_confusion_map(
    pairs: Optional[List[Tuple[str, str]]] = None,
) -> Dict[str, List[str]]:
  """Build undirected lookup map for confused partners."""
  if pairs is None:
    pairs = get_confusion_pairs()
  cmap = {}
  for d1, d2 in pairs:
    cmap.setdefault(d1, []).append(d2)
    cmap.setdefault(d2, []).append(d1)
  return cmap


def build_candidates_for_query(
    positives: List[str],
    all_doc_ids: List[str],
    intent_map: Dict[str, str],
    confusion_map: Dict[str, List[str]],
    rng: random.Random,
    target_k: int = 12,
) -> List[str]:
  """Construct a balanced set of ~12 candidates:

  1. Positives (all relevant docs, e.g. primary + secondaries).
  2. Hard negatives from DEV confusion map.
  3. Hard negatives from same-family siblings.
  4. Random negatives from remaining corpus.
  5. Shuffled deterministically.
  """
  candidates = list(positives)
  candidate_set = set(candidates)

  # 1. Add confusion pairs for any positive doc
  for pos in positives:
    for confused in confusion_map.get(pos, []):
      if confused not in candidate_set and confused in all_doc_ids:
        candidates.append(confused)
        candidate_set.add(confused)
        if len(candidates) >= target_k:
          break
    if len(candidates) >= target_k:
      break

  # 2. Add same-family siblings
  if len(candidates) < target_k and positives:
    primary_intent = intent_map.get(positives[0])
    if primary_intent:
      siblings = [
          d
          for d in all_doc_ids
          if intent_map.get(d) == primary_intent and d not in candidate_set
      ]
      rng.shuffle(siblings)
      for sib in siblings:
        candidates.append(sib)
        candidate_set.add(sib)
        if len(candidates) >= target_k:
          break

  # 3. Add random other docs
  if len(candidates) < target_k:
    remaining = [d for d in all_doc_ids if d not in candidate_set]
    rng.shuffle(remaining)
    for rem in remaining:
      candidates.append(rem)
      candidate_set.add(rem)
      if len(candidates) >= target_k:
        break

  # Final shuffle so positives are not always leading
  rng.shuffle(candidates)
  return candidates


def assign_candidate_grades(
    candidates: List[str], primary: str, secondaries: List[str]
) -> Dict[str, int]:
  """Assign grades (2 for primary, 1 for secondary, 0 for negative) for all candidate doc_ids."""
  sec_set = set(secondaries)
  rels = {}
  for doc_id in candidates:
    if doc_id == primary:
      rels[doc_id] = 2
    elif doc_id in sec_set:
      rels[doc_id] = 1
    else:
      rels[doc_id] = 0
  return rels


def load_graded_data(
    graded_path: Path,
    eval_tids: Set[str],
    eval_qnorms: Set[str],
    docs_catalog: Dict[str, Any],
) -> List[Dict[str, Any]]:
  """Load the 447 agent-labeled disambiguation pool tweets, enforcing zero leakage."""
  if not graded_path.exists():
    raise FileNotFoundError(f"Graded labels file not found: {graded_path}")

  graded_items = []
  with open(graded_path, "r", encoding="utf-8") as f:
    for line in f:
      if not line.strip():
        continue
      it = json.loads(line)
      tid = str(it.get("thread_id", ""))
      q = it.get("query", "")
      primary = it.get("primary", "")
      if not primary or primary not in docs_catalog:
        continue

      secondaries = [
          s
          for s in it.get("secondary", [])
          if s and s in docs_catalog and s != primary
      ]

      graded_items.append({
          "qid": it.get("qid", f"disam_{len(graded_items)+1:04d}"),
          "query": q,
          "source": "graded",
          "thread_id": tid,
          "primary": primary,
          "secondary": secondaries,
      })

  verify_no_leakage(graded_items, eval_tids, eval_qnorms)
  return graded_items


def is_english_ascii(text: str) -> bool:
  """Basic English heuristic: non-ascii ratio < 0.15."""
  if not text:
    return False
  non_ascii = len([c for c in text if ord(c) > 127])
  return (non_ascii / len(text)) < 0.15


def is_support_like(text: str) -> bool:
  """Filter out empty or ultra-short social chatter."""
  stripped = re.sub(r"@\w+", "", text).strip()
  return len(stripped.split()) >= 3


def load_silver_data(
    themes_path: Path,
    threads_path: Path,
    eval_tids: Set[str],
    eval_qnorms: Set[str],
    graded_tids: Set[str],
    docs_catalog: Dict[str, Any],
    per_article_cap: int = 80,
    seed: int = 42,
) -> List[Dict[str, Any]]:
  """Extract customer first turns from themes_v2 threads outside DEV/TEST/graded, balanced per article."""
  if not themes_path.exists():
    raise FileNotFoundError(f"Themes file not found: {themes_path}")
  if not threads_path.exists():
    raise FileNotFoundError(f"Threads file not found: {threads_path}")

  themes = json.loads(themes_path.read_text(encoding="utf-8"))
  theme_to_doc = {}
  for t in themes:
    doc_id = (
        f"faq-{t['intent_family'].replace('_', '-')}-{t['theme_id'].replace('_', '-')}"
    )
    if doc_id in docs_catalog:
      for tid in t.get("thread_ids", []):
        theme_to_doc[str(tid)] = doc_id

  excluded_tids = eval_tids | graded_tids

  silver_by_doc = {doc_id: [] for doc_id in docs_catalog}
  with open(threads_path, "r", encoding="utf-8") as f:
    for line in f:
      if not line.strip():
        continue
      t = json.loads(line)
      tid = str(t.get("thread_id", ""))
      if tid in theme_to_doc and tid not in excluded_tids:
        doc_id = theme_to_doc[tid]
        turns = t.get("turns", [])
        cust = [turn for turn in turns if turn.get("role") == "customer"]
        if cust:
          txt = cust[0].get("text", "")
          qnorm = normalize_query(txt)
          if (
              qnorm not in eval_qnorms
              and is_english_ascii(txt)
              and is_support_like(txt)
          ):
            silver_by_doc[doc_id].append({
                "thread_id": tid,
                "query": txt,
                "primary": doc_id,
                "secondary": [],
            })

  rng = random.Random(seed)
  silver_items = []
  silver_counter = 1
  for doc_id in sorted(silver_by_doc.keys()):
    items = silver_by_doc[doc_id]
    # Deterministic sampling up to per_article_cap
    if len(items) > per_article_cap:
      rng.shuffle(items)
      items = items[:per_article_cap]

    for it in items:
      silver_items.append({
          "qid": f"silver_{silver_counter:04d}",
          "query": it["query"],
          "source": "silver",
          "thread_id": it["thread_id"],
          "primary": it["primary"],
          "secondary": [],
      })
      silver_counter += 1

  verify_no_leakage(silver_items, eval_tids, eval_qnorms)
  return silver_items


def split_train_val_by_thread(
    items: List[Dict[str, Any]], val_ratio: float = 0.10, seed: int = 42
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
  """Deterministically split items into train and val by thread_id so no thread is split across splits."""
  unique_tids = sorted(list(set(str(it["thread_id"]) for it in items)))
  rng = random.Random(seed)
  shuffled_tids = list(unique_tids)
  rng.shuffle(shuffled_tids)

  n_val = max(1, int(len(shuffled_tids) * val_ratio))
  val_tids = set(shuffled_tids[:n_val])
  train_tids = set(shuffled_tids[n_val:])

  # Sanity check disjointness
  if val_tids & train_tids:
    raise ValueError("Train and val thread IDs are not disjoint")

  train_items = [it for it in items if str(it["thread_id"]) in train_tids]
  val_items = [it for it in items if str(it["thread_id"]) in val_tids]

  return train_items, val_items


def assemble_dataset_rows(
    items: List[Dict[str, Any]],
    docs_catalog: Dict[str, Any],
    intent_map: Dict[str, str],
    confusion_map: Dict[str, List[str]],
    target_k: int = 12,
    seed: int = 42,
) -> List[Dict[str, Any]]:
  """Assemble final row schema: {qid, query, source, rels, candidates}."""
  all_doc_ids = sorted(list(docs_catalog.keys()))
  rows = []

  for it in items:
    qid = it["qid"]
    # Seed RNG deterministically per query for candidate shuffling
    q_seed = (
        int(hashlib.md5(f"{seed}:{qid}".encode("utf-8")).hexdigest()[:8], 16)
        % 1000000
    )
    rng = random.Random(q_seed)

    primary = it["primary"]
    secondaries = it.get("secondary", [])
    positives = [primary] + [s for s in secondaries if s != primary]

    candidates = build_candidates_for_query(
        positives=positives,
        all_doc_ids=all_doc_ids,
        intent_map=intent_map,
        confusion_map=confusion_map,
        rng=rng,
        target_k=target_k,
    )

    rels = assign_candidate_grades(candidates, primary, secondaries)

    rows.append({
        "qid": qid,
        "query": it["query"],
        "source": it["source"],
        "thread_id": it["thread_id"],
        "rels": rels,
        "candidates": candidates,
    })

  return rows


def export_dev_candidates(
    dev_rows_path: Path, out_path: Path, config_name: str = "intent_noisy_0.2"
) -> int:
  """Export offline DEV eval candidates file without labels: {qid, query, top10}."""
  if not dev_rows_path.exists():
    raise FileNotFoundError(f"Dev rows file not found: {dev_rows_path}")

  data = json.loads(dev_rows_path.read_text(encoding="utf-8"))
  selected_rows = []
  for res in data.get("results", []):
    if res.get("config") == config_name:
      selected_rows = res.get("rows", [])
      break

  if not selected_rows:
    raise ValueError(
        f"Config '{config_name}' not found in {dev_rows_path}. Available:"
        f" {[r.get('config') for r in data.get('results', [])]}"
    )

  out_path.parent.mkdir(parents=True, exist_ok=True)
  count = 0
  with open(out_path, "w", encoding="utf-8") as f:
    for r in selected_rows:
      top10 = r.get("top10_ids") or r.get("top10", [])
      row = {"qid": r["qid"], "query": r["query"], "top10": top10}
      f.write(json.dumps(row) + "\n")
      count += 1

  return count


def generate_data_card(
    train_rows: List[Dict[str, Any]],
    val_rows: List[Dict[str, Any]],
    docs_catalog: Dict[str, Any],
    eval_tids: Set[str],
    eval_qnorms: Set[str],
) -> str:
  """Generate markdown data card documenting counts, candidate statistics, and leakage proofs."""
  all_rows = train_rows + val_rows
  total_queries = len(all_rows)
  graded_count = sum(1 for r in all_rows if r["source"] == "graded")
  silver_count = sum(1 for r in all_rows if r["source"] == "silver")

  train_graded = sum(1 for r in train_rows if r["source"] == "graded")
  train_silver = sum(1 for r in train_rows if r["source"] == "silver")
  val_graded = sum(1 for r in val_rows if r["source"] == "graded")
  val_silver = sum(1 for r in val_rows if r["source"] == "silver")

  train_tids = set(str(r["thread_id"]) for r in train_rows)
  val_tids = set(str(r["thread_id"]) for r in val_rows)

  # Check leakage
  thread_leak_dev = len((train_tids | val_tids) & eval_tids)
  all_qnorms = set(normalize_query(r["query"]) for r in all_rows)
  query_leak_dev = len(all_qnorms & eval_qnorms)
  train_val_thread_leak = len(train_tids & val_tids)

  # Candidate stats
  candidate_lens = [len(r["candidates"]) for r in all_rows]
  pos_counts = [
      sum(1 for g in r["rels"].values() if g > 0) for r in all_rows
  ]
  hard_neg_counts = [
      sum(1 for g in r["rels"].values() if g == 0) for r in all_rows
  ]

  # Per-article distribution
  article_counts = {doc_id: 0 for doc_id in docs_catalog}
  for r in all_rows:
    for doc_id, g in r["rels"].items():
      if g == 2:  # primary target
        article_counts[doc_id] += 1

  counts_list = list(article_counts.values())

  md = rf"""# PLAN7 RAG Reranker Training Data Card

- **Date**: 2026-10-25
- **Corpus Source**: Release 5 (`faq_v2p/docs`, 50 articles)
- **Target Candidates Per Query**: 12 (Positives + Hard Negatives + Randoms)

## 1. Split & Source Breakdown

| Metric | Total | Train | Validation | Description |
| :--- | :---: | :---: | :---: | :--- |
| **Total Queries** | **{total_queries}** | **{len(train_rows)}** | **{len(val_rows)}** | 90% / 10% thread-level split |
| **Graded Queries** | {graded_count} | {train_graded} | {val_graded} | Agent-labeled disambiguation pool (primary=2, secondary=1) |
| **Silver Queries** | {silver_count} | {train_silver} | {val_silver} | Capped customer first turns from themes_v2 (target=2, other=0) |
| **Unique Threads** | {len(train_tids | val_tids)} | {len(train_tids)} | {len(val_tids)} | Disjoint thread partition |
| **Corpus Articles** | {len(docs_catalog)} | 50 | 50 | 100% Release 5 coverage |

## 2. Per-Article Primary Target Balance

- **Min Queries / Article**: {min(counts_list)}
- **Max Queries / Article**: {max(counts_list)}
- **Mean Queries / Article**: {sum(counts_list)/len(counts_list):.1f}
- **Median Queries / Article**: {sorted(counts_list)[len(counts_list)//2]}

## 3. Candidate Composition & Label Distribution

- **Mean Candidates / Query**: {sum(candidate_lens)/len(candidate_lens):.1f} (min={min(candidate_lens)}, max={max(candidate_lens)})
- **Mean Positives / Query (Grade $\ge$ 1)**: {sum(pos_counts)/len(pos_counts):.2f}
- **Mean Negatives / Query (Grade 0)**: {sum(hard_neg_counts)/len(hard_neg_counts):.2f}
- **Graded Relevance Levels**:
  - `Grade 2`: Primary relevant FAQ document
  - `Grade 1`: Secondary relevant FAQ document
  - `Grade 0`: Hard negative or non-relevant candidate document

## 4. Leakage & Split Invariant Proofs

| Invariant | Value | Status | Acceptance Criterion |
| :--- | :---: | :---: | :--- |
| **Thread Overlap with DEV/TEST** | **{thread_leak_dev}** | **PASSED** | Must be strictly 0 |
| **Normalized Query Overlap with DEV/TEST** | **{query_leak_dev}** | **PASSED** | Must be strictly 0 |
| **Thread Overlap between Train and Val** | **{train_val_thread_leak}** | **PASSED** | Must be strictly 0 |
| **Doc Text Release 5 Conformance** | **50 / 50** | **PASSED** | 100% extracted from Release 5 `$S/faq_v2p/docs/` |
| **Offline DEV Eval File Leakage** | **0 labels** | **PASSED** | `dev_candidates.jsonl` contains zero DEV labels |

## 5. File Manifest

- `docs.json`: Formatted article catalog `{{"doc_id": {{"title", "also_asked", "answer_prefix", "text"}}}}`
- `train.jsonl`: Training queries `{{"qid", "query", "source", "rels", "candidates"}}`
- `val.jsonl`: Validation queries `{{"qid", "query", "source", "rels", "candidates"}}`
- `dev_candidates.jsonl`: Unlabeled offline DEV candidates `{{"qid", "query", "top10"}}`
"""
  return md


def main():
  parser = argparse.ArgumentParser(
      description="Build RAG reranker training and validation dataset."
  )
  parser.add_argument(
      "--scratch-dir",
      type=Path,
      default=Path(
          "/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag"
      ),
      help="Scratch directory path ($S)",
  )
  parser.add_argument(
      "--docs-dir",
      type=Path,
      default=None,
      help="Release 5 docs directory ($S/faq_v2p/docs)",
  )
  parser.add_argument(
      "--eval-dir",
      type=Path,
      default=None,
      help="Eval directory containing dev.jsonl and test.jsonl",
  )
  parser.add_argument(
      "--graded-path",
      type=Path,
      default=Path(
          "rag/corpus_v2/agent_decisions/disambiguation_pool_labels.jsonl"
      ),
      help="Graded disambiguation pool labels JSONL",
  )
  parser.add_argument(
      "--themes-path",
      type=Path,
      default=None,
      help="Path to themes_v2.json ($S/faq_v2p/themes_v2.json)",
  )
  parser.add_argument(
      "--threads-path",
      type=Path,
      default=None,
      help="Path to threads.jsonl ($S/threads.jsonl)",
  )
  parser.add_argument(
      "--dev-rows-path",
      type=Path,
      default=None,
      help="Path to dev_rows_phaseD.json ($S/ablation/dev_rows_phaseD.json)",
  )
  parser.add_argument(
      "--out-dir",
      type=Path,
      default=None,
      help="Output directory for rerank dataset ($S/rerank)",
  )
  parser.add_argument(
      "--silver-cap",
      type=int,
      default=80,
      help="Per-article cap for silver first turns",
  )
  parser.add_argument(
      "--val-ratio",
      type=float,
      default=0.10,
      help="Validation ratio by thread split",
  )
  parser.add_argument(
      "--target-k", type=int, default=12, help="Target candidates per query"
  )
  parser.add_argument(
      "--seed", type=int, default=42, help="Deterministic random seed"
  )

  args = parser.parse_args()

  s = args.scratch_dir
  docs_dir = args.docs_dir or (s / "faq_v2p" / "docs")
  eval_dir = args.eval_dir or (s / "eval")
  themes_path = args.themes_path or (s / "faq_v2p" / "themes_v2.json")
  threads_path = args.threads_path or (s / "threads.jsonl")
  dev_rows_path = args.dev_rows_path or (
      s / "ablation" / "dev_rows_phaseD.json"
  )
  out_dir = args.out_dir or (s / "rerank")

  out_dir.mkdir(parents=True, exist_ok=True)

  print(f"Loading docs from: {docs_dir}")
  docs_catalog = build_docs_catalog(docs_dir)
  intent_map = build_intent_family_map(docs_catalog)
  confusion_map = build_confusion_map()

  print(f"Loading eval leakage sets from: {eval_dir}")
  eval_tids, eval_qnorms = load_eval_leakage_sets(
      eval_dir / "dev.jsonl", eval_dir / "test.jsonl"
  )
  print(f"  Eval threads: {len(eval_tids)}, Eval queries: {len(eval_qnorms)}")

  print(f"Loading graded data from: {args.graded_path}")
  graded_items = load_graded_data(
      args.graded_path, eval_tids, eval_qnorms, docs_catalog
  )
  graded_tids = set(str(it["thread_id"]) for it in graded_items)
  print(f"  Graded items: {len(graded_items)}")

  print(f"Loading silver data from: {themes_path}")
  silver_items = load_silver_data(
      themes_path=themes_path,
      threads_path=threads_path,
      eval_tids=eval_tids,
      eval_qnorms=eval_qnorms,
      graded_tids=graded_tids,
      docs_catalog=docs_catalog,
      per_article_cap=args.silver_cap,
      seed=args.seed,
  )
  print(f"  Silver items: {len(silver_items)}")

  all_items = graded_items + silver_items
  print(f"Total pool items: {len(all_items)}")

  print(f"Splitting train/val by thread (val_ratio={args.val_ratio})...")
  train_items, val_items = split_train_val_by_thread(
      all_items, val_ratio=args.val_ratio, seed=args.seed
  )
  print(f"  Train items: {len(train_items)}, Val items: {len(val_items)}")

  print(f"Assembling candidate sets (target_k={args.target_k})...")
  train_rows = assemble_dataset_rows(
      train_items,
      docs_catalog,
      intent_map,
      confusion_map,
      target_k=args.target_k,
      seed=args.seed,
  )
  val_rows = assemble_dataset_rows(
      val_items,
      docs_catalog,
      intent_map,
      confusion_map,
      target_k=args.target_k,
      seed=args.seed + 1,
  )

  # Write docs.json
  docs_out = out_dir / "docs.json"
  docs_out.write_text(json.dumps(docs_catalog, indent=2), encoding="utf-8")
  print(f"Saved docs catalog to {docs_out}")

  # Write train.jsonl
  train_out = out_dir / "train.jsonl"
  with open(train_out, "w", encoding="utf-8") as f:
    for r in train_rows:
      f.write(json.dumps(r) + "\n")
  print(f"Saved {len(train_rows)} train rows to {train_out}")

  # Write val.jsonl
  val_out = out_dir / "val.jsonl"
  with open(val_out, "w", encoding="utf-8") as f:
    for r in val_rows:
      f.write(json.dumps(r) + "\n")
  print(f"Saved {len(val_rows)} val rows to {val_out}")

  # Export dev candidates
  dev_out = out_dir / "dev_candidates.jsonl"
  dev_count = export_dev_candidates(dev_rows_path, dev_out)
  print(f"Saved {dev_count} dev candidate rows to {dev_out}")

  # Generate and save data card
  data_card_md = generate_data_card(
      train_rows, val_rows, docs_catalog, eval_tids, eval_qnorms
  )
  card_out = out_dir / "data_card.md"
  card_out.write_text(data_card_md, encoding="utf-8")
  print(f"Saved data card to {card_out}")
  print("\n" + data_card_md)


if __name__ == "__main__":
  main()
