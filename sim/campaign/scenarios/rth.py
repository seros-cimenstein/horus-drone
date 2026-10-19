"""RTH scenario: return to launch, disarm, and land within 2 m of the pad."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import math
import time
from typing import Any

from sim.campaign.episode import STOP, Episode


async def wait_disarmed(episode: Episode, limit_sim_s: float = 240.0) -> dict[str, Any]:
    start_sim = episode._sim_now()
    armed_seen = False
    latest_state: Any = None
    # The disarm budget is simulation time (limit_sim_s); the wall cap only protects against
    # a frozen clock and must scale with the host's real-time factor like every other wait.
    wall_deadline = time.monotonic() + 900.0 * max(1.0, episode.ttl_scale)
    while time.monotonic() < wall_deadline and not STOP.is_set():
        latest_state = await episode._tool("get_state")
        vehicle = latest_state.get("vehicle", {}) if isinstance(latest_state, Mapping) else {}
        armed = bool(vehicle.get("armed")) if isinstance(vehicle, Mapping) else False
        armed_seen = armed_seen or armed
        episode._record_gt()
        drone = episode._drone()
        ground = episode.ground.height(drone[0], drone[1]) if drone is not None else 0.0
        if not armed and (armed_seen or drone is None or drone[2] <= ground + 0.5):
            return {"disarmed": True, "elapsed_sim_s": episode._sim_now() - start_sim,
                    "state": latest_state, "armed_seen": armed_seen}
        if episode._sim_now() - start_sim >= limit_sim_s:
            break
        await asyncio.sleep(1.0)
    return {"disarmed": False, "elapsed_sim_s": episode._sim_now() - start_sim,
            "state": latest_state, "armed_seen": armed_seen,
            "reason": "did not disarm within 240 simulation seconds"}


def airborne_altitude_m(episode: Episode) -> float | None:
    """Ground-truth height above the local ground, or None without a GT drone pose."""
    drone = episode._drone()
    if drone is None:
        return None
    return drone[2] - episode.ground.height(drone[0], drone[1])


async def return_home(episode: Episode, *, require_airborne: bool = False) -> dict[str, Any]:
    start = episode._gt_latest()
    start_sim = episode._sim_now()
    if require_airborne:
        altitude = airborne_altitude_m(episode)
        if altitude is None or altitude < 0.5:
            # RTH on a grounded vehicle disarms trivially and proves nothing about the
            # return-to-launch behaviour; the takeoff failure is already reported by the
            # test that attempted it, so this is a skip, not a second failure.
            return {"passed": False, "status": "skipped",
                    "reason": "vehicle was not airborne before RTH",
                    "airborne_altitude_m": altitude, "start_gt": start,
                    "rth_response": None, "disarm": None,
                    "landing_error_to_pad_m": None, "duration_sim_s": 0.0}
    response = await episode._tool("rth")
    disarm = await wait_disarmed(episode, 240.0)
    episode._record_gt()
    final = episode._gt_latest()
    drone = episode._drone(final)
    pad_error = math.hypot(drone[0], drone[1]) if drone is not None else None
    return {"rth_response": response, "disarm": disarm, "start_gt": start, "final_gt": final,
            "landing_error_to_pad_m": pad_error, "duration_sim_s": episode._sim_now() - start_sim,
            "passed": bool(disarm["disarmed"] and pad_error is not None and pad_error <= 2.0)}


async def run(episode: Episode) -> dict[str, Any]:
    return await return_home(episode, require_airborne=True)
