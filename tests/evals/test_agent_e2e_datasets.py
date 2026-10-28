"""Tests for end-to-end evaluation dataset generation and leakage prevention.

# REQ-EVAL-001: Deterministic Multi-Source Dataset Construction & Stratification
# REQ-EVAL-002: Offline-Authored Adversarial Tone & Priority Gaming Set
# REQ-EVAL-003: Leakage Prevention & DEV/TEST Partition Discipline
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "evals" / "data"
AGENT_E2E_DIR = DATA_DIR / "agent_e2e_v1"
ADVERSARIAL_FILE = DATA_DIR / "adversarial_tone_v1.jsonl"
DEV_FILE = AGENT_E2E_DIR / "dev.jsonl"
TEST_FILE = AGENT_E2E_DIR / "test.jsonl"


def test_adversarial_tone_set_properties():
    # REQ-EVAL-002
    assert ADVERSARIAL_FILE.is_file(), f"Missing {ADVERSARIAL_FILE}"

    records = []
    with open(ADVERSARIAL_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))

    assert len(records) == 160, f"Expected 160 cases (40 seeds x 4 variants), got {len(records)}"

    seeds = set(r["seed_id"] for r in records)
    assert len(seeds) == 40, f"Expected 40 seeds, got {len(seeds)}"

    # Check 4 variants per seed
    variants_per_seed = Counter(r["seed_id"] for r in records)
    for s_id, count in variants_per_seed.items():
        assert count == 4, f"Seed {s_id} has {count} variants; expected 4"

    # Split strictly by seed
    dev_seeds = set(r["seed_id"] for r in records if r["partition"] == "dev")
    test_seeds = set(r["seed_id"] for r in records if r["partition"] == "test")

    assert len(dev_seeds) == 20, f"Expected 20 DEV seeds, got {len(dev_seeds)}"
    assert len(test_seeds) == 20, f"Expected 20 TEST seeds, got {len(test_seeds)}"
    assert not (dev_seeds & test_seeds), "Seeds must not overlap between DEV and TEST partitions"

    # Check required variants exist
    for r in records:
        assert r["variant"] in ("calm", "fake-urgent", "angry", "abusive")
        if r["variant"] in ("angry", "abusive"):
            assert r["expected_escalate"] is True, f"Variant {r['variant']} must skip automation"
        if r["variant"] == "abusive":
            assert r["expected_boundary_ack"] is True, "Abusive variant must receive boundary ack"


def test_e2e_dataset_build_deterministic():
    # REQ-EVAL-001
    assert DEV_FILE.is_file(), f"Missing DEV dataset at {DEV_FILE}"
    assert TEST_FILE.is_file(), f"Missing TEST dataset at {TEST_FILE}"

    dev_cases = []
    with open(DEV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                dev_cases.append(json.loads(line))

    test_cases = []
    with open(TEST_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                test_cases.append(json.loads(line))

    assert len(dev_cases) == 534, f"Expected 534 DEV cases, got {len(dev_cases)}"
    assert len(test_cases) == 534, f"Expected 534 TEST cases, got {len(test_cases)}"

    # Verify breakdown of DEV components:
    # 300 retrieval, 150 router golden, 4 identity, 80 adversarial
    dev_sources = Counter(c["source"] for c in dev_cases)
    assert dev_sources["retrieval_v1"] == 300
    assert dev_sources["router_golden_v1"] == 150
    assert dev_sources["customer_identity"] == 4
    assert dev_sources["adversarial_tone_v1"] == 80

    # Verify breakdown of TEST components:
    test_sources = Counter(c["source"] for c in test_cases)
    assert test_sources["retrieval_v1"] == 300
    assert test_sources["router_golden_v1"] == 150
    assert test_sources["customer_identity"] == 4
    assert test_sources["adversarial_tone_v1"] == 80

    # Verify router golden stratification across both partitions
    dev_strata = Counter(c["stratum"] for c in dev_cases if c["source"] == "router_golden_v1")
    test_strata = Counter(c["stratum"] for c in test_cases if c["source"] == "router_golden_v1")

    assert dev_strata["strict"] == 45 and test_strata["strict"] == 45
    assert dev_strata["split"] == 38 and test_strata["split"] == 37
    assert dev_strata["rest"] == 67 and test_strata["rest"] == 68

    # Verify golden labels are tagged as teacher/LLM consensus
    for c in dev_cases + test_cases:
        if c["source"] == "router_golden_v1":
            assert c.get("golden_source") == "teacher_llm_consensus"
            assert c.get("human_review_status") == "pending"


def test_dev_test_strict_isolation_no_leakage():
    # REQ-EVAL-003
    with open(DEV_FILE, "r", encoding="utf-8") as f:
        dev_cases = [json.loads(line) for line in f if line.strip()]

    with open(TEST_FILE, "r", encoding="utf-8") as f:
        test_cases = [json.loads(line) for line in f if line.strip()]

    # Case IDs must be completely disjoint
    dev_case_ids = {c["case_id"] for c in dev_cases}
    test_case_ids = {c["case_id"] for c in test_cases}
    # Identity cases share same regression probe IDs intentionally
    non_ident_overlap = (dev_case_ids & test_case_ids) - {
        "IDENT-001-own-order",
        "IDENT-002-cross-order-bola",
        "IDENT-003-unauth-order",
        "IDENT-004-unauth-angry-urgent",
    }
    assert not non_ident_overlap, f"Case ID leakage: {non_ident_overlap}"

    # Normalized message texts must not overlap outside identity probes
    dev_texts = {c["message"].strip().lower() for c in dev_cases if c["source"] != "customer_identity"}
    test_texts = {c["message"].strip().lower() for c in test_cases if c["source"] != "customer_identity"}
    overlap_texts = dev_texts & test_texts
    assert not overlap_texts, f"Query text leakage between DEV and TEST ({len(overlap_texts)} cases): {list(overlap_texts)[:3]}"
