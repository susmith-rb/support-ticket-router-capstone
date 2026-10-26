#!/usr/bin/env python3
"""strict_recall.py — recall for the TDD's strict definition (gold sentiment=angry OR urgency=immediate),
with Tier-0 rule sets of different cost, on val (choose tau_low) and test (report)."""
import sys

from training.router_s1.pipeline.simulate_system import load


def strict(r):
    return r["expected"]["sentiment"] == "angry" or int(r["expected"]["urgency"]) == 3


RULESETS = {"none": set(), "lean": {"shouting", "negative", "repeat"}, "lean+deadline": {"shouting", "negative", "repeat", "deadline"},
            "full": {"shouting", "negative", "repeat", "deadline", "non_english"}}


def run(rows, tau_low, tau_esc, soft_used):
    P = sum(strict(r) for r in rows); c = {"human": 0, "tier2": 0, "auto": 0}; miss = []
    for r in rows:
        hard, soft = r["_t0"]; s = r["_pesc"]; y = strict(r)
        if hard or s >= tau_esc: c["human"] += 1
        elif s >= tau_low or (set(soft) & soft_used): c["tier2"] += 1
        else:
            c["auto"] += 1
            if y: miss.append(r["text"][:140])
    n = len(rows)
    return {"recall_bound": round(1 - len(miss) / P, 4), "P": P, **{k: round(v / n, 3) for k, v in c.items()}, "missed": miss}


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if len(args) < 2 or args[0] in ("-h", "--help"):
        print("Usage: python strict_recall.py val.jsonl test.jsonl")
        return 0

    val, test = load(args[0]), load(args[1])
    grid = [0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.2, 0.25, 0.3]
    for name, soft in RULESETS.items():
        ok = [t for t in grid if run(val, t, 0.5, soft)["recall_bound"] == 1.0]
        t = max(ok) if ok else None
        if t is None:
            best = max(grid, key=lambda g: (run(val, g, 0.5, soft)["recall_bound"], g)); t = best
        rv = run(val, t, 0.5, soft); rt = run(test, t, 0.5, soft)
        print(f"{name:14s} tau_low={t:<5} VAL recall {rv['recall_bound']} (P={rv['P']}) | TEST recall {rt['recall_bound']} (P={rt['P']}) "
              f"human {rt['human']} tier2 {rt['tier2']} auto {rt['auto']}")
        for m in rt["missed"][:4]: print("     missed:", m)
    print("\nstrict positives share (test):", round(sum(strict(r) for r in test) / len(test), 3))
    for t in (0.05, 0.1, 0.15, 0.2, 0.3):
        r = run(test, t, 0.5, set()); print(f"tier1-only tau_low={t}: strict recall {r['recall_bound']}  tier2 {r['tier2']} auto {r['auto']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
