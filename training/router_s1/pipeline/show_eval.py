#!/usr/bin/env python3
import json, sys


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: python show_eval.py <eval.json>")
        return 0
    r = json.load(open(args[0]))
    print("tau_low tau_esc tier2 auto t1esc t1prec sysrecall missed")
    for s in r.get("tiered_simulation", []):
        print(s["tau_low"], s["tau_esc"], s["tier2_share"], s["auto_share"], s["tier1_escalate_share"], s["tier1_precision"], s["system_recall_if_tier2_matches_teacher"], s["missed_below_tau_low"])
    print("\nlowest-scoring positives:")
    for m in r.get("missed_positives_low_score", []):
        print(f"  p_esc={m['p_esc']:.3f} gold={m['gold_p_escalate']} pred={m['pred']}  {m['text'][:150]}")
    sw = r.get("escalation", {}).get("signals", {}).get("max_escalate_angry_urgent", {}).get("sweep", [])
    print("\nsweep (all positives):")
    for s in sw:
        if s.get("tau") in (0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5): print(" ", s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
