"""Wall-clock command TTLs for hosts that run the simulation below real time."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import math
from typing import Any

# Lowest real-time factor campaign hosts show on the 1.2 km worlds (Koko: ~0.35-0.45; see
# sim/README.md). Command TTLs are wall-clock, so a leg must budget sim_time /
# CAMPAIGN_MIN_RTF wall seconds.
CAMPAIGN_MIN_RTF = 0.35
MAX_TTL_S = 3600.0
MAX_TTL_SCALE = 20.0


def ttl_scale(text: str) -> float:
    """``--ttl-scale`` argument type shared by the campaign and episode command lines."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid float value: {text!r}") from None
    if not math.isfinite(value) or not 0.0 < value <= MAX_TTL_SCALE:
        raise argparse.ArgumentTypeError(f"must be finite and between 0 and {MAX_TTL_SCALE:g}")
    return value


def scaled_ttl(ttl_s: float, scale: float) -> float:
    """Scale a command TTL, capped at the executor's one-hour limit.

    The executor's TTL is wall-clock, so on a slow host a fixed TTL expires mid-motion
    (e.g. a 34 m take-off at RTF 0.2 needs ~240 s). ``--ttl-scale`` keeps the same
    sim-time budget; 1.0 leaves command timing unchanged.
    """
    return min(float(ttl_s) * scale, MAX_TTL_S)


def goto_ttl_s(leg_distance_m: float, speed_m_s: float) -> float:
    """Wall-clock goto TTL for one cruise leg, covering the campaign's RTF floor.

    The executor TTL is wall-clock, so a leg of ``leg_distance_m`` at ``speed_m_s`` needs
    ``distance / (speed * CAMPAIGN_MIN_RTF)`` wall seconds; 60 s covers acceleration, yaw
    alignment and the arrival slowdown. The 120 s floor keeps canonical 60 m legs (and every
    existing canonical command) unchanged.
    """
    return max(120.0, leg_distance_m / (max(0.1, speed_m_s) * CAMPAIGN_MIN_RTF) + 60.0)


def scale_mission_ttls(mission: dict[str, Any], scale: float) -> None:
    """Scale command TTLs in a generated mission for hosts that run the sim below real time.

    Only ``ttl_s`` values and tool-step wall waits scale; selection waits are wall-clock UI
    waits and stay fixed.
    """
    if scale == 1.0:
        return
    steps = mission.get("steps")
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        for key in ("ttl_s", "timeout_s"):
            value = step.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                step[key] = scaled_ttl(value, scale)
        arguments = step.get("arguments")
        if isinstance(arguments, Mapping) and isinstance(arguments.get("ttl_s"), (int, float)):
            arguments["ttl_s"] = scaled_ttl(arguments["ttl_s"], scale)
