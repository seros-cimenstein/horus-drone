"""Hazard scenario: a goto straight through a static obstacle must be blocked or avoided."""
from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

from sim.campaign.episode import Episode, clearance_below_threshold

# A hazard trial is only meaningful when the direct path is a real collision course:
# the 0.6 m drone radius is already subtracted, so <= -0.3 m means the hull cuts at
# least 0.3 m into the obstacle along the flown line.
HAZARD_MIN_PATH_PENETRATION_M = 0.3
# Hazard trial search: obstacle kinds worth trying, how far from the obstacle the trial starts,
# and the approach directions tried (a tangent obstacle needs a different crossing heading).
HAZARD_OBSTACLE_TOKENS = ("tree", "building", "roof", "canopy", "shelter", "vehicle")
HAZARD_APPROACH_DISTANCE_M = 8.0
HAZARD_APPROACH_HEADINGS_DEG = tuple(range(0, 360, 45))
EXPECTED_SAFE_BEHAVIOR = "blocked-or-avoided"


class HazardCrash(RuntimeError):
    """Ground truth shows ground contact or an excessive attitude during the hazard goto."""


def path_is_collision_course(clearance_m: float) -> bool:
    """True when the planned direct path penetrates the obstacle by a clear margin."""
    return math.isfinite(clearance_m) and clearance_m <= -HAZARD_MIN_PATH_PENETRATION_M


def _trial_summary(trial: Mapping[str, Any]) -> dict[str, Any]:
    """The fields worth keeping when a hazard trial is considered but not flown."""
    return {key: trial.get(key) for key in (
        "obstacle", "obstacle_model", "direct_path_static_clearance_m", "approach_min_clearance_m",
        "distance_m")}


def _is_obstacle(primitive: Any) -> bool:
    text = f"{primitive.model} {primitive.name}".lower()
    return primitive.static and any(token in text for token in HAZARD_OBSTACLE_TOKENS)


def hazard_trials(episode: Episode) -> list[dict[str, Any]] | None:
    """Obstacle/approach pairs with a real collision course and a safe approach leg, nearest first.

    The direct path must penetrate the obstacle by ``HAZARD_MIN_PATH_PENETRATION_M`` for the
    0.6 m hull, and the transit from the current position to the start point must clear the
    world, so the trial exercises avoidance instead of being unrunnable. A tangent obstacle
    (e.g. +0.06 m clearance) is not a collision course and must not be chosen.
    """
    drone = episode._drone()
    if drone is None:
        return None
    from sim.worldgen.plan import cruise_legs, heading_deg, legs_clearance
    trials: list[dict[str, Any]] = []
    for primitive in filter(_is_obstacle, episode.world.primitives):
        cx, cy, cz = primitive.transform[:3]
        dz = min(max(cz, 3.0), episode.transit_alt - 1.0)
        distance = math.hypot(cx - drone[0], cy - drone[1])
        if distance <= 0.0:
            continue
        for heading_step in HAZARD_APPROACH_HEADINGS_DEG:
            angle = math.radians(heading_step)
            ux, uy = math.cos(angle), math.sin(angle)
            start = (cx + ux * HAZARD_APPROACH_DISTANCE_M, cy + uy * HAZARD_APPROACH_DISTANCE_M, dz)
            destination = (cx - ux * HAZARD_APPROACH_DISTANCE_M, cy - uy * HAZARD_APPROACH_DISTANCE_M, dz)
            clearance, _blocker = episode.world.segment_clearance(start, destination, radius=0.6)
            if not path_is_collision_course(clearance):
                continue
            yaw_rad = math.atan2(destination[1] - start[1], destination[0] - start[0])
            legs = cruise_legs(drone[:3], start, heading_deg(yaw_rad), episode.transit_alt,
                               max_leg_m=episode.max_leg_m)
            approach_clearance = legs_clearance(episode.world, drone[:3], legs, drone_radius_m=0.6)
            if clearance_below_threshold(approach_clearance):
                continue
            trials.append({"distance_m": distance, "obstacle": primitive.name,
                           "obstacle_model": primitive.model, "obstacle_pose": primitive.transform,
                           "start": start, "destination_far_side": destination, "yaw_rad": yaw_rad,
                           "direct_path_static_clearance_m": clearance,
                           "approach_min_clearance_m": approach_clearance})
    return sorted(trials, key=lambda trial: trial["distance_m"])


async def _reach_trial_start(
    episode: Episode, candidates: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, Any], list[dict[str, Any]]]:
    """Fly to the first candidate whose live transit is clear and accepted.

    The drone can drift between ranking and flight, so the transit is re-validated from the live
    position and a refused candidate falls through to the next one instead of skipping.
    Returns ``(trial or None, last route, considered entries)``.
    """
    from sim.worldgen.plan import cruise_legs, heading_deg, legs_clearance
    approach_route: dict[str, Any] = {"passed": False, "reason": "no hazard trial was flown"}
    considered: list[dict[str, Any]] = []
    for candidate in candidates:
        live = episode._drone()
        if live is None:
            break
        legs = cruise_legs(live[:3], candidate["start"], heading_deg(candidate["yaw_rad"]),
                           episode.transit_alt, max_leg_m=episode.max_leg_m)
        live_clearance = legs_clearance(episode.world, live[:3], legs, drone_radius_m=0.6)
        entry = {**_trial_summary(candidate), "live_transit_clearance_m": live_clearance}
        if clearance_below_threshold(live_clearance):
            considered.append({**entry, "outcome": "transit clearance below threshold"})
            continue
        route = await episode._navigate(candidate["start"], candidate["yaw_rad"])
        entry["outcome"] = "flown" if route.get("passed") else "navigation refused"
        entry["route_reason"] = route.get("reason")
        considered.append(entry)
        approach_route = route
        if route.get("passed"):
            return candidate, approach_route, considered
    return None, approach_route, considered


