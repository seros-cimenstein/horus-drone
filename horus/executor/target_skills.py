"""Approach, follow and orbit stepping: range the target, re-identify it after a loss, and command
the bounded velocity that keeps the vehicle facing it."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from horus.executor.skills import TARGET_SKILLS
from horus.executor.target_range import TOF_BEARING_GATE_RAD, TargetGeometry, TargetNotVisible

if TYPE_CHECKING:
    from horus.executor.core import HorusExecutor, SkillTask
    from horus.executor.perception_port import PerceptionPort


logger = logging.getLogger(__name__)


def step_target(executor: HorusExecutor, task: SkillTask) -> bool:
    """One approach/follow/orbit tick; True when an approach has arrived."""
    geometry = _target_geometry(executor, task)
    if geometry is None:
        return False
    bearing, distance = geometry.bearing_rad, geometry.range_m
    _record_range(task, geometry)
    telemetry = executor._telemetry()
    # Target motion rotates the camera bearing by the vehicle heading; both must be current.
    executor._require_fresh(telemetry.pose_updated_monotonic, "pose")
    executor._require_fresh(telemetry.heading_updated_monotonic, "heading")
    heading = math.radians(float(telemetry.heading_deg or 0.0)) + bearing
    facing_deg = math.degrees(heading) % 360.0
    task._last_target_facing_deg = facing_deg
    logger.debug(
        "%s target=%s bearing_deg=%.1f range_m=%s source=%s facing_deg=%.1f",
        task.skill, task.arguments["id"], math.degrees(bearing),
        "none" if distance is None else f"{distance:.2f}", geometry.range_source, facing_deg,
    )
    if task.skill in ("approach", "follow"):
        _raise_standoff_to_view_limit(executor, task, geometry)
        _remember_target_position(task, geometry, telemetry, heading)
    if task.skill == "follow":
        desired = float(task.executed_target["dist_m"])
        if distance is None:
            # Safe visual following: turn toward the target's bearing without translating
            # until a calibrated range estimate is available.
            _turn_to(executor, facing_deg)
            return False
        _range_velocity(executor, task, heading, distance - desired, yaw_deg=facing_deg)
        return False
    if task.skill in ("approach", "orbit") and distance is None:
        # Turn in place until the target is inside the forward ToF's bearing gate.
        if abs(bearing) > TOF_BEARING_GATE_RAD:
            _turn_to(executor, facing_deg)
            return False
        label = geometry.label or "unknown"
        hint = (
            f"detector label {label!r} has no person evidence; pass target_size_m or configure "
            "target_height_m for a person target"
        )
        if task.skill == "approach":
            raise RuntimeError(
                f"approach needs a calibrated target range ({hint}); refusing unbounded "
                "forward motion"
            )
        raise RuntimeError(f"orbit needs a calibrated target range ({hint}); refusing open-loop orbit")
    if task.skill == "approach":
        desired = float(task.executed_target["dist_m"])
        if distance <= desired + executor.settings.arrival_tolerance_m:
            _turn_to(executor, facing_deg)
            return True
        progress = _range_velocity(executor, task, heading, distance - desired, yaw_deg=facing_deg)
        if progress < _MIN_APPROACH_PROGRESS_M_S:
            raise RuntimeError(
                f"approach stopped at the geofence edge {distance:.1f} m from the target "
                f"(standoff {desired:g} m): the target lies beyond the fence"
            )
        return False
    if task.skill == "orbit":
        radius = float(task.executed_target["radius_m"])
        _orbit_velocity(executor, task, heading, distance, radius, yaw_deg=facing_deg)
        return False
    raise RuntimeError(f"unsupported target skill {task.skill}")


def _record_range(task: SkillTask, geometry: TargetGeometry) -> None:
    """Report the range and its source; warn once when the source needs a caveat."""
    range_source, range_note = geometry.range_source, geometry.range_note
    task.tracking.update({"range_m": geometry.range_m, "range_source": range_source})
    if range_note is None:
        task.tracking.pop("range_source_note", None)
        return
    task.tracking["range_source_note"] = range_note
    if not task._range_note_warned:
        task._range_note_warned = True
        logger.warning(
            "%s target=%s: %s%s",
            task.skill,
            task.arguments["id"],
            range_note,
            " (a non-person target needs target_size_m)"
            if range_source == "person_height"
            else "",
        )


def _raise_standoff_to_view_limit(
    executor: HorusExecutor, task: SkillTask, geometry: TargetGeometry
) -> None:
    """Raise the standoff to the closest range that keeps the target inside the camera's view.

    From high above (the GTA profile flies at 6 m or more), a standoff shorter than that range is
    unreachable: closing on it would push the target out of the bottom of the frame and the skill
    would lose it. The raised standoff is reported in ``clamp_reason`` like the other clamps.
    """
    limit = geometry.view_limit_m
    desired = float(task.executed_target["dist_m"])
    # Raise only past the arrival tolerance, so box jitter does not keep rewriting the standoff.
    if limit is None or limit <= desired + executor.settings.arrival_tolerance_m:
        return
    raised = round(limit, 2)
    task.executed_target["dist_m"] = raised
    reason = (
        f"{task.skill} distance raised to {raised:g} m: closer than that, the target leaves the "
        f"camera's view from this height"
    )
    previous = [
        part for part in (task.clamp_reason or "").split("; ")
        if part and " distance raised to " not in part
    ]
    task.clamp_reason = "; ".join([*previous, reason])
    logger.info("%s target=%s: %s", task.skill, task.arguments["id"], reason)


def _remember_target_position(
    task: SkillTask, geometry: TargetGeometry, telemetry: Any, heading: float
) -> None:
    """Store the target's ground position and the stop distance for closing on it if it is lost."""
    pose = telemetry.pose
    if geometry.horizontal_m is None or pose is None:
        return
    horizontal = float(geometry.horizontal_m)
    task._last_target_ned = (
        pose.north_m + horizontal * math.cos(heading),
        pose.east_m + horizontal * math.sin(heading),
    )
    # The horizontal distance at which the (possibly raised) standoff is met: the range's vertical
    # part is the same at any horizontal distance from this height.
    desired = float(task.executed_target["dist_m"])
    vertical_sq = max(0.0, float(geometry.range_m or 0.0) ** 2 - horizontal**2)
    task._last_target_stop_m = math.sqrt(max(0.0, desired**2 - vertical_sq))


