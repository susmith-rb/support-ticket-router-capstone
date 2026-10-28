#!/usr/bin/env python3
"""agent_judge_ingest.py — LLM-as-a-Judge packet exporter, score ingest, and calibration validator.

# REQ-EVAL-007: Anchored 5/3/1 LLM Judge Rubric & Offline Subagent Evaluation

Implements:
1. Export of evaluation trace answers into judging packets (evals/judge/packets_*.jsonl).
2. Offline scoring ingest for Groundedness, Helpfulness & Empathy, and Calming Ack Tone.
3. Cohen's Kappa calculation for inter-annotator agreement and calibration against anchors.
4. Validation against 20-case calibration anchors asserting kappa >= 0.70.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agent_judge_ingest")

REPO_ROOT = Path(__file__).resolve().parents[1]
JUDGE_DIR = REPO_ROOT / "evals" / "judge"
CALIBRATION_ANCHORS_FILE = JUDGE_DIR / "calibration_anchors.jsonl"

# Anchored 5/3/1 rubrics from TDD §7.3
RUBRICS: Dict[str, Dict[int, str]] = {
    "groundedness": {
        5: "Every factual claim (windows, fees, eligibility) is supported by cited chunks; no claims beyond sources; cites most specific article.",
        3: "Claims are supported but one citation is weak or a minor detail is unsupported.",
        1: "Any policy claim not present in cited sources, or a citation that does not contain quoted text.",
    },
    "helpfulness": {
        5: "Directly answers in <=120 words, acknowledges situation in one sentence without over-apologizing, gives next concrete step, offers human when relevant.",
        3: "Correct answer but generic tone, or buries answer under policy text, or misses next step.",
        1: "Wrong, evasive, or tone-deaf; or asks customer to repeat info already in message.",
    },
    "calming_ack": {
        5: "Names problem in customer terms, human specialist has ticket with ticket ID + ETA, no promises about outcomes, no filler, <=60 words.",
        3: "Appropriate but generic; missing ETA or ticket ID; slightly long.",
        1: "Defensive, dismissive, over-promising ('we will refund you'), or asks angry customer to try FAQ.",
    },
}


@dataclass
class JudgePacket:
    packet_id: str
    case_id: str
    dimension: str
    query: str
    response: str
    context: str
    rubric_guidelines: Dict[int, str]
    gold_score: Optional[int] = None
    judge_score: Optional[int] = None
    reasoning: Optional[str] = None


def export_judge_packets(traces_path: Path, output_path: Path) -> int:
    """Export answers from evaluation traces into standardized judge packets."""
    packets: List[JudgePacket] = []
    if not traces_path.is_file():
        raise FileNotFoundError(f"Traces file not found at {traces_path}")

    with open(traces_path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f, 1):
            if not line.strip():
                continue
            trace = json.loads(line)
            case_id = trace.get("case_id", f"case-{idx}")
            message = trace.get("input_message", "")
            answer = trace.get("answer", "")
            escalate = bool(trace.get("escalate", False))

            if not answer:
                continue

            if escalate or "human" in trace.get("queue", ""):
                # Calming Ack evaluation
                packets.append(
                    JudgePacket(
                        packet_id=f"PACKET-{case_id}-calming",
                        case_id=case_id,
                        dimension="calming_ack",
                        query=message,
                        response=answer,
                        context="",
                        rubric_guidelines=RUBRICS["calming_ack"],
                    )
                )
            else:
                # Factual answer evaluation: Groundedness + Helpfulness
                citations_str = json.dumps(trace.get("citations", []))
                packets.append(
                    JudgePacket(
                        packet_id=f"PACKET-{case_id}-groundedness",
                        case_id=case_id,
                        dimension="groundedness",
                        query=message,
                        response=answer,
                        context=citations_str,
                        rubric_guidelines=RUBRICS["groundedness"],
                    )
                )
                packets.append(
                    JudgePacket(
                        packet_id=f"PACKET-{case_id}-helpfulness",
                        case_id=case_id,
                        dimension="helpfulness",
                        query=message,
                        response=answer,
                        context="",
                        rubric_guidelines=RUBRICS["helpfulness"],
                    )
                )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for p in packets:
            f.write(json.dumps(asdict(p)) + "\n")

    logger.info("Exported %d judge packets to %s", len(packets), output_path)
    return len(packets)


def compute_cohens_kappa(rater1: List[int], rater2: List[int]) -> float:
    """Compute Cohen's Kappa coefficient between two raters on discrete categories (e.g. 1, 3, 5)."""
    if len(rater1) != len(rater2) or not rater1:
        return 0.0

    categories = sorted(list(set(rater1) | set(rater2)))
    n = len(rater1)

    # Observed agreement
    agree_count = sum(1 for a, b in zip(rater1, rater2) if a == b)
    po = agree_count / n

    # Expected chance agreement
    pe = 0.0
    for cat in categories:
        p1 = sum(1 for a in rater1 if a == cat) / n
        p2 = sum(1 for b in rater2 if b == cat) / n
        pe += p1 * p2

    if pe == 1.0:
        return 1.0
    kappa = (po - pe) / (1.0 - pe)
    return kappa


