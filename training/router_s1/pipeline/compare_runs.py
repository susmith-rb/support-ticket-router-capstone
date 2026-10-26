#!/usr/bin/env python3
import json
import sys


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: python compare_runs.py <run_dir>")
        return 0
    rows = []
    for label, p in [("base english", "baseline_english_v2.json"), ("base typed-decisions", "baseline_typed-decisions_v2.json"), ("v2", "eval_test.json")]:
        r = json.load(open(f"{args[0]}/{p}"))
        e = r["escalation"]["signals"]["max_escalate_angry_urgent"]
        rows.append((label, r["n"], r["sentiment"], r["intent"], r["urgency"], r["latency_ms"], r["escalation"]["positives"], e["min_score_of_any_positive"]))
    for l, n, s, i, u, lat, pos, mn in rows:
        print(f"{l:22s} n={n} sent acc {s['accuracy']:.3f} f1 {s['macro_f1']:.3f} ece {s['ece']:.3f} | intent acc {i['accuracy']:.3f} f1 {i['macro_f1']:.3f} ece {i['ece']:.3f} | urg {u['accuracy']:.3f}/{u['within_one']:.3f} | p50 {lat['p50']} p95 {lat['p95']} | pos {pos} minpos {mn}")
    st = json.load(open(f"{args[0]}/stats.json"))
    print({k: st[k]["n"] for k in ("train", "val", "test")}, "test esc", st["test"]["escalate_rate"], "test intent", st["test"]["intent"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
