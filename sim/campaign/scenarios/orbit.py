"""Orbit scenario: a 6 m orbit around a GT-matched, appearance-validated person."""
from __future__ import annotations

from collections.abc import Mapping
import math
import statistics
from typing import Any

from sim.campaign.camera_geometry import torso
from sim.campaign.episode import CLEARANCE_THRESHOLD_M, Episode, clearance_below_threshold

ORBIT_RADIUS_M = 6.0
# How many GT-matched people the orbit test may visit while looking for a clear circle.
ORBIT_TARGET_ATTEMPTS = 4


def orbit_outcome(*, status: str, validated: bool, reference_views: int,
                  mean_radius_m: float | None) -> tuple[bool, str, str | None]:
    """Pass/classification/reason for an orbit task.

    A target that never left its creation view (the multi-view gallery admitted nothing) was
    never appearance-validated, which is a different failure from an orbit that flew but missed
    its geometry or behaviour.
    """
    passed = status in {"succeeded", "expired"} and mean_radius_m is not None and validated
    if passed:
        return True, "passed", None
    if not validated:
        return False, "never_validated", (
            f"orbit target was never appearance-validated before the orbit started "
            f"(reference_views {reference_views})")
    if mean_radius_m is None:
        return False, "orbit_behaviour", "orbit produced no ground-truth samples"
    return False, "orbit_behaviour", f"orbit task ended {status}"


def orbit_circle_clearance(episode: Episode, target: tuple[float, float, float], *, altitude: float) -> float:
    """Minimum static clearance on the orbit circle flown at ``altitude``."""
    count = 48
    circle = [(target[0] + ORBIT_RADIUS_M * math.cos(2 * math.pi * i / count),
               target[1] + ORBIT_RADIUS_M * math.sin(2 * math.pi * i / count), altitude) for i in range(count + 1)]
    return min((episode.world.segment_clearance(a, b, radius=0.6)[0] for a, b in zip(circle, circle[1:])),
               default=math.inf)


async def matched_orbit_target(
    episode: Episode,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None, str | None, list[dict[str, Any]], float | None]:
    """Choose a GT-matched visible target whose orbit circle is clear of static geometry.

    Tries several people before giving up: the first GT-matched person may stand too close
    to a tree, while another person in the same world supports the 6 m / 1.5 m orbit.
    """
    pool = list(episode.people)
    episode.rng.shuffle(pool)
    attempts: list[dict[str, Any]] = []
    matched_seen = 0
    best: tuple[Mapping[str, Any], Mapping[str, Any], float] | None = None
    for index, person in enumerate(pool[:max(12, episode.args.vantages * 3)]):
        if matched_seen >= ORBIT_TARGET_ATTEMPTS:
            break
        candidate, reason = await episode._observe_matched_person(person, f"orbit-target-{index:02d}")
        if candidate is None:
            attempts.append({"model": person.get("model"), "reason": reason})
            continue
        matched_seen += 1
        current = episode._drone()
        target = episode._people_gt().get(str(person.get("model")))
        if current is None or target is None:
            attempts.append({"model": person.get("model"), "target_id": candidate.get("id"),
                             "reason": "missing current GT pose"})
            continue
        clearance = orbit_circle_clearance(episode, target, altitude=current[2])
        if clearance_below_threshold(clearance):
            attempts.append({"model": person.get("model"), "target_id": candidate.get("id"),
                             "min_static_clearance_m": clearance,
                             "reason": f"orbit circle clearance below {CLEARANCE_THRESHOLD_M:.1f} m"})
            if best is None or clearance > best[2]:
                best = (person, candidate, clearance)
            continue
        attempts.append({"model": person.get("model"), "target_id": candidate.get("id"),
                         "min_static_clearance_m": clearance, "reason": None})
        return person, candidate, None, attempts, clearance
    if best is not None:
        return (best[0], best[1],
                f"orbit circle clearance below {CLEARANCE_THRESHOLD_M:.1f} m for every visible "
                f"candidate ({matched_seen} matched)", attempts, best[2])
    return None, None, "no current candidate GT-matched to any target", attempts, None


