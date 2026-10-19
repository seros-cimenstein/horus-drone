"""Skill argument validation and envelope clamping at submission time."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from horus.config import ExecutorSettings
from horus.executor.geofence import Envelope
from horus.flight import Pose

# Skills that chase a tracker ID and hold/re-identify it while it is out of view.
TARGET_SKILLS = frozenset({"approach", "follow", "orbit"})
# Skills whose status/result carries a ``tracking`` record.
TRACKING_SKILLS = TARGET_SKILLS | {"search"}


class SkillContext(NamedTuple):
    settings: ExecutorSettings
    envelope: Envelope
    current_pose: Callable[[], Pose | None]
    has_perception: bool


Prepared = tuple[Any, Any, str | None]


def prepare(skill: str, args: dict[str, Any], context: SkillContext) -> Prepared:
    """Validate ``args`` in place and return (requested target, executed target, clamp reason)."""
    try:
        preparer = _PREPARERS[skill]
    except KeyError:
        raise ValueError(f"cannot prepare {skill}") from None
    return preparer(skill, args, context)


def tracking_record(skill: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """The initial ``tracking`` record of a submitted skill (empty for untracked skills)."""
    if skill in TARGET_SKILLS:
        # Every target skill holds and re-identifies a temporarily invisible target, bounded by
        # the task TTL, and reports reacquisition in this shared shape.
        return {
            "range_m": None,
            "range_source": None,
            "reference_id": args["id"],
            "reacquired": False,
            "matched_id": None,
            "time_to_reacquire_s": None,
            "embedding_validated": False,
            "embedding_similarity": None,
            "id_preserved": None,
            "id_rematched": None,
        }
    if skill == "search":
        return {
            "mode": "reidentify" if "reference_id" in args else "cue",
            "class_name": args.get("class_name"),
            "attribute": args.get("attribute"),
            "equals": args.get("equals"),
            "reference_id": args.get("reference_id"),
            "found": False,
            "found_id": None,
            "matching_ids": [],
            "ambiguous": False,
            "similarity": None,
            "frames_checked": 0,
            "found_at_waypoint": None,
        }
    return {}


def parse_pose(value: Any) -> Pose:
    """A finite Pose from a Pose or a ``north_m``/``east_m``/``up_m`` mapping."""
    if isinstance(value, Pose):
        pose = value
    elif isinstance(value, Mapping):
        north = value.get("north_m", value.get("north"))
        east = value.get("east_m", value.get("east"))
        up = value.get("up_m", value.get("altitude_m", value.get("up")))
        if north is None or east is None or up is None:
            raise ValueError("pose needs north_m, east_m, and up_m")
        pose = Pose(float(north), float(east), float(up))
    else:
        raise ValueError("pose must be a mapping")
    if not all(math.isfinite(v) for v in (pose.north_m, pose.east_m, pose.up_m)):
        raise ValueError("pose coordinates must be finite")
    return pose


def finite_argument(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _heading(args: Mapping[str, Any], requested: dict[str, Any], executed: dict[str, Any]) -> None:
    """Record the optional final ``heading_deg``; the executed one is normalised to [0, 360)."""
    heading = args.get("heading_deg")
    if heading is not None:
        heading = finite_argument(heading, "heading_deg")
        requested["heading_deg"] = heading
        executed["heading_deg"] = heading % 360.0


def _prepare_goto(skill: str, args: dict[str, Any], context: SkillContext) -> Prepared:
    if "pose" not in args:
        raise ValueError("goto requires pose")
    requested = parse_pose(args["pose"])
    executed, reasons = context.envelope.clamp_pose(requested, context.current_pose())
    requested_target, executed_target = requested.as_dict(), executed.as_dict()
    _heading(args, requested_target, executed_target)
    return requested_target, executed_target, "; ".join(reasons) or None


def _prepare_take_off(skill: str, args: dict[str, Any], context: SkillContext) -> Prepared:
    settings = context.settings
    altitude = finite_argument(args.get("altitude_m", 2.0), "altitude_m")
    bounded = min(settings.max_altitude_m, max(settings.min_altitude_m, altitude))
    requested: dict[str, Any] = {"altitude_m": altitude}
    executed: dict[str, Any] = {"altitude_m": bounded}
    _heading(args, requested, executed)
    return requested, executed, (
        "altitude clamped to configured flight envelope" if bounded != altitude else None
    )


# Target skill -> (standoff argument, its noun in the clamp reason, default, minimum metres).
_STANDOFFS: dict[str, tuple[str, str, Callable[[ExecutorSettings], float], float]] = {
    "approach": ("dist_m", "distance", lambda settings: 2.0, 0.5),
    "follow": ("dist_m", "distance", lambda settings: settings.follow_distance_m, 0.5),
    "orbit": ("radius_m", "radius", lambda settings: 2.0, 0.75),
}


def _prepare_target(skill: str, args: dict[str, Any], context: SkillContext) -> Prepared:
    _require_target_id(args)
    name, noun, default, minimum = _STANDOFFS[skill]
    value = finite_argument(args.get(name, default(context.settings)), name)
    bounded = min(context.settings.max_distance_m, max(minimum, value))
    requested = {"id": args["id"], name: value}
    executed = {"id": args["id"], name: bounded}
    reason = (
        f"{skill} {noun} clamped to [{minimum:g} m, max_distance_m]" if bounded != value else None
    )
    if args.get("target_size_m") is not None:
        target_size = finite_argument(args["target_size_m"], "target_size_m")
        if target_size <= 0.0:
            raise ValueError("target_size_m must be positive")
        args["target_size_m"] = requested["target_size_m"] = executed["target_size_m"] = target_size
    return requested, executed, reason


def _prepare_area(skill: str, args: dict[str, Any], context: SkillContext) -> Prepared:
    if not isinstance(args.get("area"), Mapping):
        raise ValueError(f"{skill} requires a rectangular area mapping")
    if skill == "search":
        _prepare_search_cue(args, context)
    requested, waypoints, reasons = context.envelope.scan_waypoints(args["area"])
    return requested, [point.as_dict() for point in waypoints], "; ".join(reasons) or None


def _prepare_search_cue(args: dict[str, Any], context: SkillContext) -> None:
    """Search matches either a whole public cue (label + optional attribute) or a lost ID."""
    has_class = args.get("class_name") is not None
    has_reference = args.get("reference_id") is not None
    if has_class == has_reference:
        raise ValueError("search needs exactly one of class_name or reference_id")
    if not context.has_perception:
        raise ValueError("search needs perception")
    has_attribute = args.get("attribute") is not None
    if has_attribute != (args.get("equals") is not None):
        raise ValueError("search attribute and equals must be given together")
    if has_class:
        name = args["class_name"]
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
            raise ValueError("class_name must be a non-empty label of at most 64 characters")
        args["class_name"] = name.strip()
        if has_attribute and (not isinstance(args["attribute"], str) or not args["attribute"].strip()):
            raise ValueError("search attribute must be a non-empty candidate attribute name")
        args.pop("reference_id", None)
    else:
        if has_attribute:
            raise ValueError("search by reference_id takes no attribute cue")
        reference = args["reference_id"]
        if isinstance(reference, bool) or not isinstance(reference, int) or reference < 1:
            raise ValueError("reference_id must be a positive tracker ID")
        args.pop("class_name", None)


def _require_target_id(args: dict[str, Any]) -> None:
    raw_id = args.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
        raise ValueError("target id must be a positive integer")
    try:
        candidate_id = int(raw_id)
    except ValueError as exc:
        raise ValueError("target id must be a positive integer") from exc
    if candidate_id < 1 or str(raw_id).strip() != str(candidate_id):
        raise ValueError("target id must be a positive integer")
    args["id"] = candidate_id


_PREPARERS: dict[str, Callable[[str, dict[str, Any], SkillContext], Prepared]] = {
    "goto": _prepare_goto,
    "take_off": _prepare_take_off,
    "approach": _prepare_target,
    "follow": _prepare_target,
    "orbit": _prepare_target,
    "scan": _prepare_area,
    "search": _prepare_area,
}
