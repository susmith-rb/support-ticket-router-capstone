#!/usr/bin/env python3
"""usage_probe.py — measure real token usage per teacher call (input / output / thinking)."""
import json, os, pathlib, sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config.models import get, model_id


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print("Usage: PROJECT=<id> python usage_probe.py")
        return 0

    import pandas as pd
    from google import genai
    from google.genai import types
    sys.path.insert(0, os.path.dirname(__file__))
    from label_teacher import PROMPT, SCHEMA, rubric

    teacher_cfg = get("teacher")
    loc = os.environ.get("LOCATION", teacher_cfg.get("location", "global"))
    thinking_level = str(teacher_cfg.get("thinking", "low")).lower()
    client = genai.Client(vertexai=True, project=os.environ.get("PROJECT"), location=loc)
    df = pd.read_parquet(os.path.join(os.path.dirname(__file__), "../data/processed/inbound_first_turn.parquet")).sample(5, random_state=99)
    tot = {"in": 0, "out": 0, "think": 0}
    for t in df.text:
        r = client.models.generate_content(
            model=model_id("teacher"),
            contents=PROMPT.format(rubric=rubric(), text=t),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SCHEMA,
                temperature=0.7,
                thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
            ),
        )
        u = r.usage_metadata
        i, o, th = u.prompt_token_count or 0, u.candidates_token_count or 0, getattr(u, "thoughts_token_count", 0) or 0
        tot["in"] += i; tot["out"] += o; tot["think"] += th
        print(f"in={i} out={o} thinking={th}")
    n = len(df)
    print(json.dumps({k: v / n for k, v in tot.items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