def _orbit_ground_truth(episode: Episode, person: Mapping[str, Any], start_index: int) -> tuple[list[float], int]:
    """Horizontal drone-to-torso distances over the orbit and how many samples kept it in frame."""
    distances: list[float] = []
    visible = 0
    for sample in episode.path_history[start_index:]:
        drone = episode._drone(sample)
        poses = sample.get("models", {})
        target_pose = poses.get(str(person.get("model"))) if isinstance(poses, Mapping) else None
        if drone is None or not isinstance(target_pose, list) or len(target_pose) < 3:
            continue
        point = torso(person, tuple(float(v) for v in target_pose[:3]))
        distances.append(math.hypot(drone[0] - point[0], drone[1] - point[1]))
        if episode.camera.in_frame(drone[:3], drone[3], point):
            visible += 1
    return distances, visible


async def run(episode: Episode) -> dict[str, Any]:
    if not await episode._ensure_airborne():
        return episode._takeoff_failure("vehicle could not take off")
    person, candidate, reason, attempts, minimum = await matched_orbit_target(episode)
    if person is None or candidate is None:
        return {"passed": False, "reason": reason, "status": "skipped",
                "target_attempts": attempts, "min_static_clearance_m": minimum}
    # The multi-view gallery admits views only after the tracker validates the appearance;
    # a target still on its creation view was never validated, which is a different failure
    # from an orbit that flew but missed its geometry.
    reference_views = int(candidate.get("reference_views") or 1)
    validated = reference_views > 1
    validation = {"target_reference_views": reference_views,
                  "target_validated_before_orbit": validated}
    current = episode._drone()
    target = episode._people_gt().get(str(person.get("model")))
    if current is None or target is None:
        return {"passed": False, "reason": "missing current GT pose", "target_attempts": attempts,
                **validation}
    minimum = orbit_circle_clearance(episode, target, altitude=current[2])
    if clearance_below_threshold(minimum):
        # The circle was re-checked at the settled vantage altitude; trust the stricter value.
        return {"passed": False, "status": "skipped",
                "reason": f"orbit circle clearance below {CLEARANCE_THRESHOLD_M:.1f} m",
                "min_static_clearance_m": minimum, "target_attempts": attempts,
                "classification": "orbit_geometry", **validation}
    start_index = len(episode.path_history)
    try:
        # The wall budget must outlast the submitted TTL (240 s), otherwise the harness
        # cancels the task before the executor's own expiry can be observed.
        status = await episode._skill("orbit", {"id": candidate["id"], "radius_m": ORBIT_RADIUS_M, "ttl_s": 240.0},
                                      timeout_s=300.0)
    except Exception as exc:
        return {"passed": False, "reason": f"orbit failed: {exc}", "min_static_clearance_m": minimum,
                "target_attempts": attempts, "classification": "orbit_behaviour", **validation}
    episode._record_gt()
    distances, visible = _orbit_ground_truth(episode, person, start_index)
    mean = statistics.mean(distances) if distances else None
    std = statistics.pstdev(distances) if len(distances) > 1 else (0.0 if distances else None)
    passed, classification, reason = orbit_outcome(
        status=str(status.get("status")), validated=validated, reference_views=reference_views,
        mean_radius_m=mean)
    return {"passed": passed, "reason": reason, "classification": classification,
            "task_status": status, "target_model": person.get("model"),
            "target_source": candidate.get("source"), "requested_radius_m": ORBIT_RADIUS_M,
            "gt_radius_mean_m": mean, "gt_radius_std_m": std,
            "gt_radius_error_m": None if mean is None else mean - ORBIT_RADIUS_M,
            "target_in_frame_fraction": visible / len(distances) if distances else None,
            "gt_samples": len(distances), "min_static_clearance_m": minimum,
            "target_attempts": attempts, **validation}
