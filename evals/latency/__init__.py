"""Live per-path latency benchmark package with trace-level step attribution.

# REQ-EVAL-011: Live Per-Path Latency Benchmark with Trace-Level Step Attribution
"""

from __future__ import annotations

__all__ = [
    "cases_path",
]

import pathlib


def cases_path() -> pathlib.Path:
    """Return the absolute path to the bundled cases_v1.jsonl file."""
    return pathlib.Path(__file__).resolve().parent / "cases_v1.jsonl"
