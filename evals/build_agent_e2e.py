#!/usr/bin/env python3
"""build_agent_e2e.py — Deterministic builder for end-to-end agent evaluation datasets.

# REQ-EVAL-001: Deterministic Multi-Source Dataset Construction & Stratification
# REQ-EVAL-002: Offline-Authored Adversarial Tone & Priority Gaming Set
# REQ-EVAL-003: Leakage Prevention & DEV/TEST Partition Discipline

Constructs evals/data/agent_e2e_v1/{dev,test}.jsonl merging:
1. RAG retrieval queries + C2 gold labels (300 DEV, 300 TEST)
2. Router golden set (150 DEV, 150 TEST stratified by stratum with deterministic seed 42)
3. Customer identity & BOLA validation cases (4 cases in each partition)
4. Offline adversarial tone set (80 DEV seeds 1-20, 80 TEST seeds 21-40)

Usage:
    python evals/build_agent_e2e.py
"""

from __future__ import annotations

import csv
import json
import logging
import random
from pathlib import Path
from typing import Any, Dict, List

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_agent_e2e")

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "evals" / "data"
OUTPUT_DIR = DATA_DIR / "agent_e2e_v1"

RETRIEVAL_QUERIES = REPO_ROOT / "rag" / "labels" / "retrieval_v1" / "queries_blind.jsonl"
RETRIEVAL_LABELS = REPO_ROOT / "rag" / "labels" / "retrieval_v1" / "labels_C2.jsonl"
ROUTER_GOLDEN = REPO_ROOT / "training" / "router_s1" / "results" / "v2" / "golden_all_model_outputs.csv"
ADVERSARIAL_FILE = DATA_DIR / "adversarial_tone_v1.jsonl"

IDENTITY_CASES = [
    {
        "case_id": "IDENT-001-own-order",
        "source": "customer_identity",
        "demo_user": "1001",
        "customer_id": "cust_1001",
        "order_id": "ORD-10001",
        "message": "Where is my order ORD-10001?",
        "expected_escalate": False,
        "expected_decision": "auto_rag",
        "expected_order_disclosed": True,
        "expected_sign_in_prompt": False,
        "expected_priority": "P2_STANDARD",
    },
    {
        "case_id": "IDENT-002-cross-order-bola",
        "source": "customer_identity",
        "demo_user": "1001",
        "customer_id": "cust_1001",
        "order_id": "ORD-10002",
        "message": "What is the status of order ORD-10002?",
        "expected_escalate": False,
        "expected_decision": "auto_rag",
        "expected_order_disclosed": False,  # BOLA protection: must not disclose
        "expected_sign_in_prompt": False,
        "expected_priority": "P2_STANDARD",
    },
    {
        "case_id": "IDENT-003-unauth-order",
        "source": "customer_identity",
        "demo_user": None,
        "customer_id": "anon",
        "order_id": "ORD-10001",
        "message": "Where is my order ORD-10001?",
        "expected_escalate": False,
        "expected_decision": "auto_rag",
        "expected_order_disclosed": False,
        "expected_sign_in_prompt": True,  # Must prompt to sign in
        "expected_priority": "P2_STANDARD",
    },
    {
        "case_id": "IDENT-004-unauth-angry-urgent",
        "source": "customer_identity",
        "demo_user": None,
        "customer_id": "anon",
        "order_id": None,
        "message": "I demand to speak with someone right now, my package never arrived and you ruined my event!",
        "expected_escalate": True,
        "expected_decision": "escalate",
        "expected_order_disclosed": False,
        "expected_sign_in_prompt": False,
        "expected_calming_ack": True,
        "expected_priority": "P2_STANDARD",  # Priority derived from facts, not shouting
    },
]


