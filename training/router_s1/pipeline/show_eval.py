import json, sys
r = json.load(open(sys.argv[1]))
print("tau_low tau_esc tier2 auto t1esc t1prec sysrecall missed")
for s in r["tiered_simulation"]:
    print(s["tau_low"], s["tau_esc"], s["tier2_share"], s["auto_share"], s["tier1_escalate_share"], s["tier1_precision"], s["system_recall_if_tier2_matches_teacher"], s["missed_below_tau_low"])
print("\nlowest-scoring positives:")
for m in r["missed_positives_low_score"]:
    print(f"  p_esc={m['p_esc']:.3f} gold={m['gold_p_escalate']} pred={m['pred']}  {m['text'][:150]}")
sw = r["escalation"]["signals"]["max_escalate_angry_urgent"]["sweep"]
print("\nsweep (all positives):")
for s in sw:
    if s["tau"] in (0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5): print(" ", s)
