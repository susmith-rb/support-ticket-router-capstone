"""Tests for hermetic 20-case CI smoke evaluation gate.

# REQ-EVAL-010: Hermetic CI Smoke Evaluation Gate
"""

from __future__ import annotations

import time

from evals.smoke_gate import SMOKE_CASES, run_smoke_gate


def test_smoke_subset_deterministic_gate():
    # REQ-EVAL-010
    assert len(SMOKE_CASES) == 20, f"Expected 20 smoke cases, got {len(SMOKE_CASES)}"

    t0 = time.monotonic()
    success = run_smoke_gate()
    duration_s = time.monotonic() - t0

    assert success is True, "Smoke gate failed execution or violated hard gates"
    assert duration_s < 15.0, f"Smoke gate exceeded 15.0s budget ({duration_s:.2f}s)"
