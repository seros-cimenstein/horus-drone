"""Camera state at frame capture: parsing, drift against a reference, and drift tolerances.

``camera_state_from_pose`` parses the frame-pose contract documented in
``horus.perception.scene``. A missing or stale required component makes the state unavailable;
it is never assumed stationary. Unreported optional components are listed as unmeasured,
never assumed unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any, NamedTuple

# Camera-state freshness and the drift tolerated before a fixed-camera watch pauses.
CAMERA_STATE_MAX_AGE_S = 1.5
CAMERA_HORIZONTAL_TOLERANCE_M = 0.5
CAMERA_VERTICAL_TOLERANCE_M = 0.3
CAMERA_HEADING_TOLERANCE_DEG = 3.0
CAMERA_GIMBAL_TOLERANCE_DEG = 2.0
CAMERA_HFOV_TOLERANCE_DEG = 0.5
# Vehicle attitude drifts are small in a steady hover; braking or gusts exceed this. A stabilized
# gimbal may cancel them, so pausing on them is conservative (never an invented transition).
CAMERA_ATTITUDE_TOLERANCE_DEG = 3.0


class CameraState(NamedTuple):
    """Camera position (local metres), heading and gimbal attitude (degrees) at frame capture.

    Vehicle roll/pitch, gimbal roll and field of view are optional: ``None`` means not measured,
    never level or unchanged.
    """

    north_m: float
    east_m: float
    up_m: float
    heading_deg: float
    gimbal_pitch_deg: float
    gimbal_yaw_deg: float
    hfov_deg: float | None = None
    vehicle_roll_deg: float | None = None
    vehicle_pitch_deg: float | None = None
    gimbal_roll_deg: float | None = None


def camera_state_from_pose(pose: Any) -> tuple[CameraState | None, str | None]:
    """Parse the frame-pose contract; return ``(state, None)`` or ``(None, reason)``."""
    if not isinstance(pose, Mapping):
        return None, "camera_pose_unavailable"
    groups = (
        ("position", ("north_m", "east_m", "up_m"), "pose_age_s"),
        ("heading", ("heading_deg",), "heading_age_s"),
        ("gimbal", ("gimbal_pitch_deg", "gimbal_yaw_deg"), "gimbal_age_s"),
    )
    values: list[float] = []
    for group, keys, age_key in groups:
        numbers = [_finite(pose.get(key)) for key in keys]
        if any(number is None for number in numbers):
            return None, f"{group}_unavailable"
        age = _finite(pose.get(age_key))
        if age is None:
            return None, f"{group}_age_unknown"
        if age > CAMERA_STATE_MAX_AGE_S:
            return None, f"{group}_stale"
        values.extend(numbers)  # type: ignore[arg-type]
    roll = pitch = None
    if "vehicle_roll_deg" in pose or "vehicle_pitch_deg" in pose:
        # Once a provider reports attitude it must stay fresh; stale attitude is not "level".
        roll, pitch = _finite(pose.get("vehicle_roll_deg")), _finite(pose.get("vehicle_pitch_deg"))
        age = _finite(pose.get("attitude_age_s"))
        if roll is None or pitch is None:
            return None, "attitude_unavailable"
        if age is None:
            return None, "attitude_age_unknown"
        if age > CAMERA_STATE_MAX_AGE_S:
            return None, "attitude_stale"
    gimbal_roll = None
    if "gimbal_roll_deg" in pose:
        # Same gimbal sample (and age) as gimbal pitch/yaw.
        gimbal_roll = _finite(pose.get("gimbal_roll_deg"))
        if gimbal_roll is None:
            return None, "gimbal_unavailable"
    return (
        CameraState(*values, _finite(pose.get("camera_hfov_deg")), roll, pitch, gimbal_roll),
        None,
    )


def unmeasured_camera_components(state: CameraState) -> list[str]:
    """Camera degrees of freedom this state cannot vouch for (they may change undetected)."""
    missing = []
    if state.vehicle_roll_deg is None:
        missing.extend(("vehicle_roll", "vehicle_pitch"))
    if state.gimbal_roll_deg is None:
        missing.append("gimbal_roll")
    if state.hfov_deg is None:
        missing.append("zoom_field_of_view")
    return missing


def camera_deviation(reference: CameraState, current: CameraState) -> tuple[dict[str, float], list[str]]:
    """Return absolute drift of measured camera components and the names of exceeded tolerances.

    A component measured in only one of the two states counts as exceeded: it may have changed.
    """
    deviation = {
        "horizontal_m": math.hypot(current.north_m - reference.north_m, current.east_m - reference.east_m),
        "vertical_m": abs(current.up_m - reference.up_m),
        "heading_deg": abs(_wrap_degrees(current.heading_deg - reference.heading_deg)),
        "gimbal_pitch_deg": abs(current.gimbal_pitch_deg - reference.gimbal_pitch_deg),
        "gimbal_yaw_deg": abs(_wrap_degrees(current.gimbal_yaw_deg - reference.gimbal_yaw_deg)),
    }
    exceeded = [name for name, limit in _TOLERANCES.items() if deviation[name] > limit]
    for name, before, after, limit in (
        ("hfov_deg", reference.hfov_deg, current.hfov_deg, CAMERA_HFOV_TOLERANCE_DEG),
        ("vehicle_roll_deg", reference.vehicle_roll_deg, current.vehicle_roll_deg, CAMERA_ATTITUDE_TOLERANCE_DEG),
        ("vehicle_pitch_deg", reference.vehicle_pitch_deg, current.vehicle_pitch_deg, CAMERA_ATTITUDE_TOLERANCE_DEG),
        ("gimbal_roll_deg", reference.gimbal_roll_deg, current.gimbal_roll_deg, CAMERA_GIMBAL_TOLERANCE_DEG),
    ):
        if before is None and after is None:
            continue
        if before is None or after is None:
            exceeded.append(name)
            continue
        deviation[name] = abs(_wrap_degrees(after - before))
        if deviation[name] > limit:
            exceeded.append(name)
    return {name: round(value, 3) for name, value in deviation.items()}, exceeded


_TOLERANCES = {
    "horizontal_m": CAMERA_HORIZONTAL_TOLERANCE_M,
    "vertical_m": CAMERA_VERTICAL_TOLERANCE_M,
    "heading_deg": CAMERA_HEADING_TOLERANCE_DEG,
    "gimbal_pitch_deg": CAMERA_GIMBAL_TOLERANCE_DEG,
    "gimbal_yaw_deg": CAMERA_GIMBAL_TOLERANCE_DEG,
}


def camera_tolerances() -> dict[str, float]:
    """Camera drift limits used for watches and motion ``camera_motion`` classification."""
    return {
        **_TOLERANCES,
        "vehicle_roll_deg": CAMERA_ATTITUDE_TOLERANCE_DEG,
        "vehicle_pitch_deg": CAMERA_ATTITUDE_TOLERANCE_DEG,
        "gimbal_roll_deg": CAMERA_GIMBAL_TOLERANCE_DEG,
        "hfov_deg": CAMERA_HFOV_TOLERANCE_DEG,
        "max_state_age_s": CAMERA_STATE_MAX_AGE_S,
    }


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _wrap_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0
