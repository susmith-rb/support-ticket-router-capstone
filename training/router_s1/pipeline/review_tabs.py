#!/usr/bin/env python3
"""review_tabs.py — payload for: Review A/B tabs (116 disagreement cases + 30 agreed spot-checks)
and an 'All model outputs' comparison tab (Gemini teacher, Laya v2, Claude Sonnet 5.5, Claude Opus 5.5).

  python review_tabs.py golden_key.jsonl golden_sheet_rows.json sonnet.jsonl opus.jsonl disagree.json out_requests.json out_dir
"""
import csv, json, random, sys

Q = ("escalate", "sentiment", "urgency", "intent")
URG = {"0": "0 - can wait days", "1": "1 - within a day", "2": "2 - within hours", "3": "3 - immediate"}


def esc(v): return "yes" if str(v).lower() == "true" else "no"
def votes_str(vs, q): return "/".join((esc(v[q]) if q == "escalate" else str(v[q])) for v in vs if "error" not in v)


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if len(args) < 7 or args[0] in ("-h", "--help"):
        print("Usage: python review_tabs.py golden_key.jsonl golden_sheet_rows.json sonnet.jsonl opus.jsonl disagree.json out_requests.json out_dir")
        return 0

    key = {json.loads(l)["case_id"]: json.loads(l) for l in open(args[0])}
    msgs = dict(json.load(open(args[1])))
    son = {json.loads(l)["case_id"]: json.loads(l) for l in open(args[2])}
    opu = {json.loads(l)["case_id"]: json.loads(l) for l in open(args[3])}
    dis = json.load(open(args[4])); out_req, out_dir = args[5], args[6]

    agreed = sorted(c for c in key if c not in set(dis))
    spot = sorted(random.Random(5).sample(agreed, 30))
    review = sorted(set(dis) | set(spot))
    hdr = ["case_id", "message", "escalate", "sentiment", "urgency", "intent", "unsure", "notes"]
    rev_rows = [[c, msgs[c], "", "", "", "", "", ""] for c in review]

    comp_hdr = ["case_id", "message", "stratum", "in review set",
                "Gemini escalate", "Gemini sentiment", "Gemini urgency", "Gemini intent", "Gemini votes (esc | sent)",
                "Laya escalate", "Laya sentiment", "Laya urgency", "Laya intent", "Laya p_esc",
                "Sonnet escalate", "Sonnet sentiment", "Sonnet urgency", "Sonnet intent", "Sonnet self-agreement",
                "Opus escalate", "Opus sentiment", "Opus urgency", "Opus intent", "Opus self-agreement",
                "all agree on escalate", "all agree on sentiment"]
    comp = []
    for c in sorted(key):
        k, s, o = key[c], son[c], opu[c]
        t, l, sm, om = k["teacher"], k["laya"], s["majority"], o["majority"]
        comp.append([c, msgs[c], k["stratum"], "disagreement" if c in dis else ("spot-check" if c in spot else ""),
                     esc(t["escalate"]), t["sentiment"], URG[str(t["urgency"])], t["intent"],
                     f"{votes_str(k['votes'], 'escalate')} | {votes_str(k['votes'], 'sentiment')}",
                     esc(l["escalate"]), l["sentiment"], URG[str(l["urgency"])], l["intent"], l["p_esc"],
                     esc(sm["escalate"]), sm["sentiment"], URG.get(str(sm["urgency"]), ""), sm["intent"], round(min(s["agreement"].values()), 2),
                     esc(om["escalate"]), om["sentiment"], URG.get(str(om["urgency"]), ""), om["intent"], round(min(o["agreement"].values()), 2),
                     "yes" if len({esc(t["escalate"]), esc(sm["escalate"]), esc(om["escalate"])}) == 1 else "NO",
                     "yes" if len({t["sentiment"], sm["sentiment"], om["sentiment"]}) == 1 else "NO"])
    reqs = [{"op": "write", "range": f"'Review A ({len(review)})'!A1", "data": [hdr] + rev_rows},
            {"op": "write", "range": f"'Review B ({len(review)})'!A1", "data": [hdr] + rev_rows},
            {"op": "write", "range": "'All model outputs'!A1", "data": [comp_hdr] + comp}]
    json.dump(reqs, open(out_req, "w"))
    with open(f"{out_dir}/golden_all_model_outputs.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(comp_hdr); w.writerows(comp)
    json.dump({"review": review, "disagreement": sorted(dis), "spot_check": spot}, open(f"{out_dir}/review_set.json", "w"))
    print(f"review set: {len(review)} = {len(dis)} disagreement + {len(spot)} spot-check; comparison rows {len(comp)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