async def _proximity_sensors(episode: Episode) -> list[Any]:
    try:
        state = await episode._tool("get_state")
        vehicle = state.get("vehicle", {}) if isinstance(state, Mapping) else {}
        if isinstance(vehicle, Mapping) and isinstance(vehicle.get("proximity"), list):
            return vehicle["proximity"]
    except Exception as exc:
        print(f"pre-hazard get_state failed: {type(exc).__name__}: {exc}", flush=True)
    return []


def _skipped(reason: str, **fields: Any) -> dict[str, Any]:
    return {"passed": False, "status": "skipped", "reason": reason,
            "expected_safe_behavior": EXPECTED_SAFE_BEHAVIOR, **fields}


async def run(episode: Episode) -> dict[str, Any]:
    from sim.worldgen.plan import heading_deg, ned_pose

    if not await episode._ensure_airborne():
        return {**episode._takeoff_failure("vehicle could not take off"), "crashed": False}
    candidates = hazard_trials(episode)
    if not candidates:
        return _skipped(f"no static obstacle offers a collision course "
                        f"(at most -{HAZARD_MIN_PATH_PENETRATION_M:.2f} m for the 0.6 m hull) "
                        f"with a safe approach leg",
                        obstacles_considered=[primitive.name for primitive in episode.world.primitives
                                              if _is_obstacle(primitive)],
                        crashed=False)
    trial, approach_route, considered = await _reach_trial_start(episode, candidates)
    if trial is None:
        return _skipped(approach_route.get("reason") or "no hazard trial could be reached",
                        crashed=False, hazard_trials_considered=considered)
    start, destination = trial["start"], trial["destination_far_side"]
    clearance = trial["direct_path_static_clearance_m"]
    start_index = len(episode.path_history)
    crash = False
    failure = None
    status: dict[str, Any] = {}
    proximity = await _proximity_sensors(episode)

    async def crash_check() -> None:
        gt = episode._drone()
        if gt is None:
            return
        roll = gt[4] if len(gt) > 4 else 0.0
        pitch = gt[5] if len(gt) > 5 else 0.0
        if (abs(roll) > math.radians(60) or abs(pitch) > math.radians(60)
                or gt[2] < episode.ground.height(gt[0], gt[1]) + 0.35):
            raise HazardCrash("unexpected ground contact or attitude exceeded 60 degrees")

    try:
        submitted = await episode._tool("goto", {"pose": ned_pose(destination),
            "heading_deg": heading_deg(math.atan2(destination[1] - start[1], destination[0] - start[0])),
            "ttl_s": episode._scaled_ttl(75.0)})
    except Exception as exc:
        failure = f"hazard goto failed: {exc}"
    else:
        if not (isinstance(submitted, Mapping) and submitted.get("task_id")):
            return {"passed": False, "reason": f"goto did not return task id: {submitted}",
                    "obstacle": trial["obstacle"], "path_clearance_m": clearance, "crashed": False}
        try:
            status = await episode._wait_tool_task(submitted, max_wall_s=300.0, observer=crash_check)
        except HazardCrash as exc:
            crash, failure = True, str(exc)
        except TimeoutError:
            failure = "hazard task did not terminate within bounded 300 s wall watchdog"
        except Exception as exc:
            crash, failure = True, f"SITL/MCP unavailable during hazard trial: {exc}"
    safe = not crash and status.get("status") in {"failed", "cancelled", "expired"} and clearance < 0.0
    result = {"passed": safe, "reason": None if safe else failure or "unsafe obstacle crossing was not blocked",
              "expected_safe_behavior": EXPECTED_SAFE_BEHAVIOR, "obstacle": trial["obstacle"],
              "obstacle_model": trial["obstacle_model"], "obstacle_pose": trial["obstacle_pose"],
              "start": start, "destination_far_side": destination,
              "approach_min_clearance_m": trial["approach_min_clearance_m"],
              "hazard_trials_considered": considered,
              "executor_result": status, "direct_path_static_clearance_m": clearance,
              "gt_min_clearance_m": episode._trajectory_clearance(start_index), "crashed": crash,
              "watchdog_failure": failure,
              "vehicle_proximity_sensors": len(proximity), "proximity_available": bool(proximity)}
    if not proximity:
        result["environment_note"] = ("the outdoor airframe reports no proximity sensors, so the "
                                      "ArduPilot proximity-avoidance safeguard is unavailable in this world")
        if result["reason"]:
            result["reason"] = f"{result['reason']}; {result['environment_note']}"
    return result
