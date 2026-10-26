#!/usr/bin/env python3
import json
import sys

W = {"rest": 429 / 646, "strict": 111 / 646, "split": 106 / 646}


def lv(key, c, q):
    v = key[c]["laya"][q]
    return str(v).lower()


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if len(args) < 4 or args[0] in ("-h", "--help"):
        print("Usage: python laya_vs_models.py golden_key.jsonl sonnet.jsonl opus.jsonl disagree.json")
        return 0
    key = {json.loads(l)["case_id"]: json.loads(l) for l in open(args[0])}
    mods = {"sonnet": {json.loads(l)["case_id"]: json.loads(l) for l in open(args[1])},
            "opus": {json.loads(l)["case_id"]: json.loads(l) for l in open(args[2])}}
    cnt = {s: sum(1 for k in key.values() if k["stratum"] == s) for s in W}
    for q in ("escalate", "sentiment", "urgency", "intent"):
        out = []
        for ref in ("gemini", "sonnet", "opus"):
            raw = wsum = 0.0
            for c, k in key.items():
                r = str(k["teacher"][q]).lower() if ref == "gemini" else str(mods[ref][c]["majority"][q]).lower()
                hit = lv(key, c, q) == r
                raw += hit
                wsum += hit * W[k["stratum"]] / cnt[k["stratum"]]
            out.append(f"laya vs {ref} {raw / len(key):.3f} (reweighted {wsum:.3f})")
        print(f"{q:10s} " + " | ".join(out))
    dis = json.load(open(args[3]))
    by = {s: sum(1 for c in dis if key[c]["stratum"] == s) for s in W}
    print("3-model disagreement on escalate/sentiment, reweighted to test mix:", round(sum(W[s] * by[s] / cnt[s] for s in W), 3), by, cnt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
