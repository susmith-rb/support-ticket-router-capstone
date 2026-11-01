"""CLI script to generate tests/agents/golden/triage_golden_v1.json."""

from __future__ import annotations

import json
from pathlib import Path

from tests.agents.golden.harness import generate_all_golden


def main() -> None:
    data = generate_all_golden()
    golden_path = Path(__file__).resolve().parent / "triage_golden_v1.json"
    with open(golden_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    print(f"Generated golden file with {data['scenario_count']} scenarios at {golden_path}")


if __name__ == "__main__":
    main()
