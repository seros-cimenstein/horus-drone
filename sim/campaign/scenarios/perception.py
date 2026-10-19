"""Perception scenario: GT-matched person detection from planned vantages, including an occluded one."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import math
import statistics
from typing import Any

from sim.campaign.camera_geometry import POSE_SOURCE
from sim.campaign.episode import Episode
from sim.campaign.scoring import score_detection_rows

PROJECTION_MIN_MATCHES = 3
PROJECTION_MAX_ERROR_PX = 15.0


def projection_validation(errors: Iterable[float]) -> dict[str, Any]:
    valid_errors = [
        float(error) for error in errors
        if isinstance(error, (int, float)) and not isinstance(error, bool) and math.isfinite(float(error))
    ]
    median_error = statistics.median(valid_errors) if valid_errors else None
    return {
        "confident_match_count": len(valid_errors),
        "median_pixel_error": median_error,
        "minimum_confident_matches": PROJECTION_MIN_MATCHES,
        "threshold_px": PROJECTION_MAX_ERROR_PX,
        "passed": len(valid_errors) >= PROJECTION_MIN_MATCHES
        and median_error is not None and median_error <= PROJECTION_MAX_ERROR_PX,
    }


def vantage_candidates(episode: Episode) -> list[Any]:
    """Collision-safe clear vantages on random people, plus one occluded vantage when possible."""
    from sim.worldgen.plan import person_point, plan_vantage

    wanted = episode.args.vantages
    available = [person for person in episode.people if person.get("posture") in {"standing", "sitting", "lying"}]
    if not available:
        return []
    order = episode.rng.sample(available, k=min(len(available), max(wanted * 2, wanted)))
    vantages: list[Any] = []
    blocked = None
    for person in order:
        point = person_point(person, episode.ground, str(person["posture"]))
        vantage = plan_vantage(episode.world, episode.ground, episode.camera, str(person["model"]), point,
                               episode.rng, transit_alt=episode.transit_alt, require_los=False,
                               min_clearance_m=1.5, bounds=episode.vantage_bounds_m, attempts=160)
        if vantage is None:
            continue
        if vantage.blocker and blocked is None:
            blocked = vantage
        elif vantage.blocker is None:
            vantages.append(vantage)
        if len(vantages) >= wanted and blocked is not None:
            break
    if blocked is not None:
        if len(vantages) >= wanted:
            vantages[-1] = blocked
        else:
            vantages.append(blocked)
    return vantages[:wanted]


def _camera_pose_summary(vantage_frames: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    camera_poses = [row["actual_gt_camera_pose"] for row in vantage_frames
                    if isinstance(row.get("actual_gt_camera_pose"), Mapping)]
    pitch_values = [float(pose["pitch_deg"]) for pose in camera_poses]
    yaw_offset_values = [float(pose["yaw_offset_deg"]) for pose in camera_poses
                         if isinstance(pose.get("yaw_offset_deg"), (float, int))]
    return {
        "vantages": len(camera_poses),
        "pitch_deg": {
            "mean": statistics.mean(pitch_values) if pitch_values else None,
            "std": statistics.pstdev(pitch_values) if pitch_values else None,
        },
        "yaw_offset_deg": {
            "mean": statistics.mean(yaw_offset_values) if yaw_offset_values else None,
            "std": statistics.pstdev(yaw_offset_values) if yaw_offset_values else None,
        },
    }


def _confident_errors(rows: Iterable[Mapping[str, Any]]) -> list[float]:
    """Projection errors of detected people whose candidate confidence is at least 0.5."""
    return [
        float(row["center_error_px"]) for row in rows
        if row.get("detected") and isinstance(row.get("center_error_px"), (int, float))
        and isinstance(row.get("raw_candidate"), Mapping)
        and float(row["raw_candidate"].get("confidence", 0.0)) >= 0.5
    ]


async def run(episode: Episode) -> dict[str, Any]:
    if not await episode._ensure_airborne():
        return {"passed": False, "reason": "Gazebo ground truth did not reach transit altitude after takeoff",
                "ground_truth_drone": episode._drone(),
                "takeoff_altitude_samples": episode._takeoff_altitude_samples, "person_observations": []}
    vantages = vantage_candidates(episode)
    if not vantages:
        return {"passed": False, "reason": "no collision-safe vantage could be planned", "person_observations": []}
    observations: list[dict[str, Any]] = []
    clearance_skips: list[dict[str, Any]] = []
    projection_errors: list[float] = []
    projection_frame_ids: list[Any] = []
    vantage_frames: list[dict[str, Any]] = []
    for index, vantage in enumerate(vantages, 1):
        route = await episode._navigate(vantage.eye, vantage.yaw_rad)
        if not route.get("passed"):
            clearance_skips.append({"target": vantage.target, **route})
            continue
        if not await episode._wait_gt(vantage.eye, tolerance=0.5, settle_sim_s=3.0):
            clearance_skips.append({"target": vantage.target, "reason": "GT position did not settle within 0.5 m"})
            continue
        frame = await episode._observe(f"vantage-{index:02d}")
        rows, false_positives, matches = episode._detection_rows(frame)
        vantage_frames.append({
            "vantage": index, "frame_id": frame.get("frame_id"), "timestamp": frame.get("timestamp"),
            "capture_sim_time": frame.get("capture_sim_time"), "target": vantage.target,
            "planned_eye": vantage.eye, "planned_slant_m": vantage.slant_m,
            "planned_depression_deg": vantage.depression_deg, "planned_blocker": vantage.blocker,
            "actual_gt_camera_pose": matches["camera_pose"], "route_min_clearance_m": route.get("min_clearance_m"),
            "matched": {name: item.get("id") for name, item in matches["matches"].items()},
            "false_positives": false_positives, "people": rows,
            "raw_candidates": frame.get("candidate_table"), "saved_frame": frame.get("saved_frame"),
        })
        observations.extend(rows)
        frame_errors = _confident_errors(rows)
        if frame_errors:
            projection_errors.extend(frame_errors)
            projection_frame_ids.append(frame.get("frame_id"))
    validation = projection_validation(projection_errors)
    validation.update({"frames_with_confident_matches": projection_frame_ids,
                       "pose_source": POSE_SOURCE, "camera_info": episode.camera_info})
    confident_match_count = validation["confident_match_count"]
    passed = bool(observations) and validation["passed"]
    if passed:
        reason = None
    elif confident_match_count < PROJECTION_MIN_MATCHES:
        reason = (f"only {confident_match_count} confident matched people across all perception vantages; "
                  f"at least {PROJECTION_MIN_MATCHES} required")
    else:
        reason = (f"episode-wide camera projection median error {validation['median_pixel_error']:.1f} px "
                  f"exceeds {PROJECTION_MAX_ERROR_PX:.0f} px")
    return {
        "passed": passed, "reason": reason,
        "requested_vantages": episode.args.vantages, "visited_vantages": len(vantage_frames),
        "vantages": len(vantages), "clearance_skips": clearance_skips,
        "occluded_vantage_included": any(vantage.blocker is not None for vantage in vantages),
        "occluded_people_observed": any(row.get("los_blocked") for row in observations),
        "projection_validation": validation,
        "camera_pose_summary": _camera_pose_summary(vantage_frames), "metrics": score_detection_rows(observations),
        "person_observations": observations, "vantage_frames": vantage_frames,
        "takeoff_altitude_samples": episode._takeoff_altitude_samples,
    }
