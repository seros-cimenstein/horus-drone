"""Bearing and range to a validated target from its box, the camera geometry and range sensors."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any, NamedTuple

from horus.config import ExecutorSettings
from horus.executor.perception_port import PerceptionPort
from horus.flight import Telemetry

# The forward ToF ranges a target only within this bearing of the optical axis; outside it the
# target skills turn in place first.
TOF_BEARING_GATE_RAD = math.radians(8.0)


class TargetNotVisible(RuntimeError):
    """A tracked ID is temporarily absent from the current camera frame."""


class TargetGeometry(NamedTuple):
    """Validated target geometry, the range source used, and the detector label seen."""

    bearing_rad: float
    range_m: float | None
    range_source: str | None
    range_note: str | None
    label: str


class _GroundFix(NamedTuple):
    """The box's ground-contact ray intersected with the ground plane."""

    slant_m: float
    bearing_rad: float
    horizontal_m: float
    height_m: float
    pitch_deg: float


def candidate_value(candidate: Any, name: str, default: Any = None) -> Any:
    """A field of a candidate row, which perception returns as a mapping or an object."""
    if isinstance(candidate, Mapping):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def candidate_box(candidate: Any) -> tuple[float, float, float, float]:
    """The candidate's ``bbox`` as finite ``(x1, y1, x2, y2)`` pixels."""
    try:
        x1, y1, x2, y2 = (float(value) for value in candidate_value(candidate, "bbox"))
    except (TypeError, ValueError):
        raise RuntimeError("candidate has no usable bbox") from None
    if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
        raise RuntimeError("candidate has no usable bbox")
    return x1, y1, x2, y2


def _frame_size(candidate: Any) -> tuple[float, float] | None:
    """Positive ``(width, height)`` of the candidate's frame, or None when the height is unknown."""
    frame_height = candidate_value(candidate, "frame_height")
    if frame_height is None:
        return None
    try:
        image_height = float(frame_height)
        image_width = float(candidate_value(candidate, "frame_width", 640.0))
    except (TypeError, ValueError, OverflowError):
        return None
    if image_height <= 0.0 or image_width <= 0.0:
        return None
    return image_width, image_height


