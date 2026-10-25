import json
import sys

S = sys.argv[1]


def rows(f, cfg):
    d = json.load(open(f))
    res = d.get("results") or next(v for v in d.values() if isinstance(v, dict) and "results" in v)["results"]
    r = [c for c in res if c["config"] == cfg][0]["rows"]
    return {
        x["qid"]: [
            t if isinstance(t, str) else t.get("id")
            for t in (x.get("top10_ids") or x.get("top10") or [])
        ]
        for x in r
    }


base = rows(f"{S}/ablation/dev_rows_phaseD.json", "intent_noisy_0.2")
lay = rows(f"{S}/rerank/dev_rows_laya.json", "laya_choice_top5")
ce = rows(f"{S}/rerank/dev_rows_ce.json", "ce_minilm_a0.7")
out = []
for name, w in [
    ("rrf_laya_ce", (1, 1, 0)),
    ("rrf_laya_ce_base", (1, 1, 1)),
    ("rrf_laya2_ce", (2, 1, 0)),
    ("rrf_laya2_ce_base", (2, 1, 1)),
]:
    rr = []
    for q in base:
        sc = {}
        for wt, lst in zip(w, (lay.get(q) or base[q], ce.get(q) or base[q], base[q])):
            for i, d in enumerate(lst):
                sc[d] = sc.get(d, 0) + wt / (i + 1 + 5)
        rr.append({"qid": q, "top10_ids": sorted(sc, key=lambda d: -sc[d])[:10]})
    out.append({"config": name, "rows": rr})
json.dump({"meta": "rrf fusion", "results": out}, open(f"{S}/rerank/dev_rows_fused.json", "w"))