def _target_geometry(executor: HorusExecutor, task: SkillTask) -> TargetGeometry | None:
    """Geometry of the task's target, re-identifying it after a loss; None while it is lost.

    While the target is not validated on the current frame (or not yet re-identified by the
    stored reference), the vehicle closes on its last validated ground position and then holds
    facing it (``_close_on_last_position``); this lasts at most the task TTL.
    """
    ranger = executor._ranger
    target_id = task.arguments["id"]
    target_size_m = task.executed_target.get("target_size_m")
    try:
        geometry = ranger.geometry(target_id, target_size_m, executor._telemetry())
    except TargetNotVisible as exc:
        if task.skill not in TARGET_SKILLS:
            raise
        if task._target_lost_at is None:
            task._target_lost_at = time.monotonic()
            task._target_lost_at_sim_s = executor._sim_time_s(executor._telemetry())
            logger.info("%s target %s lost: %s", task.skill, target_id, exc)
            executor._publish_event(
                "target_lost",
                f"{task.skill.capitalize()} target lost: {exc}",
                task_id=task.task_id,
                reference_id=task.tracking["reference_id"],
            )
        match = reidentify_target(executor.perception, task.tracking["reference_id"])
        if match is None:
            logger.debug(
                "%s target %s not visible and not re-identified; closing on its last position",
                task.skill,
                target_id,
            )
            _close_on_last_position(executor, task)
            return None
        target_id, similarity, threshold, _ = match
        try:
            geometry = ranger.geometry(target_id, target_size_m, executor._telemetry())
        except TargetNotVisible:
            _close_on_last_position(executor, task)
            return None
    else:
        if task._target_lost_at is None:
            return geometry
        match = reidentify_target(executor.perception, task.tracking["reference_id"])
        if match is None:
            _close_on_last_position(executor, task)
            return None
        target_id, similarity, threshold, _ = match
        if target_id != task.arguments["id"]:
            geometry = ranger.geometry(target_id, target_size_m, executor._telemetry())
    task.arguments["id"] = target_id
    task.executed_target["id"] = target_id
    _record_reacquisition(executor, task, target_id, similarity, threshold)
    return geometry