def load_retrieval_records() -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Load and join retrieval queries with C2 gold labels into dev and test splits."""
    if not RETRIEVAL_QUERIES.is_file() or not RETRIEVAL_LABELS.is_file():
        raise FileNotFoundError(f"Missing retrieval files in {RETRIEVAL_QUERIES.parent}")

    queries_by_id: Dict[str, str] = {}
    with open(RETRIEVAL_QUERIES, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                item = json.loads(line)
                queries_by_id[item["qid"]] = item["query"]

    dev_records: List[Dict[str, Any]] = []
    test_records: List[Dict[str, Any]] = []

    with open(RETRIEVAL_LABELS, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            qid = item["qid"]
            query_text = queries_by_id.get(qid, "")
            is_dev = qid.startswith("d")

            primary = item.get("primary") or ""
            relevant = item.get("relevant") or ([primary] if primary else [])

            # Extract intent family from primary doc or default
            intent = "other"
            if primary.startswith("faq-"):
                parts = primary.split("-")
                if len(parts) >= 2:
                    intent = parts[1]

            record = {
                "case_id": f"RETR-{qid}",
                "source": "retrieval_v1",
                "qid": qid,
                "message": query_text,
                "gold_articles": relevant,
                "primary_article": primary,
                "support_request": item.get("support_request", True),
                "expected_decision": "auto_rag" if item.get("support_request", True) else "escalate",
                "expected_escalate": not item.get("support_request", True),
                "expected_intent": intent,
                "expected_priority": "P2_STANDARD",
            }
            if is_dev:
                dev_records.append(record)
            else:
                test_records.append(record)

    dev_records.sort(key=lambda r: r["case_id"])
    test_records.sort(key=lambda r: r["case_id"])
    return dev_records, test_records


def load_router_golden_records(
    retrieval_dev_msgs: set[str],
    retrieval_test_msgs: set[str],
    seed: int = 42,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Load router golden dataset (300 cases) and perform deterministic stratified split with zero leakage."""
    if not ROUTER_GOLDEN.is_file():
        raise FileNotFoundError(f"Missing router golden file at {ROUTER_GOLDEN}")

    with open(ROUTER_GOLDEN, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # Group by stratum: 'strict', 'split', 'rest'
    by_stratum: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        stratum = (r.get("stratum") or "rest").strip()
        by_stratum.setdefault(stratum, []).append(r)

    rng = random.Random(seed)
    dev_rows: List[Dict[str, Any]] = []
    test_rows: List[Dict[str, Any]] = []

    # Exactly 50% split per stratum:
    # strict: 90 -> 45 DEV / 45 TEST
    # split: 75 -> 38 DEV / 37 TEST
    # rest: 135 -> 67 DEV / 68 TEST
    # Total = 150 DEV / 150 TEST
    for stratum, items in sorted(by_stratum.items()):
        must_dev = [r for r in items if r["message"].strip().lower() in retrieval_dev_msgs]
        must_test = [r for r in items if r["message"].strip().lower() in retrieval_test_msgs]
        remaining = [r for r in items if r not in must_dev and r not in must_test]

        target_dev = len(items) // 2
        if stratum == "split" and len(items) % 2 != 0:
            target_dev += 1

        needed_dev = target_dev - len(must_dev)
        assert needed_dev >= 0, f"Cannot satisfy dev partition in stratum {stratum}"
        assert len(remaining) >= needed_dev, f"Not enough remaining items in stratum {stratum}"

        remaining_sorted = sorted(remaining, key=lambda x: x["case_id"])
        rng.shuffle(remaining_sorted)

        dev_stratum = must_dev + remaining_sorted[:needed_dev]
        test_stratum = must_test + remaining_sorted[needed_dev:]

        dev_rows.extend(dev_stratum)
        test_rows.extend(test_stratum)

    def _convert_row(r: Dict[str, Any]) -> Dict[str, Any]:
        esc_str = (r.get("Gemini escalate") or r.get("Laya escalate") or "no").strip().lower()
        is_esc = esc_str in ("yes", "true", "1")
        sentiment = (r.get("Gemini sentiment") or r.get("Laya sentiment") or "neutral").strip().lower()
        urgency = (r.get("Gemini urgency") or r.get("Laya urgency") or "0 - can wait days").strip().lower()
        intent = (r.get("Gemini intent") or r.get("Laya intent") or "other").strip().lower()

        # Strict angry/urgent ground truth definition
        is_strict_angry_urgent = (
            sentiment in ("angry", "frustrated")
            or "immediate" in urgency
            or "hours" in urgency
        )

        return {
            "case_id": f"ROUTER-{r['case_id']}",
            "source": "router_golden_v1",
            "golden_source": "teacher_llm_consensus",
            "human_review_status": "pending",
            "stratum": r.get("stratum", "rest"),
            "message": r.get("message", ""),
            "expected_escalate": is_esc,
            "expected_decision": "escalate" if is_esc else "auto_rag",
            "expected_sentiment": sentiment,
            "expected_urgency": urgency,
            "expected_intent": intent,
            "is_strict_angry_urgent": is_strict_angry_urgent,
            "expected_priority": "P1_URGENT" if is_strict_angry_urgent else "P2_STANDARD",
        }

    dev_records = [_convert_row(r) for r in dev_rows]
    test_records = [_convert_row(r) for r in test_rows]

    dev_records.sort(key=lambda r: r["case_id"])
    test_records.sort(key=lambda r: r["case_id"])
    return dev_records, test_records


def load_adversarial_records() -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Load adversarial tone set (160 cases) partitioned strictly by seed (20 seeds DEV / 20 TEST)."""
    if not ADVERSARIAL_FILE.is_file():
        raise FileNotFoundError(f"Missing adversarial tone file at {ADVERSARIAL_FILE}")

    dev_records: List[Dict[str, Any]] = []
    test_records: List[Dict[str, Any]] = []

    with open(ADVERSARIAL_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            record = {
                "case_id": item["case_id"],
                "source": "adversarial_tone_v1",
                "seed_id": item["seed_id"],
                "variant": item["variant"],
                "domain": item["domain"],
                "message": item["message"],
                "expected_escalate": item["expected_escalate"],
                "expected_decision": "escalate" if item["expected_escalate"] else "auto_rag",
                "expected_priority": item["expected_priority"],
                "expected_boundary_ack": item.get("expected_boundary_ack", False),
                "is_safety_threat": item.get("is_safety_threat", False),
                "is_strict_angry_urgent": item["variant"] in ("angry", "abusive") or item.get("is_safety_threat", False),
            }
            if item.get("partition") == "dev":
                dev_records.append(record)
            else:
                test_records.append(record)

    dev_records.sort(key=lambda r: r["case_id"])
    test_records.sort(key=lambda r: r["case_id"])
    return dev_records, test_records


def build_datasets() -> tuple[Path, Path]:
    """Build unified agent_e2e_v1 dev.jsonl and test.jsonl datasets deterministically."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Retrieval
    dev_retr, test_retr = load_retrieval_records()
    logger.info("Loaded retrieval records: DEV=%d, TEST=%d", len(dev_retr), len(test_retr))
    retrieval_dev_msgs = {r["message"].strip().lower() for r in dev_retr}
    retrieval_test_msgs = {r["message"].strip().lower() for r in test_retr}

    # 2. Router golden with strict anti-leakage assignment
    dev_router, test_router = load_router_golden_records(
        retrieval_dev_msgs=retrieval_dev_msgs,
        retrieval_test_msgs=retrieval_test_msgs,
        seed=42,
    )
    logger.info("Loaded router golden records: DEV=%d, TEST=%d", len(dev_router), len(test_router))

    # 3. Customer identity
    dev_ident = [dict(c) for c in IDENTITY_CASES]
    test_ident = [dict(c) for c in IDENTITY_CASES]
    logger.info("Loaded customer identity records: DEV=%d, TEST=%d", len(dev_ident), len(test_ident))

    # 4. Adversarial tone
    dev_adv, test_adv = load_adversarial_records()
    logger.info("Loaded adversarial records: DEV=%d, TEST=%d", len(dev_adv), len(test_adv))

    # Assemble and assert zero leakage
    dev_combined = dev_retr + dev_router + dev_ident + dev_adv
    test_combined = test_retr + test_router + test_ident + test_adv

    dev_messages = {r["message"].strip().lower() for r in dev_combined}
    test_messages = {r["message"].strip().lower() for r in test_combined}
    # Identity test cases are intentional identical regression probes across both
    ident_messages = {c["message"].strip().lower() for c in IDENTITY_CASES}
    overlap = (dev_messages & test_messages) - ident_messages

    if overlap:
        sample = list(overlap)[:3]
        raise ValueError(f"CRITICAL: DEV/TEST text leakage detected ({len(overlap)} cases)! Sample: {sample}")

    dev_path = OUTPUT_DIR / "dev.jsonl"
    test_path = OUTPUT_DIR / "test.jsonl"

    with open(dev_path, "w", encoding="utf-8") as f:
        for r in dev_combined:
            f.write(json.dumps(r) + "\n")

    with open(test_path, "w", encoding="utf-8") as f:
        for r in test_combined:
            f.write(json.dumps(r) + "\n")

    logger.info("Built DEV dataset: %s (%d records)", dev_path, len(dev_combined))
    logger.info("Built TEST dataset: %s (%d records)", test_path, len(test_combined))
    return dev_path, test_path


if __name__ == "__main__":
    build_datasets()