class TargetRanger:
    """Turn a validated candidate and the current telemetry into a bearing and a range."""

    def __init__(self, settings: ExecutorSettings, perception: PerceptionPort | None):
        self.settings = settings
        self.perception = perception

    def fresh(self, updated_monotonic: float | None) -> bool:
        """Whether a sample received at ``updated_monotonic`` is recent enough to act on."""
        if updated_monotonic is None:
            return False
        age = time.monotonic() - float(updated_monotonic)
        return math.isfinite(age) and age <= self.settings.telemetry_max_age_s

    def focal_px(self, image_width: float) -> float:
        """Pinhole focal length in pixels for the configured horizontal field of view."""
        return image_width / (2.0 * math.tan(math.radians(self.settings.camera_horizontal_fov_deg) / 2.0))

    def geometry(
        self, target_id: Any, target_size_m: float | None, telemetry: Telemetry
    ) -> TargetGeometry:
        """Revalidate ``target_id`` on the current frame and range it from the best source.

        Raises TargetNotVisible when the ID is not validated on the current frame.
        """
        if self.perception is None:
            raise RuntimeError("perception identity revalidation is required before target action")
        candidate = self.perception.validate_candidate(target_id)
        if candidate is None:
            raise TargetNotVisible(self._not_visible_message(target_id))
        profile_score = candidate_value(candidate, "target_match_confidence")
        if profile_score is not None:
            profile_score = float(profile_score)
            if not math.isfinite(profile_score) or profile_score < self.settings.target_match_threshold:
                raise RuntimeError("target did not meet the configured profile-match confidence")
        x1, _, x2, _ = box = candidate_box(candidate)
        image_width = float(candidate_value(candidate, "frame_width", 640.0))
        focal_px = self.focal_px(image_width)
        bearing = math.atan2((x1 + x2) * 0.5 - image_width * 0.5, focal_px)
        # The ground-plane bearing accounts for the camera pitch; the image bearing does not.
        ground = self._ground_plane(box, candidate, telemetry)
        if ground is not None:
            bearing = ground.bearing_rad
        class_name = str(candidate_value(candidate, "class_name", "")).strip().lower()
        range_m, range_source, range_note = self._range(
            candidate, box, focal_px, class_name, bearing, ground, target_size_m, telemetry
        )
        confidence = float(candidate_value(candidate, "confidence", 1.0))
        if not math.isfinite(bearing) or not math.isfinite(confidence) or confidence < 0.2:
            raise RuntimeError("target identity/confidence did not pass on-arrival validation")
        if range_m is not None:
            range_m = float(range_m)
            if not math.isfinite(range_m) or range_m <= 0:
                range_m = range_source = range_note = None
        return TargetGeometry(bearing, range_m, range_source, range_note, class_name)

    def _range(
        self,
        candidate: Any,
        box: tuple[float, float, float, float],
        focal_px: float,
        class_name: str,
        bearing: float,
        ground: _GroundFix | None,
        target_size_m: float | None,
        telemetry: Telemetry,
    ) -> tuple[float | None, str | None, str | None]:
        """(range, source, note) from the first source that applies, most trusted first.

        A measured forward ToF range beats geometry, geometry beats any assumed size: a declared
        size is a height, which a prone or foreshortened box does not show.
        """
        x1, y1, x2, y2 = box

        def size_range(size_m: float | None, *, vertical: bool) -> float | None:
            if size_m is None:
                return None
            pixels = (y2 - y1) if vertical else (x2 - x1)
            return size_m * focal_px / pixels if pixels > 1.0 else None

        if abs(bearing) <= TOF_BEARING_GATE_RAD and self._forward_tof_applies(box, candidate, telemetry):
            range_m = self._forward_proximity_range(telemetry)
            if range_m is not None:
                return range_m, "forward_tof", None
        if ground is not None:
            return ground.slant_m, "ground_plane", (
                f"ground-plane ray from {ground.height_m:.2f} m above ground at "
                f"pitch {ground.pitch_deg:.1f} deg: {ground.horizontal_m:.2f} m horizontal"
            )
        # target_size_m is the operator's declared height for this specific target.
        range_m = size_range(target_size_m, vertical=True)
        if range_m is not None:
            return range_m, "target_size", None
        if self.settings.target_height_m is not None:
            label_is_person, applies = self._person_height_applies(candidate, class_name)
            range_m = size_range(self.settings.target_height_m, vertical=True) if applies else None
            if range_m is not None:
                note = None
                if not label_is_person and class_name:
                    note = (
                        f"configured target height {self.settings.target_height_m:g} m used for a "
                        f"target labeled {class_name!r} with person evidence"
                    )
                return range_m, "person_height", note
        range_m = size_range(self.settings.target_width_m, vertical=False)
        if range_m is not None:
            return range_m, "configured_target_width", None
        return None, None, None

    @staticmethod
    def _person_height_applies(candidate: Any, class_name: str) -> tuple[bool, bool]:
        """(label is a detector person, configured person height may range this target).

        The person height ranges a detector person, a classless candidate, or a track with person
        evidence (a person-only shirt_colour attribute or a person label in its history). An
        open-vocabulary seed is not a detector person: it needs target_size_m or person evidence.
        """
        class_id = candidate_value(candidate, "class_id")
        source = str(candidate_value(candidate, "source") or "")
        attributes = candidate_value(candidate, "attributes")
        label_is_person = (class_name == "person" or class_id == 0) and source != "open_vocab"
        person_evidence = bool(candidate_value(candidate, "person_evidence", False)) or (
            # shirt_colour is classified only for person-class detections (perception pipeline).
            isinstance(attributes, Mapping) and attributes.get("shirt_colour") is not None
        )
        applies = label_is_person or person_evidence or (not class_name and class_id is None)
        return label_is_person, applies

    def _not_visible_message(self, target_id: Any) -> str:
        """Distinguish a gone/stale candidate from a reference-appearance mismatch."""
        reason = self.perception.validate_candidate_failure(target_id)
        if reason == "appearance_mismatch":
            return (
                f"tracked target {target_id!r} failed appearance-embedding revalidation "
                "(reference appearance mismatch)"
            )
        if reason == "no_reference_appearance":
            return f"tracked target {target_id!r} has no stored reference appearance to validate"
        if reason in ("not_visible", "stale"):
            state = (
                "stale in the current frame" if reason == "stale" else "not visible in the current frame"
            )
            return f"tracked target {target_id!r} is {state}"
        if self.perception.get_candidate(target_id) is None:
            return f"tracked target {target_id!r} is not visible in the current frame"
        return f"tracked target {target_id!r} failed appearance-embedding revalidation"

    def height_above_ground(self, telemetry: Telemetry) -> float | None:
        """Fresh unsaturated downward range, else the pose altitude above home, else None.

        A downward reading at its sensor bound means "at least max" / "at most min", not a height.
        """
        ground = telemetry.ground_distance
        if ground is not None and self.fresh(ground.updated_monotonic):
            values = (ground.distance_m, ground.min_distance_m, ground.max_distance_m)
            if all(math.isfinite(value) for value in values):
                distance, minimum, maximum = (float(value) for value in values)
                margin = max(0.05, 0.02 * maximum)
                if minimum + margin < distance < maximum - margin:
                    return distance
        pose = telemetry.pose
        if pose is not None and math.isfinite(pose.up_m) and pose.up_m > 0.0:
            return float(pose.up_m)
        return None

    def camera_pitch_deg(self, telemetry: Telemetry) -> float:
        """Fresh earth-frame mount pitch, else the configured default.

        Mount pitch and the gimbal command are both earth-frame, so the vehicle's own pitch is
        never added.
        """
        mount = telemetry.mount_status
        if mount is not None and self.fresh(mount.updated_monotonic) and math.isfinite(mount.pitch_deg):
            return float(mount.pitch_deg)
        return float(self.settings.default_gimbal_pitch_deg)

    def _ray_depression_deg(self, box: Any, candidate: Any, telemetry: Telemetry) -> float | None:
        """Depression of the ray through the box centre in the vehicle frame, or None."""
        size = _frame_size(candidate)
        if size is None:
            return None
        image_width, image_height = size
        y1, y2 = box[1], box[3]
        theta = math.radians(-self.camera_pitch_deg(telemetry))
        vertical = ((y1 + y2) * 0.5 - image_height * 0.5) / self.focal_px(image_width)
        down = vertical * math.cos(theta) + math.sin(theta)
        north = math.cos(theta) - vertical * math.sin(theta)
        if abs(north) < 1e-9 and abs(down) < 1e-9:
            return None
        return math.degrees(math.atan2(down, abs(north)))

    def _forward_tof_applies(self, box: Any, candidate: Any, telemetry: Telemetry) -> bool:
        """Whether the level, zero-vertical-FOV forward lidar beam passes through the target.

        Without the frame geometry to place the ray, the bearing gate alone decides.
        """
        depression = self._ray_depression_deg(box, candidate, telemetry)
        if depression is None:
            return True
        return abs(depression) <= self.settings.forward_tof_vertical_half_fov_deg

    def _ground_plane(self, box: Any, candidate: Any, telemetry: Telemetry) -> _GroundFix | None:
        """Intersect the box's ground-contact ray with the ground plane at the measured height.

        The contact row is the bottom edge for an upright box and the centre row for a prone (wider
        than tall) one, so the range does not depend on posture. Refused when the ray is shallower
        than ``min_ground_range_depression_deg`` or the height or frame size is unknown.
        """
        size = _frame_size(candidate)
        if size is None:
            return None
        image_width, image_height = size
        height_above_ground = self.height_above_ground(telemetry)
        if height_above_ground is None:
            return None
        pitch_deg = self.camera_pitch_deg(telemetry)
        x1, y1, x2, y2 = box
        if x2 <= x1 or y2 <= y1:
            return None
        focal_px = self.focal_px(image_width)
        theta = math.radians(-pitch_deg)  # optical-axis depression, positive downwards
        prone = (y2 - y1) < (x2 - x1)

        def ray(row: float) -> tuple[float, float, float]:
            vertical = (row - image_height * 0.5) / focal_px
            horizontal_px = ((x1 + x2) * 0.5 - image_width * 0.5) / focal_px
            return (
                math.cos(theta) - vertical * math.sin(theta),
                horizontal_px,
                vertical * math.cos(theta) + math.sin(theta),
            )

        north, east, down = ray((y1 + y2) * 0.5 if prone else y2)
        if down <= 0.0:
            return None
        depression_deg = math.degrees(math.atan2(down, math.hypot(north, east)))
        if depression_deg < self.settings.min_ground_range_depression_deg:
            return None
        slant_to_ground = height_above_ground / down
        horizontal = math.hypot(north, east) * slant_to_ground
        torso_north, _torso_east, torso_down = ray((y1 + y2) * 0.5)
        if torso_north > 1e-9:
            torso_height = max(0.0, height_above_ground - horizontal * (torso_down / torso_north))
            slant = math.hypot(horizontal, height_above_ground - torso_height)
        else:
            slant = slant_to_ground
        if not math.isfinite(slant) or slant <= 0.0:
            return None
        return _GroundFix(slant, math.atan2(east, north), horizontal, height_above_ground, pitch_deg)

    def _forward_proximity_range(self, telemetry: Telemetry) -> float | None:
        """Fresh in-range forward (orientation 0) ToF distance, or None."""
        for sensor in telemetry.proximity:
            # The link keeps the last in-range reading; a stalled range bridge must not supply it.
            if sensor.orientation != 0 or not self.fresh(sensor.updated_monotonic):
                continue
            distance = float(sensor.distance_m)
            minimum = float(sensor.min_distance_m)
            maximum = float(sensor.max_distance_m)
            if (
                math.isfinite(distance)
                and math.isfinite(minimum)
                and math.isfinite(maximum)
                and minimum <= distance < maximum
            ):
                return distance + 0.24
        return None
