#!/usr/bin/env python3
"""prepare_twcs.py — Kaggle Customer Support on Twitter -> AmazonHelp slice for the router.

Outputs (all PII-redacted):
  out/inbound_first_turn.parquet  one row per customer-opened thread (what the router classifies)
  out/threads.jsonl               reconstructed threads (for FAQ synthesis / resolved-threads store)

Usage: python prepare_twcs.py --csv twcs.csv --brand AmazonHelp --out out/
Run on the Workbench VM or Cloud Shell (twcs.csv from kaggle datasets download -d thoughtvector/customer-support-on-twitter).
"""
import argparse
import hashlib
import json
import pathlib
import re

ORDER = re.compile(r"(?i)\b(?:order|ord)\.?\s*(?:no\.?|number|#|id)?\s*[:\-#]?\s*(\d{3}-\d{7}-\d{7}|\d{5,12})\b|\b\d{3}-\d{7}-\d{7}\b")
CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
PII = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"https?://\S+"), "[URL]"),
    (re.compile(r"@\d+"), "@user"),                                  # dataset already pseudonymizes customers as numeric IDs
]
PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
SIG = re.compile(r"\s*[\^\-–—]\s?[A-Z]{1,3}\b\s*$")                 # agent signatures like ^AB / -JL


def luhn_ok(digits: str) -> bool:
    d = [int(c) for c in digits][::-1]
    return len(d) >= 13 and sum(x if i % 2 == 0 else (x * 2 - 9 if x * 2 > 9 else x * 2) for i, x in enumerate(d)) % 10 == 0


def redact(t: str) -> str:
    t = SIG.sub("", str(t))
    for rx, tok in PII:
        t = rx.sub(tok, t)
    t = ORDER.sub(lambda m: ("order [ORDER]" if m.group(0).lower().startswith(("order", "ord")) else "[ORDER]"), t)  # before cards
    t = CARD.sub(lambda m: "[CARD]" if luhn_ok(re.sub(r"\D", "", m.group(0))) else m.group(0), t)
    t = PHONE.sub(lambda m: "[PHONE]" if len(re.sub(r"\D", "", m.group(0))) >= 10 else m.group(0), t)
    return re.sub(r"\s+", " ", t).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True); ap.add_argument("--brand", default="AmazonHelp")
    ap.add_argument("--out", default="out"); ap.add_argument("--max_threads", type=int, default=0)
    a = ap.parse_args()
    import pandas as pd
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(a.csv, dtype={"tweet_id": "int64", "author_id": str, "text": str,
                                   "response_tweet_id": str, "in_response_to_tweet_id": "float64"})
    df["inbound"] = df["inbound"].astype(str).str.lower().eq("true")
    by_id = df.set_index("tweet_id")
    brand = df[df.author_id == a.brand]
    print(f"{a.brand} tweets: {len(brand):,}")

    # parents of brand replies = customer tweets Amazon answered
    parent_ids = set(brand.in_response_to_tweet_id.dropna().astype("int64"))
    cust = df[df.inbound & df.tweet_id.isin(parent_ids)]
    print(f"customer tweets answered by {a.brand}: {len(cust):,}")

    # thread reconstruction: walk up to the root, then collect the chain of replies downward
    def root_of(tid):
        seen = set()
        while True:
            seen.add(tid)
            p = by_id.at[tid, "in_response_to_tweet_id"] if tid in by_id.index else float("nan")
            if pd.isna(p) or int(p) in seen or int(p) not in by_id.index:
                return tid
            tid = int(p)
    children = {}
    for tid, p in df[["tweet_id", "in_response_to_tweet_id"]].dropna().itertuples(index=False):
        children.setdefault(int(p), []).append(int(tid))

    roots, seen_root = [], set()
    for tid in cust.tweet_id:
        r = root_of(int(tid))
        if r not in seen_root:
            seen_root.add(r); roots.append(r)
    if a.max_threads: roots = roots[: a.max_threads]
    print(f"threads: {len(roots):,}")

    first_rows, dedupe = [], set()
    with open(out / "threads.jsonl", "w") as fh:
        for r in roots:
            turns, stack = [], [r]
            while stack and len(turns) < 20:
                t = stack.pop(0)
                if t not in by_id.index: continue
                row = by_id.loc[t]
                turns.append({"tweet_id": int(t), "role": "customer" if row.inbound else "agent",
                              "text": redact(row.text), "ts": str(row.created_at)})
                stack.extend(sorted(children.get(t, [])))
            if not turns or turns[0]["role"] != "customer": continue
            has_agent = any(x["role"] == "agent" for x in turns)
            if not has_agent: continue
            h = hashlib.md5(turns[0]["text"].lower().encode()).hexdigest()
            if h in dedupe: continue
            dedupe.add(h)
            fh.write(json.dumps({"thread_id": int(r), "brand": a.brand, "turns": turns,
                                 "n_customer": sum(x["role"] == "customer" for x in turns),
                                 "n_agent": sum(x["role"] == "agent" for x in turns)}) + "\n")
            first_rows.append({"thread_id": int(r), "text": turns[0]["text"], "ts": turns[0]["ts"],
                               "n_turns": len(turns)})
    pd.DataFrame(first_rows).to_parquet(out / "inbound_first_turn.parquet", index=False)
    print(f"wrote {len(first_rows):,} first-turn messages and threads to {out}/")


if __name__ == "__main__":
    main()
