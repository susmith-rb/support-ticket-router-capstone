"""Load test profiles: smoke, sustained, and spike (Black Friday).

# REQ-LOAD-001: Locust Load Testing Profiles and Cost/Quota Safeguards
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class LoadProfile:
    """Configuration for a load testing traffic profile."""

    name: str
    target_qps: float
    duration_s: int
    estimated_requests: int
    users: int
    spawn_rate: float
    requires_cost_ack: bool
    description: str

    def get_target_qps_at(self, elapsed_s: float) -> float:
        """Return the target instantaneous QPS at a given elapsed time in seconds."""
        if self.name != "spike":
            return self.target_qps

        # Black Friday Spike shape over 300 seconds (5 min):
        # 0s - 60s: baseline (33 msg/s)
        # 60s - 120s: ramp from 33 -> 100 msg/s (~3x)
        # 120s - 240s: peak sustained 100 msg/s (2 min)
        # 240s - 300s: ramp down back to 33 msg/s
        if elapsed_s < 60.0:
            return 33.0
        if elapsed_s < 120.0:
            # linear interpolation 33 -> 100 over 60s
            progress = (elapsed_s - 60.0) / 60.0
            return 33.0 + (100.0 - 33.0) * progress
        if elapsed_s < 240.0:
            return 100.0
        if elapsed_s < 300.0:
            # linear interpolation 100 -> 33 over 60s
            progress = (elapsed_s - 240.0) / 60.0
            return 100.0 - (100.0 - 33.0) * progress
        return 33.0


PROFILES: Dict[str, LoadProfile] = {
    "smoke": LoadProfile(
        name="smoke",
        target_qps=1.0,
        duration_s=120,
        estimated_requests=120,
        users=2,
        spawn_rate=1.0,
        requires_cost_ack=False,
        description="Light smoke test: 1 msg/s for 2 minutes (~120 requests)",
    ),
    "sustained": LoadProfile(
        name="sustained",
        target_qps=33.0,
        duration_s=1200,
        estimated_requests=39600,
        users=66,
        spawn_rate=5.0,
        requires_cost_ack=True,
        description="Sustained production volume: 33 msg/s for 20 minutes (~39,600 requests)",
    ),
    "spike": LoadProfile(
        name="spike",
        target_qps=100.0,
        duration_s=300,
        estimated_requests=20000,
        users=100,
        spawn_rate=10.0,
        requires_cost_ack=True,
        description="Black Friday spike: ramp from 33 to ~100 msg/s for 5 minutes (~20,000 requests)",
    ),
}


def get_profile(name: str) -> LoadProfile:
    """Retrieve load profile by name or raise ValueError."""
    key = name.strip().lower()
    if key not in PROFILES:
        valid = ", ".join(PROFILES.keys())
        raise ValueError(f"Unknown load profile '{name}'. Valid profiles: {valid}")
    return PROFILES[key]
