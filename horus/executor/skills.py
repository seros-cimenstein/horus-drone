"""Skill argument validation at submission time."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from horus.config import ExecutorSettings
from horus.flight import Pose


def outcome_reason(status: str, clamped: bool) -> str:
    """The short code of a finished skill, reported as the task's ``reason``.

    ``clamped`` marks a success that reached a target the executor changed (a turn limited to
    one revolution).
    """
    if status == "succeeded":
        return "clamped" if clamped else "succeeded"
    return status


class SkillContext(NamedTuple):
    settings: ExecutorSettings


Prepared = tuple[Any, Any, str | None]


def prepare(skill: str, args: dict[str, Any], context: SkillContext) -> Prepared:
    """Validate ``args`` in place and return (requested target, executed target, clamp reason)."""
    try:
        preparer = _PREPARERS[skill]
    except KeyError:
        raise ValueError(f"cannot prepare {skill}") from None
    return preparer(skill, args, context)


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


def require_above_home(pose: Pose, skill: str) -> None:
    """Reject a target at or below home altitude: an input check, not a flight limit."""
    if pose.up_m <= 0.0:
        raise ValueError(f"{skill} target altitude must be above home (up_m > 0, got {pose.up_m:g})")


def _prepare_goto(skill: str, args: dict[str, Any], context: SkillContext) -> Prepared:
    if "pose" not in args:
        raise ValueError("goto requires pose")
    requested = parse_pose(args["pose"])
    require_above_home(requested, skill)
    requested_target, executed_target = requested.as_dict(), requested.as_dict()
    _heading(args, requested_target, executed_target)
    return requested_target, executed_target, None


def _prepare_take_off(skill: str, args: dict[str, Any], context: SkillContext) -> Prepared:
    altitude = finite_argument(args.get("altitude_m", 2.0), "altitude_m")
    if altitude <= 0.0:
        raise ValueError("altitude_m must be above 0")
    requested: dict[str, Any] = {"altitude_m": altitude}
    executed: dict[str, Any] = {"altitude_m": altitude}
    _heading(args, requested, executed)
    return requested, executed, None


# A relative turn sweeps at most one full revolution.
MAX_TURN_SWEEP_DEG = 360.0


def _prepare_turn(skill: str, args: dict[str, Any], context: SkillContext) -> Prepared:
    """Turn in place to ``heading_deg`` (shortest way) or by ``by_deg`` (positive = clockwise)."""
    absolute = args.get("heading_deg") is not None
    if absolute == (args.get("by_deg") is not None):
        raise ValueError("turn needs exactly one of heading_deg or by_deg")
    settings = context.settings
    reasons: list[str] = []
    requested: dict[str, Any] = {}
    executed: dict[str, Any] = {}
    if absolute:
        _heading(args, requested, executed)
    else:
        by = finite_argument(args["by_deg"], "by_deg")
        bounded = max(-MAX_TURN_SWEEP_DEG, min(MAX_TURN_SWEEP_DEG, by))
        requested["by_deg"], executed["by_deg"] = by, bounded
        if bounded != by:
            reasons.append(f"turn limited to one revolution ({MAX_TURN_SWEEP_DEG:g} deg)")
    rate = args.get("rate_deg_s")
    if rate is None:
        executed["rate_deg_s"] = settings.turn_rate_deg_s
    else:
        rate = finite_argument(rate, "rate_deg_s")
        if rate <= 0.0:
            raise ValueError("rate_deg_s must be positive")
        # ArduPilot's yaw-rate limits (ATC_RATE_Y_MAX, ATC_SLEW_YAW) bound how fast it turns.
        requested["rate_deg_s"] = executed["rate_deg_s"] = rate
    return requested, executed, "; ".join(reasons) or None


def _prepare_move(skill: str, args: dict[str, Any], context: SkillContext) -> Prepared:
    """Body-frame offsets; the target pose is resolved when the move starts."""
    offsets = {
        name: finite_argument(args.get(name, 0.0), name) for name in ("forward_m", "right_m", "up_m")
    }
    return dict(offsets), dict(offsets), None


def move_target(current: Pose, heading_deg: float, offsets: Mapping[str, float]) -> Pose:
    """The pose ``forward_m``/``right_m``/``up_m`` away from ``current`` along ``heading_deg``."""
    heading = math.radians(heading_deg)
    forward, right = float(offsets["forward_m"]), float(offsets["right_m"])
    return Pose(
        current.north_m + forward * math.cos(heading) - right * math.sin(heading),
        current.east_m + forward * math.sin(heading) + right * math.cos(heading),
        current.up_m + float(offsets["up_m"]),
    )


_PREPARERS: dict[str, Callable[[str, dict[str, Any], SkillContext], Prepared]] = {
    "goto": _prepare_goto,
    "move": _prepare_move,
    "turn": _prepare_turn,
    "take_off": _prepare_take_off,
}
