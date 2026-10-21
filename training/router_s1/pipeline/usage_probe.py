#!/usr/bin/env python3
"""usage_probe.py — measure real token usage per teacher call (input / output / thinking)."""
import os, sys, json
import pandas as pd
from google import genai
from google.genai import types
sys.path.insert(0, os.path.dirname(__file__))
from label_teacher import PROMPT, SCHEMA, rubric

client = genai.Client(vertexai=True, project=os.environ["PROJECT"], location="global")
df = pd.read_parquet(os.path.join(os.path.dirname(__file__), "../data/processed/inbound_first_turn.parquet")).sample(5, random_state=99)
tot = {"in": 0, "out": 0, "think": 0}
for t in df.text:
    r = client.models.generate_content(model="gemini-3.1-pro-preview", contents=PROMPT.format(rubric=rubric(), text=t),
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SCHEMA, temperature=0.7,
                                           thinking_config=types.ThinkingConfig(thinking_level="low")))
    u = r.usage_metadata
    i, o, th = u.prompt_token_count or 0, u.candidates_token_count or 0, getattr(u, "thoughts_token_count", 0) or 0
    tot["in"] += i; tot["out"] += o; tot["think"] += th
    print(f"in={i} out={o} thinking={th}")
n = len(df)
print(json.dumps({k: v / n for k, v in tot.items()}))
