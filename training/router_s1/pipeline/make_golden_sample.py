#!/usr/bin/env python3
"""make_golden_sample.py — sample 300 test messages for human labeling (the golden set).

Stratified, with oversampling of the cases that matter most and are hardest:
  * split : the teacher's 3 votes disagreed on escalate or sentiment      (up to 75)
  * strict: teacher majority says angry OR urgency=immediate               (up to 90)
  * rest  : stratified by teacher intent                                   (fill to N)
Writes:
  golden_sheet_rows.json  [case_id, message] rows for the labeling sheet (no model outputs, to avoid bias)
  golden_key.jsonl        case_id -> thread_id, stratum, teacher votes/majority, Laya v2 prediction
Because strata are oversampled, metrics must be reported per stratum and re-weighted to the test
distribution (weights = stratum share in test / stratum share in sample; stored in the key).

  python make_golden_sample.py labels.jsonl test.jsonl pred_test.jsonl out_dir [--n 300]
"""
import argparse, collections, json, pathlib, random


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("labels"); ap.add_argument("test"); ap.add_argument("pred"); ap.add_argument("out")
    ap.add_argument("--n", type=int, default=300); ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    votes = {str(json.loads(l)["thread_id"]): json.loads(l)["votes"] for l in open(a.labels)}
    test = [json.loads(l) for l in open(a.test)]
    pred = [json.loads(l) for l in open(a.pred)]
    rows = []
    for t, p in zip(test, pred):
        vs = [v for v in votes[t["id"]] if "error" not in v]
        maj = lambda q: collections.Counter(str(v[q]) for v in vs).most_common(1)[0][0]
        split = len({str(v["escalate"]) for v in vs}) > 1 or len({v["sentiment"] for v in vs}) > 1
        strict = maj("sentiment") == "angry" or maj("urgency") == "3"
        stratum = "split" if split else ("strict" if strict else "rest")
        laya = {q: max(p["p"][q], key=p["p"][q].get) for q in ("sentiment", "urgency", "intent")}
        laya["escalate"] = p["p"]["escalate"].get("true", 0.0) >= 0.5
        laya["p_esc"] = round(max(p["p"]["escalate"].get("true", 0.0), p["p"]["sentiment"].get("angry", 0.0),
                                  p["p"]["urgency"].get("3", 0.0)), 4)
        rows.append({"thread_id": t["id"], "text": t["state"], "stratum": stratum, "votes": vs,
                     "teacher": {q: maj(q) for q in ("escalate", "sentiment", "urgency", "intent")}, "laya": laya})

    by = collections.defaultdict(list)
    for r in rows: by[r["stratum"]].append(r)
    for v in by.values(): rng.shuffle(v)
    pick = by["split"][:75] + by["strict"][:90]
    need = a.n - len(pick)
    rest = by["rest"]; by_int = collections.defaultdict(list)
    for r in rest: by_int[r["teacher"]["intent"]].append(r)
    for intent, group in sorted(by_int.items()):
        k = round(need * len(group) / len(rest)); pick += group[:k]
    pick = pick[: a.n]
    rng.shuffle(pick)

    test_share = {s: len(v) / len(rows) for s, v in by.items()}
    samp_cnt = collections.Counter(r["stratum"] for r in pick)
    weights = {s: round(test_share[s] / (samp_cnt[s] / len(pick)), 4) for s in samp_cnt}

    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    sheet = []
    with open(out / "golden_key.jsonl", "w") as fh:
        for i, r in enumerate(pick, 1):
            cid = f"G{i:03d}"
            sheet.append([cid, r["text"]])
            fh.write(json.dumps({"case_id": cid, "weight": weights[r["stratum"]], **{k: r[k] for k in
                      ("thread_id", "stratum", "teacher", "laya", "votes")}}) + "\n")
    json.dump(sheet, open(out / "golden_sheet_rows.json", "w"))
    print("sample:", dict(samp_cnt), "| test strata:", {s: len(v) for s, v in by.items()}, "| weights:", weights)


if __name__ == "__main__":
    main()