def reidentify_target(
    perception: PerceptionPort, reference_id: int
) -> tuple[int, float, float, Any] | None:
    """(candidate id, similarity, threshold, frame id of the validating frame) or None."""
    match = perception.reidentify_candidate(reference_id)
    if not isinstance(match, Mapping):
        return None
    candidate = match.get("candidate")
    if not isinstance(candidate, Mapping):
        return None
    try:
        candidate_id = int(candidate["id"])
        similarity = float(match["similarity"])
        threshold = float(match["similarity_threshold"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if (
        candidate_id < 1
        or not math.isfinite(similarity)
        or not math.isfinite(threshold)
        or not 0.0 <= threshold <= 1.0
        or similarity < threshold
        or similarity > 1.0 + 1e-6
    ):
        return None
    return candidate_id, min(1.0, similarity), threshold, candidate.get("frame_id")


def _record_reacquisition(
    executor: HorusExecutor, task: SkillTask, candidate_id: int, similarity: float, threshold: float
) -> None:
    if task._target_lost_at is None:
        return
    # Occlusions are timed in simulation/autopilot time when available (SYSTEM_TIME), so the
    # measured gap matches the scenario's design at any real-time factor; wall time otherwise.
    now_sim = executor._sim_time_s(executor._telemetry())
    if now_sim is not None and task._target_lost_at_sim_s is not None:
        gap, basis = max(0.0, now_sim - task._target_lost_at_sim_s), "autopilot_time_boot"
    else:
        gap, basis = max(0.0, time.monotonic() - task._target_lost_at), "wall"
    reference_id = task.tracking["reference_id"]
    event = {
        "matched_id": candidate_id,
        "time_to_reacquire_s": gap,
        "time_basis": basis,
        "embedding_similarity": similarity,
        "id_preserved": candidate_id == reference_id,
    }
    events = task.tracking.setdefault("reacquisitions", [])
    events.append(event)
    longest = max(events, key=lambda item: item["time_to_reacquire_s"])
    # Summary fields describe the longest occlusion, i.e. the scenario's designed one.
    task.tracking.update(
        {
            "reacquired": True,
            "matched_id": longest["matched_id"],
            "time_to_reacquire_s": longest["time_to_reacquire_s"],
            "time_basis": longest["time_basis"],
            "embedding_validated": True,
            "embedding_similarity": longest["embedding_similarity"],
            "embedding_similarity_threshold": threshold,
            "id_preserved": longest["id_preserved"],
            "id_rematched": not longest["id_preserved"],
        }
    )
    executor._publish_event(
        "target_reacquired",
        f"{task.skill.capitalize()} target reacquired",
        task_id=task.task_id,
        reference_id=reference_id,
        matched_id=candidate_id,
        similarity=similarity,
    )
    task._target_lost_at = None
    task._target_lost_at_sim_s = None
    task.tracking.pop("closing_on_last_position_m", None)


def _turn_to(executor: HorusExecutor, yaw_deg: float | None) -> None:
    """Hover in place while turning to ``yaw_deg``."""
    executor._command(executor.flight.set_velocity_ned, 0.0, 0.0, 0.0, yaw_deg=yaw_deg)


def _hold_facing_last_target(executor: HorusExecutor, task: SkillTask) -> None:
    """Hold position while the target is lost, turned toward where it was last validated.

    Freezing yaw instead leaves a target that walked out past the image edge outside the view
    for as long as it stands there; finishing the turn brings that edge back into the frame.
    """
    _turn_to(executor, task._last_target_facing_deg)


def _close_on_last_position(executor: HorusExecutor, task: SkillTask) -> None:
    """While the target is lost, close on its last validated ground position, then hold there.

    Holding in place leaves a target that dropped out of a small, distant view (a pedestrian near
    the top of a downward-pitched frame) small and distant, so it may never be re-identified.
    Closing at half speed toward where it was, facing that point, to the standoff's horizontal
    distance, brings that spot into a larger, central part of the frame. Re-identification by the
    stored reference still decides when the skill resumes; no other candidate is taken. Without a
    ground-plane position (indoors, ranged by ToF or size) the vehicle holds as before.
    """
    last = task._last_target_ned
    pose = executor._telemetry().pose
    if last is None or pose is None:
        _hold_facing_last_target(executor, task)
        return
    north, east = last[0] - pose.north_m, last[1] - pose.east_m
    horizontal = math.hypot(north, east)
    facing_deg = (
        math.degrees(math.atan2(east, north)) % 360.0 if horizontal > 1e-6 else task._last_target_facing_deg
    )
    gap = horizontal - task._last_target_stop_m
    task.tracking["closing_on_last_position_m"] = round(max(0.0, gap), 2)
    if gap <= executor.settings.arrival_tolerance_m:
        _turn_to(executor, facing_deg)
        return
    speed = min(executor.settings.max_speed_m_s * 0.5, gap * 0.5)
    _send_velocity(executor, task, speed * north / horizontal, speed * east / horizontal, facing_deg)


# Below this speed toward the target (after the geofence limit) an approach makes no progress.
_MIN_APPROACH_PROGRESS_M_S = 0.05


def _send_velocity(
    executor: HorusExecutor, task: SkillTask, north: float, east: float, yaw_deg: float | None
) -> tuple[float, float]:
    """Command a horizontal target-skill velocity limited to the geofence; returns what was sent.

    goto and scan clamp their targets; approach, follow and orbit steer toward a tracked
    target by velocity, so the fence is applied to the velocity itself (Envelope.limit_velocity).
    """
    pose = executor._current_pose()
    if pose is None:
        raise RuntimeError(f"{task.skill} needs a pose to keep inside the geofence")
    north, east, limited = executor.envelope.limit_velocity(pose, north, east)
    if limited and not task.tracking.get("fence_limited"):
        task.tracking["fence_limited"] = True
        logger.warning("%s target=%s: motion limited at the geofence", task.skill, task.arguments["id"])
        executor._publish_event(
            "fence_limit", f"{task.skill.capitalize()} motion limited at the geofence", task_id=task.task_id
        )
    executor._command(executor.flight.set_velocity_ned, north, east, 0.0, yaw_deg=yaw_deg)
    return north, east


def _range_velocity(
    executor: HorusExecutor, task: SkillTask, heading: float, range_error: float, *, yaw_deg: float | None = None
) -> float:
    """Close the range along ``heading``; returns the speed sent in the closing direction (m/s)."""
    max_speed = executor.settings.max_speed_m_s
    speed = min(max_speed, max(0.0, abs(range_error) * 0.5))
    direction = 1.0 if range_error > 0 else -1.0
    north, east = _send_velocity(
        executor, task, direction * speed * math.cos(heading), direction * speed * math.sin(heading), yaw_deg
    )
    return direction * (north * math.cos(heading) + east * math.sin(heading))


def _orbit_velocity(
    executor: HorusExecutor,
    task: SkillTask,
    bearing: float,
    distance: float,
    radius: float,
    *,
    yaw_deg: float | None = None,
) -> None:
    max_speed = executor.settings.max_speed_m_s
    radial_speed = min(max_speed * 0.5, abs(distance - radius) * 0.5)
    radial_sign = 1.0 if distance > radius else -1.0
    tangent_speed = min(max_speed * 0.5, max(0.2, radius * 0.35))
    # Move toward target when outside the orbit, away when inside; add a tangent.
    toward_n = math.cos(bearing) * radial_sign * radial_speed
    toward_e = math.sin(bearing) * radial_sign * radial_speed
    tangent_n = math.cos(bearing + math.pi / 2.0) * tangent_speed
    tangent_e = math.sin(bearing + math.pi / 2.0) * tangent_speed
    north, east = toward_n + tangent_n, toward_e + tangent_e
    magnitude = math.hypot(north, east)
    if magnitude > max_speed:
        north *= max_speed / magnitude
        east *= max_speed / magnitude
    # Face the target while circling; translating sideways would carry it out of the camera view.
    _send_velocity(executor, task, north, east, yaw_deg)
