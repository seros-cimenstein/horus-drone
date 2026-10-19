"""Approach scenario: close to a 5 m standoff on standing and lying/sitting GT-matched people."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

from sim.campaign.camera_geometry import torso
from sim.campaign.episode import Episode

# Approach scoring: the task must succeed and actually close to the commanded standoff at some
# point during the trial (a one-shot approach holds at the standoff; the final range is not a
# criterion because the target can walk away or become undetectable after arrival).
APPROACH_STANDOFF_M = 5.0
APPROACH_ARRIVAL_TOLERANCE_M = 2.0


def needs_declared_size(candidate: Mapping[str, Any]) -> bool:
    """Grounder-only candidates carry no detector person evidence, so range needs a size."""
    return str(candidate.get("source") or "") in {"open_vocab", "point"}


async def matched_target(
    episode: Episode, posture: str | None = None
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None, str | None]:
    """The first person (of ``posture``) with a live GT-matched candidate, in random order."""
    pool = [person for person in episode.people if posture is None or person.get("posture") == posture]
    episode.rng.shuffle(pool)
    for index, person in enumerate(pool[:max(12, episode.args.vantages * 3)]):
        candidate, _reason = await episode._observe_matched_person(person, f"target-{posture or 'any'}-{index:02d}")
        if candidate is not None:
            return person, candidate, None
    return None, None, f"no current candidate GT-matched to {posture or 'any'} target"


def camera_to_torso_range(episode: Episode, person: Mapping[str, Any],
                          sample: Mapping[str, Any] | None) -> float | None:
    """Ground-truth camera-to-torso distance for one sample, or None without a pose."""
    pose = episode._camera_pose(sample)
    target = episode._people_gt(sample).get(str(person.get("model")))
    if pose is None or target is None:
        return None
    return math.dist(pose["position"], torso(person, target))


def approach_arrival(episode: Episode, person: Mapping[str, Any], start_index: int,
                     observed: Sequence[float] = ()) -> dict[str, Any]:
    """Minimum camera-to-torso range over the trial and whether it reached the standoff.

    ``observed`` carries the ranges sampled by the trial's own observer; the recorded path
    history is the fallback (it can lack a target that only the civilian trace knows).
    """
    ranges = [float(value) for value in observed if value is not None]
    if not ranges:
        samples = episode.path_history[max(0, start_index):]
        ranges = [value for value in (camera_to_torso_range(episode, person, sample) for sample in samples)
                  if value is not None]
    minimum = min(ranges) if ranges else None
    limit = APPROACH_STANDOFF_M + APPROACH_ARRIVAL_TOLERANCE_M
    return {"gt_min_camera_to_torso_m": minimum, "arrival_limit_m": limit,
            "samples": len(ranges), "arrived": minimum is not None and minimum <= limit}


async def approach_one(episode: Episode, person: Mapping[str, Any], candidate: Mapping[str, Any], *,
                       target_size: float | None, label: str) -> dict[str, Any]:
    start_index = len(episode.path_history)
    started = episode._gt_latest()
    arguments: dict[str, Any] = {"id": candidate["id"], "dist_m": 5.0, "ttl_s": 120.0}
    if target_size is not None:
        arguments["target_size_m"] = target_size
    observed: list[float] = []

    async def observe_range() -> None:
        value = camera_to_torso_range(episode, person, episode._gt_latest())
        if value is not None:
            observed.append(value)

    try:
        status = await episode._skill("approach", arguments, timeout_s=240.0, observer=observe_range)
    except Exception as exc:
        return {"passed": False, "reason": f"approach action failed: {exc}", "arguments": arguments}
    episode._record_gt()
    sample = episode._gt_latest()
    drone = episode._drone(sample)
    target_pos = episode._people_gt(sample).get(str(person.get("model")))
    target_torso = torso(person, target_pos) if target_pos else None
    route_clearance = episode._trajectory_clearance(start_index)
    arrival = approach_arrival(episode, person, start_index, observed)
    final_range = None if drone is None or target_torso is None else math.dist(drone[:3], target_torso)
    # The reported error is the closest approach (the approach is one-shot: the target can
    # walk away after arrival, which says nothing about the executor's standoff control).
    range_error = (None if arrival["gt_min_camera_to_torso_m"] is None
                   else arrival["gt_min_camera_to_torso_m"] - APPROACH_STANDOFF_M)
    succeeded = status.get("status") == "succeeded"
    reason = None
    if not succeeded:
        reason = f"approach task ended {status.get('status')}"
    elif not arrival["arrived"]:
        reason = (f"approach never closed to the standoff (min camera-to-torso range "
                  f"{arrival['gt_min_camera_to_torso_m']} m > {arrival['arrival_limit_m']:.1f} m)")
    return {"label": label, "passed": succeeded and arrival["arrived"], "reason": reason, "arguments": arguments,
            "task_status": status, "gt_final_camera_to_torso_m": final_range,
            "final_distance_error_vs_5m_m": None if final_range is None else final_range - APPROACH_STANDOFF_M,
            "distance_error_vs_5m_m": range_error, "static_clearance_along_gt_path_m": route_clearance,
            "target_model": person.get("model"), "target_posture": person.get("posture"),
            "target_source": candidate.get("source"),
            "target_visible_size_m": target_size, "start_gt": started, "end_gt": sample, **arrival}


async def run(episode: Episode) -> dict[str, Any]:
    from sim.worldgen.plan import POSTURE_TOP_M

    if not await episode._ensure_airborne():
        return {**episode._takeoff_failure("vehicle could not take off"), "trials": []}
    trials: list[dict[str, Any]] = []
    standing, visible, reason = await matched_target(episode, "standing")
    if standing is None or visible is None:
        return {"passed": False, "reason": reason, "trials": trials}
    standing_size = POSTURE_TOP_M["standing"] if needs_declared_size(visible) else None
    trials.append(await approach_one(episode, standing, visible, target_size=standing_size,
                                     label="standing-no-target-size"))
    posture = next((p for p in ("lying", "sitting") if any(item.get("posture") == p for item in episode.people)), None)
    if posture is None:
        return {"passed": False, "reason": "manifest contains no lying or sitting target", "trials": trials}
    prone, candidate, reason = await matched_target(episode, posture)
    if prone is None or candidate is None:
        trials.extend([{"label": f"{posture}-no-target-size", "passed": False, "status": "unobservable", "reason": reason},
                       {"label": f"{posture}-with-target-size", "passed": False, "status": "unobservable", "reason": reason}])
        return {"passed": False, "reason": reason, "trials": trials}
    visible_size = POSTURE_TOP_M[posture]
    # A grounder-seeded candidate has no detector person evidence, so the executor cannot
    # range it from an assumed person height; declare the posture's visible size instead.
    first_size = visible_size if needs_declared_size(candidate) else None
    trials.append(await approach_one(episode, prone, candidate, target_size=first_size,
                                     label=f"{posture}-no-target-size"))
    # Approach may have moved away or the tracker ID may have expired; reacquire a live ID.
    reacquired, candidate2, reason = await matched_target(episode, posture)
    if reacquired is not None and candidate2 is not None:
        trials.append(await approach_one(episode, reacquired, candidate2, target_size=visible_size,
                                         label=f"{posture}-with-target-size"))
    else:
        trials.append({"label": f"{posture}-with-target-size", "passed": False,
                       "status": "unobservable", "reason": reason, "target_visible_size_m": visible_size})
    return {"passed": all(trial.get("passed") is True for trial in trials), "trials": trials}