def ingest_judge_scores(scored_packets_path: Path) -> Dict[str, Any]:
    """Ingest scored packets and compute aggregated qualitative metrics."""
    if not scored_packets_path.is_file():
        raise FileNotFoundError(f"Scored packets not found at {scored_packets_path}")

    scores_by_dim: Dict[str, List[int]] = {
        "groundedness": [],
        "helpfulness": [],
        "calming_ack": [],
    }

    with open(scored_packets_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            dim = item.get("dimension")
            score = item.get("judge_score") or item.get("score")
            if dim in scores_by_dim and score is not None:
                scores_by_dim[dim].append(int(score))

    summary: Dict[str, Any] = {}
    for dim, scores in scores_by_dim.items():
        if scores:
            mean_s = sum(scores) / len(scores)
            count_5 = sum(1 for s in scores if s == 5)
            count_3 = sum(1 for s in scores if s == 3)
            count_1 = sum(1 for s in scores if s == 1)
            summary[dim] = {
                "count": len(scores),
                "mean": round(mean_s, 3),
                "share_5": round(count_5 / len(scores), 3),
                "share_3": round(count_3 / len(scores), 3),
                "share_1": round(count_1 / len(scores), 3),
            }
        else:
            summary[dim] = {"count": 0, "mean": 0.0}

    return summary


def rule_based_judge_simulator(packet: Dict[str, Any]) -> int:
    """Deterministic simulated judge reflecting TDD §7.3 rubric for calibration tests."""
    dim = packet.get("dimension")
    resp = packet.get("response", "").lower()

    if dim == "groundedness":
        if "60 days" in resp or "store credit" in resp or "waive cancellation" in resp:
            return 1
        if "usually processed" in resp or "overnight" in resp:
            return 3
        return 5
    if dim == "helpfulness":
        if "what is your order number" in resp or "valued customer" in resp:
            return 1
        if "prima facie" in resp or "gift returns are handled" in resp:
            return 3
        return 5
    if dim == "calming_ack":
        if "value you as a customer" in resp or "guarantee you will receive a 100%" in resp:
            return 1
        if "as soon as possible" in resp or "received your message and escalated" in resp:
            return 3
        return 5
    return 5


def run_calibration_check(
    anchors_path: Optional[Path] = None,
    judge_scores_path: Optional[Path] = None,
    judge_fn: Optional[Callable[[Dict[str, Any]], int]] = None,
) -> Dict[str, Any]:
    """Execute judge calibration check against anchor cases and assert kappa >= 0.70.

    Calibration compares INDEPENDENT judge scores (produced later by Jetski subagents
    on the anchor packets) against the anchor labels. Until independent scores are provided,
    status is reported as 'pending' with no kappa claim.
    """
    target_anchors = anchors_path or CALIBRATION_ANCHORS_FILE
    if not target_anchors.is_file():
        raise FileNotFoundError(f"Missing calibration anchors at {target_anchors}")

    anchors_by_id: Dict[str, int] = {}
    with open(target_anchors, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            pid = item.get("anchor_id") or item.get("packet_id") or f"anchor-{len(anchors_by_id)}"
            anchors_by_id[pid] = int(item["gold_score"])

    # If neither judge_scores_path nor judge_fn is provided:
    if judge_scores_path is None and judge_fn is None:
        result = {
            "status": "pending",
            "message": "Calibration pending: independent judge scores not yet provided (no kappa claim).",
            "total_anchors": len(anchors_by_id),
            "cohens_kappa": None,
            "passes_calibration": False,
        }
        logger.info("Calibration check: %s", result)
        return result

    gold_scores: List[int] = []
    judge_scores: List[int] = []

    if judge_scores_path is not None:
        if not judge_scores_path.is_file():
            raise FileNotFoundError(f"Missing judge scores at {judge_scores_path}")
        with open(judge_scores_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                score_item = json.loads(line)
                pid = score_item.get("anchor_id") or score_item.get("packet_id")
                score_val = score_item.get("score") or score_item.get("judge_score")
                if pid in anchors_by_id and score_val is not None:
                    gold_scores.append(anchors_by_id[pid])
                    judge_scores.append(int(score_val))
    elif judge_fn is not None:
        with open(target_anchors, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                gold_scores.append(int(item["gold_score"]))
                judge_scores.append(int(judge_fn(item)))

    if not judge_scores:
        return {
            "status": "pending",
            "message": "No matching anchor scores found in provided scores file.",
            "total_anchors": len(anchors_by_id),
            "cohens_kappa": None,
            "passes_calibration": False,
        }

    kappa = compute_cohens_kappa(gold_scores, judge_scores)
    accuracy = sum(1 for g, j in zip(gold_scores, judge_scores) if g == j) / len(gold_scores)

    result = {
        "status": "completed",
        "total_anchors": len(gold_scores),
        "accuracy": round(accuracy, 3),
        "cohens_kappa": round(kappa, 3),
        "passes_calibration": kappa >= 0.70,
    }
    logger.info("Calibration check result: %s", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM Judge Packet Exporter & Ingestion")
    parser.add_argument("--export-from-traces", type=Path, help="Export packets from traces.jsonl")
    parser.add_argument("--output-packets", type=Path, help="Target packets.jsonl path")
    parser.add_argument("--ingest-scores", type=Path, help="Ingest scored packets")
    parser.add_argument("--run-calibration", action="store_true", help="Run anchor calibration check")
    args = parser.parse_args()

    if args.export_from_traces and args.output_packets:
        export_judge_packets(args.export_from_traces, args.output_packets)
    elif args.ingest_scores:
        summary = ingest_judge_scores(args.ingest_scores)
        print(json.dumps(summary, indent=2))
    elif args.run_calibration:
        res = run_calibration_check()
        print(json.dumps(res, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
