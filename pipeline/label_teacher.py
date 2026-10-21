#!/usr/bin/env python3
"""label_teacher.py — Gemini teacher labels with self-consistency, as Laya `gold` soft targets.

Reads  data/processed/inbound_first_turn.parquet (from prepare_twcs.py)
Writes data/labels/labels.jsonl (append + resume): one record per message with vote shares.

  PROJECT=<id> python label_teacher.py --n 2000 --samples 3 --model gemini-3.1-pro-preview --location global
"""
import argparse, asyncio, json, os, pathlib, random, time
import pandas as pd
from google import genai
from google.genai import types
from router_schema import QUESTIONS, SENTIMENTS, INTENTS, URGENCY_LEVELS

SCHEMA = {
    "type": "object",
    "required": ["escalate", "sentiment", "urgency", "intent"],
    "properties": {
        "escalate": {"type": "boolean"},
        "sentiment": {"type": "string", "enum": SENTIMENTS},
        "urgency": {"type": "integer", "minimum": 0, "maximum": URGENCY_LEVELS - 1},
        "intent": {"type": "string", "enum": INTENTS},
    },
}

def rubric() -> str:
    lines = []
    for qid, q in QUESTIONS.items():
        c = q["criteria"]
        if isinstance(c, dict):
            opts = "; ".join(f"{k} = {v}" for k, v in c.items())
        else:
            opts = "; ".join(f"{i} = {v}" for i, v in enumerate(c))
        lines.append(f"- {qid}: {q['instructions']} ({opts})")
    return "\n".join(lines)

PROMPT = (
    "You label customer-support messages sent to an online retailer's support account.\n"
    "Answer four questions about the message:\n{rubric}\n"
    "Sarcasm counts as frustrated or angry. Polite wording with a hard deadline is urgent. "
    "Tokens like [ORDER], [EMAIL], [URL], @user are redactions. Judge the message only.\n"
    "Message: ```{text}```"
)

async def call(client, model, text, seed, thinking):
    cfg = dict(response_mime_type="application/json", response_schema=SCHEMA, temperature=0.7, seed=seed)
    if thinking:
        cfg["thinking_config"] = types.ThinkingConfig(thinking_level=thinking)
    for attempt in range(6):
        try:
            r = await client.aio.models.generate_content(
                model=model, contents=PROMPT.format(rubric=rubric(), text=text),
                config=types.GenerateContentConfig(**cfg))
            return json.loads(r.text)
        except Exception as e:  # 429/503 -> backoff; schema/other -> record
            msg = str(e)
            if any(s in msg for s in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500")) and attempt < 5:
                await asyncio.sleep(min(60, 2 ** attempt + random.random()))
                continue
            return {"error": msg[:300]}

def to_gold(votes):
    ok = [v for v in votes if "error" not in v]
    if len(ok) < 2:
        return None
    n = len(ok)
    p_true = sum(bool(v["escalate"]) for v in ok) / n
    return {
        "escalate": {"probabilities": {"false": 1 - p_true, "true": p_true}},
        "sentiment": {"probabilities": {k: sum(v["sentiment"] == k for v in ok) / n for k in SENTIMENTS}},
        "urgency": {"probabilities": {str(i): sum(int(v["urgency"]) == i for v in ok) / n for i in range(URGENCY_LEVELS)}},
        "intent": {"probabilities": {k: sum(v["intent"] == k for v in ok) / n for k in INTENTS}},
    }, n

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inp", default="data/processed/inbound_first_turn.parquet")
    ap.add_argument("--out", default="data/labels/labels.jsonl")
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--model", default="gemini-3.1-pro-preview")
    ap.add_argument("--location", default=os.environ.get("LOCATION", "global"))
    ap.add_argument("--thinking", default="low", help="low|high|'' (empty = model default)")
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    client = genai.Client(vertexai=True, project=os.environ["PROJECT"], location=a.location)
    df = pd.read_parquet(a.inp).sample(frac=1, random_state=a.seed).head(a.n)
    out = pathlib.Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        done = {json.loads(l)["thread_id"] for l in out.open()}
    todo = [r for r in df.itertuples(index=False) if int(r.thread_id) not in done]
    print(f"{len(done)} already labeled, {len(todo)} to go; model={a.model} location={a.location}", flush=True)

    sem = asyncio.Semaphore(a.concurrency)
    async def one(r):
        async with sem:
            votes = [await call(client, a.model, r.text, s, a.thinking or None) for s in range(a.samples)]
        return r, votes

    t0, n_ok, n_err = time.time(), 0, 0
    with out.open("a") as fh:
        for i, fut in enumerate(asyncio.as_completed([one(r) for r in todo]), 1):
            r, votes = await fut
            res = to_gold(votes)
            if res is None:
                n_err += 1
                if n_err <= 3:
                    print("  label failed:", [v.get("error", "")[:160] for v in votes], flush=True)
                continue
            gold, n = res
            fh.write(json.dumps({"thread_id": int(r.thread_id), "text": r.text, "gold": gold,
                                 "votes": votes, "n_votes": n, "model": a.model}) + "\n")
            n_ok += 1
            if i % 100 == 0:
                rate = i / (time.time() - t0)
                print(f"  {i}/{len(todo)}  ok={n_ok} err={n_err}  {rate:.1f} msg/s", flush=True)
    print(f"done: ok={n_ok} err={n_err} in {time.time() - t0:.0f}s -> {out}")

if __name__ == "__main__":
    asyncio.run(main())
