"""Scan and search stepping: fly the clamped raster, and for search check each fresh frame."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from horus.executor.target_range import candidate_value
from horus.executor.target_skills import reidentify_target

if TYPE_CHECKING:
    from horus.executor.core import HorusExecutor, SkillTask


def step_area(executor: HorusExecutor, task: SkillTask) -> bool:
    """One scan/search tick; True when the task is complete."""
    if task.skill == "search":
        state = _search_step(executor, task)
        if state == "found":
            return True
        if state == "camera_stale":
            # Never fly the raster blind: hover until fresh frames return or the TTL expires.
            executor._command_zero_velocity()
            return False
    while task._waypoint_index < len(task._waypoints):
        target, reasons = executor.envelope.clamp_pose(
            task._waypoints[task._waypoint_index], executor._current_pose()
        )
        if reasons:
            task.clamp_reason = "; ".join(filter(None, [task.clamp_reason, *reasons]))
            task._waypoints[task._waypoint_index] = target
        if executor._goto_step(target):
            task._waypoint_index += 1
            continue
        return False
    if task.skill == "search":
        raise RuntimeError("search covered the area without a validated match")
    return True


def _search_step(executor: HorusExecutor, task: SkillTask) -> str | None:
    """Check one newly processed, fresh frame; return ``"found"``, ``"camera_stale"`` or None.

    Matches come from that frame's own candidates and must validate on that same frame, so a
    newer frame arriving mid-check cannot be attributed to the checked one. Every validated
    whole-cue match in the frame is recorded; ``found_id`` is set only when exactly one is
    visible, otherwise the result is ``ambiguous`` (never a guess).
    """
    observation = executor.perception.latest_observation()
    if observation is None:
        return "camera_stale"
    frame_id = getattr(observation, "frame_id", None)
    if not executor._fresh(getattr(observation, "timestamp", None)):
        return "camera_stale"
    if frame_id == task._search_frame_id:
        return None
    task._search_frame_id = frame_id
    tracking = task.tracking
    if tracking["mode"] == "reidentify":
        match = reidentify_target(executor.perception, int(tracking["reference_id"]))
        visible = {candidate_value(candidate, "id"): candidate for candidate in observation.candidates}
        # Like a cue match, a re-identification counts only when validated on this frame.
        if match is not None and match[0] in visible and match[3] == frame_id:
            candidate = visible[match[0]]
            matches = [(match[0], None, [float(v) for v in candidate_value(candidate, "bbox", ())])]
        else:
            matches = []
        similarity = None if not matches else match[1]
    else:
        matches = _search_cue_matches(
            executor, observation, tracking["class_name"], tracking["attribute"], tracking["equals"]
        )
        similarity = None
    with executor._lock:
        executor._require_current_tick()
        tracking["frames_checked"] += 1
    if not matches:
        return None
    executor._command_zero_velocity()
    _record_find(executor, task, observation, matches, similarity)
    return "found"


def _record_find(
    executor: HorusExecutor,
    task: SkillTask,
    observation: Any,
    matches: list[tuple[int, Any, list[float]]],
    similarity: float | None,
) -> None:
    """Record and publish a find under the lock an emergency takes, so a preempted search never
    reports a find after the emergency."""
    tracking = task.tracking
    frame_id = getattr(observation, "frame_id", None)
    matching_ids = [candidate_id for candidate_id, _, _ in matches]
    found_id = matching_ids[0] if len(matching_ids) == 1 else None
    pose = getattr(observation, "pose", None)
    pose = pose if isinstance(pose, Mapping) else {}
    if tracking["mode"] == "reidentify":
        summary = (
            f"Search re-identified lost ID {tracking['reference_id']} as tracker ID "
            f"{found_id} (similarity {similarity:.2f})"
        )
    elif found_id is None:
        summary = f"Search stopped: {len(matching_ids)} visible candidates match the cue (ambiguous: {matching_ids})"
    else:
        summary = f"Search found tracker ID {found_id} {json.dumps(matches[0][1], ensure_ascii=False)}"
    with executor._lock:
        executor._require_current_tick()
        tracking.update(
            found=True,
            found_id=found_id,
            matching_ids=matching_ids,
            ambiguous=found_id is None,
            similarity=similarity,
            found_at_waypoint=task._waypoint_index,
            found_frame_id=frame_id,
        )
        executor._publish_event(
            "target_found",
            summary,
            task_id=task.task_id,
            id=found_id,
            matching_ids=matching_ids,
            ambiguous=found_id is None,
            matches=[
                {
                    "id": candidate_id,
                    "untrusted_detector_label": None if label is None else json.dumps(label, ensure_ascii=False),
                    "bbox": bbox,
                }
                for candidate_id, label, bbox in matches
            ],
            reference_id=tracking["reference_id"],
            similarity=similarity,
            frame_id=frame_id,
            frame_time_boot_ms=pose.get("time_boot_ms"),
            frame_time_boot_age_s=pose.get("time_boot_age_s"),
        )


def _search_cue_matches(
    executor: HorusExecutor, observation: Any, class_name: str, attribute: str | None, equals: Any
) -> list[tuple[int, str, list[float]]]:
    """The frame's candidates with this label (and attribute value) validated on that frame."""
    wanted = class_name.casefold()
    frame_id = getattr(observation, "frame_id", None)
    validate = executor.perception.validate_candidate
    matches = []
    for candidate in observation.candidates:
        if str(candidate_value(candidate, "class_name", "")).casefold() != wanted:
            continue
        if attribute is not None:
            attributes = candidate_value(candidate, "attributes", {})
            if not isinstance(attributes, Mapping) or attributes.get(attribute) != equals:
                continue
        candidate_id = int(candidate_value(candidate, "id"))
        validated = validate(candidate_id)
        if validated is None or candidate_value(validated, "frame_id", frame_id) != frame_id:
            continue
        bbox = [float(value) for value in candidate_value(candidate, "bbox", ())]
        matches.append((candidate_id, str(candidate_value(candidate, "class_name", "")), bbox))
    return sorted(matches)
