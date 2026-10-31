"""Evaluation dataset loader and message sampler for synthetic load testing.

# REQ-LOAD-001: Locust Load Testing Profiles and Cost/Quota Safeguards
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("loadtest.dataset")

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class LoadTestCase:
    """Single test case representing an inbound user turn."""

    case_id: str
    category: str
    message: str
    oauth: bool = False
    demo_user: Optional[str] = None
    expected_decision: Optional[str] = None


# Fallback deterministic cases if evaluation dataset files are missing
FALLBACK_CASES: List[LoadTestCase] = [
    # FAQ (~40%)
    LoadTestCase("faq_01", "faq", "What is your standard return policy?", oauth=False, expected_decision="auto_rag"),
    LoadTestCase("faq_02", "faq", "How long does standard shipping take?", oauth=False, expected_decision="auto_rag"),
    LoadTestCase("faq_03", "faq", "How can I reset my password?", oauth=False, expected_decision="auto_rag"),
    LoadTestCase("faq_04", "faq", "Do you offer international shipping to APO addresses?", oauth=False, expected_decision="auto_rag"),
    # Order Status (~30%)
    LoadTestCase("order_01", "order_auth", "Where is my order ORD-10001?", oauth=True, demo_user="1001", expected_decision="auto_rag"),
    LoadTestCase("order_02", "order_auth", "What did I buy recently?", oauth=True, demo_user="1001", expected_decision="auto_rag"),
    LoadTestCase("order_03", "order_anon", "Where is my package?", oauth=False, expected_decision="auto_rag"),
    # Escalation & Angry (~20%)
    LoadTestCase("esc_01", "escalate_angry", "This is completely unacceptable! My order is two days late and customer service is impossible!", oauth=False, expected_decision="escalate"),
    LoadTestCase("esc_02", "escalate_angry", "I want to speak with a human agent right now.", oauth=False, expected_decision="escalate"),
    # Greetings / Out of Scope (~10%)
    LoadTestCase("greet_01", "greeting", "hello there", oauth=False, expected_decision="auto_rag"),
    LoadTestCase("oos_01", "oos", "what is the current weather in Paris?", oauth=False, expected_decision="reject_oos"),
]


class LoadTestDataset:
    """Manages weighted sampling across FAQ, order, escalation, and conversational intents."""

    # Target operational message mix
    DEFAULT_WEIGHTS = {
        "faq": 0.40,
        "order_auth": 0.20,
        "order_anon": 0.10,
        "escalate_angry": 0.20,
        "greeting": 0.05,
        "oos": 0.05,
    }

    def __init__(self, repo_root: Optional[Path] = None):
        self.repo_root = repo_root or REPO_ROOT
        self.cases_by_category: dict[str, list[LoadTestCase]] = {
            "faq": [],
            "order_auth": [],
            "order_anon": [],
            "escalate_angry": [],
            "greeting": [],
            "oos": [],
        }
        self._load_datasets()

    def _load_datasets(self) -> None:
        """Load cases from latency cases, agent_e2e, and adversarial_tone datasets."""
        loaded_count = 0

        # 1. Load from evals/latency/cases_v1.jsonl
        latency_file = self.repo_root / "evals" / "latency" / "cases_v1.jsonl"
        if latency_file.is_file():
            try:
                with open(latency_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        d = json.loads(line)
                        path = d.get("path", "")
                        oauth = bool(d.get("oauth", False))
                        demo_user = "1001" if oauth else None
                        msg = d.get("message", "")
                        cid = d.get("case_id", f"case_{loaded_count}")
                        exp_dec = d.get("expected_decision")

                        cat = "faq"
                        if "policy_faq" in path:
                            cat = "faq"
                        elif "order" in path and oauth:
                            cat = "order_auth"
                        elif "order" in path:
                            cat = "order_anon"
                        elif "escalation" in path:
                            cat = "escalate_angry"
                        elif "greeting" in path or "pleasantry" in path:
                            cat = "greeting"
                        elif "out_of_scope" in path:
                            cat = "oos"

                        self.cases_by_category[cat].append(
                            LoadTestCase(cid, cat, msg, oauth, demo_user, exp_dec)
                        )
                        loaded_count += 1
            except Exception as exc:
                logger.warning("Failed loading %s: %s", latency_file, exc)

        # 2. Load angry/urgent cases from evals/data/adversarial_tone_v1.jsonl
        adv_file = self.repo_root / "evals" / "data" / "adversarial_tone_v1.jsonl"
        if adv_file.is_file():
            try:
                with open(adv_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        d = json.loads(line)
                        if d.get("variant") == "angry" or d.get("expected_escalate"):
                            cid = d.get("case_id", f"adv_{loaded_count}")
                            msg = d.get("message", "")
                            self.cases_by_category["escalate_angry"].append(
                                LoadTestCase(cid, "escalate_angry", msg, oauth=False, demo_user=None, expected_decision="escalate")
                            )
                            loaded_count += 1
            except Exception as exc:
                logger.warning("Failed loading %s: %s", adv_file, exc)

        # If any category is empty, populate from FALLBACK_CASES
        for fallback in FALLBACK_CASES:
            if not self.cases_by_category[fallback.category]:
                self.cases_by_category[fallback.category].append(fallback)

        total_loaded = sum(len(v) for v in self.cases_by_category.values())
        logger.info("Loaded %d load test cases across %d categories", total_loaded, len(self.cases_by_category))

    def sample(self, rng: Optional[random.Random] = None) -> LoadTestCase:
        """Sample a single test case adhering to the target category weights."""
        r = rng or random
        categories = list(self.DEFAULT_WEIGHTS.keys())
        weights = [self.DEFAULT_WEIGHTS[c] for c in categories]

        chosen_cat = r.choices(categories, weights=weights, k=1)[0]
        pool = self.cases_by_category.get(chosen_cat) or self.cases_by_category["faq"]
        return r.choice(pool)

    def total_cases(self) -> int:
        return sum(len(v) for v in self.cases_by_category.values())
