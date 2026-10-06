"""Bounded skill executor; every autonomous translation is a closed-loop NED velocity command."""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from horus.config import ExecutorSettings
from horus.executor.precision_landing import AprilTagPrecisionLandingController
from horus.flight import Pose


logger = logging.getLogger(__name__)

_Point = tuple[float, float]
_GEOMETRY_EPSILON = 1e-9


def _cross(left: _Point, right: _Point) -> float:
    return left[0] * right[1] - left[1] * right[0]


def _signed_area(polygon: tuple[_Point, ...]) -> float:
    return 0.5 * sum(
        _cross(polygon[index], polygon[(index + 1) % len(polygon)])
        for index in range(len(polygon))
    )


def _distance_to_segment(point: _Point, start: _Point, end: _Point) -> float:
    edge = (end[0] - start[0], end[1] - start[1])
    length_squared = edge[0] * edge[0] + edge[1] * edge[1]
    if length_squared == 0.0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    projection = (
        (point[0] - start[0]) * edge[0] + (point[1] - start[1]) * edge[1]
    ) / length_squared
    projection = max(0.0, min(1.0, projection))
    closest = (start[0] + projection * edge[0], start[1] + projection * edge[1])
    return math.hypot(point[0] - closest[0], point[1] - closest[1])


def _orientation(a: _Point, b: _Point, c: _Point) -> float:
    return _cross((b[0] - a[0], b[1] - a[1]), (c[0] - a[0], c[1] - a[1]))


def _segments_intersect(a: _Point, b: _Point, c: _Point, d: _Point) -> bool:
    ab_c, ab_d = _orientation(a, b, c), _orientation(a, b, d)
    cd_a, cd_b = _orientation(c, d, a), _orientation(c, d, b)
    epsilon = _GEOMETRY_EPSILON
    if (
        ((ab_c > epsilon and ab_d < -epsilon) or (ab_c < -epsilon and ab_d > epsilon))
        and ((cd_a > epsilon and cd_b < -epsilon) or (cd_a < -epsilon and cd_b > epsilon))
    ):
        return True

    def on_segment(start: _Point, end: _Point, point: _Point, cross: float) -> bool:
        return (
            abs(cross) <= epsilon
            and min(start[0], end[0]) - epsilon <= point[0] <= max(start[0], end[0]) + epsilon
            and min(start[1], end[1]) - epsilon <= point[1] <= max(start[1], end[1]) + epsilon
        )

    return (
        on_segment(a, b, c, ab_c)
        or on_segment(a, b, d, ab_d)
        or on_segment(c, d, a, cd_a)
        or on_segment(c, d, b, cd_b)
    )


def _validate_simple_polygon(polygon: tuple[_Point, ...]) -> float:
    if len(polygon) < 3:
        raise ValueError("geofence_polygon_ned needs at least three vertices")
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        if math.hypot(end[0] - start[0], end[1] - start[1]) <= _GEOMETRY_EPSILON:
            raise ValueError("geofence_polygon_ned cannot contain duplicate adjacent vertices")
    area = _signed_area(polygon)
    if abs(area) <= _GEOMETRY_EPSILON:
        raise ValueError("geofence_polygon_ned must enclose a non-zero area")
    count = len(polygon)
    for first in range(count):
        a, b = polygon[first], polygon[(first + 1) % count]
        for second in range(first + 1, count):
            if second == first + 1 or (first == 0 and second == count - 1):
                continue
            c, d = polygon[second], polygon[(second + 1) % count]
            if _segments_intersect(a, b, c, d):
                raise ValueError("geofence_polygon_ned must be a simple polygon")
    return area


def _point_in_polygon(point: _Point, polygon: tuple[_Point, ...]) -> bool:
    inside = False
    x, y = point
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        if _distance_to_segment(point, start, end) <= _GEOMETRY_EPSILON:
            return True
        if (start[1] > y) != (end[1] > y):
            crossing_x = start[0] + (y - start[1]) * (end[0] - start[0]) / (end[1] - start[1])
            if x < crossing_x:
                inside = not inside
    return inside


def _inset_polygon(polygon: tuple[_Point, ...], margin_m: float) -> tuple[_Point, ...]:
    area = _validate_simple_polygon(polygon)
    if margin_m == 0.0:
        return polygon
    orientation = 1.0 if area > 0.0 else -1.0
    edges: list[tuple[_Point, _Point, _Point]] = []
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        direction = (end[0] - start[0], end[1] - start[1])
        length = math.hypot(*direction)
        inward = (
            -orientation * direction[1] / length,
            orientation * direction[0] / length,
        )
        edges.append((direction, inward, start))

    inset: list[_Point] = []
    for index, point in enumerate(polygon):
        previous_direction, previous_normal, _ = edges[index - 1]
        current_direction, current_normal, _ = edges[index]
        previous_origin = (
            point[0] + previous_normal[0] * margin_m,
            point[1] + previous_normal[1] * margin_m,
        )
        current_origin = (
            point[0] + current_normal[0] * margin_m,
            point[1] + current_normal[1] * margin_m,
        )
        denominator = _cross(previous_direction, current_direction)
        if abs(denominator) <= _GEOMETRY_EPSILON:
            if (
                previous_direction[0] * current_direction[0]
                + previous_direction[1] * current_direction[1]
            ) <= 0.0:
                raise ValueError("geofence_polygon_ned has a degenerate vertex")
            inset.append(previous_origin)
            continue
        offset = (
            current_origin[0] - previous_origin[0],
            current_origin[1] - previous_origin[1],
        )
        distance_along_previous = _cross(offset, current_direction) / denominator
        inset.append(
            (
                previous_origin[0] + distance_along_previous * previous_direction[0],
                previous_origin[1] + distance_along_previous * previous_direction[1],
            )
        )

    result = tuple(inset)
    inset_area = _validate_simple_polygon(result)
    if area * inset_area <= 0.0:
        raise ValueError("geofence_margin_m collapses geofence_polygon_ned")
    for index, start in enumerate(result):
        end = result[(index + 1) % len(result)]
        for point in (
            start,
            ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0),
        ):
            if not _point_in_polygon(point, polygon):
                raise ValueError("geofence_margin_m collapses geofence_polygon_ned")
            clearance = min(
                _distance_to_segment(point, polygon[edge], polygon[(edge + 1) % len(polygon)])
                for edge in range(len(polygon))
            )
            if clearance + _GEOMETRY_EPSILON < margin_m:
                raise ValueError("geofence_margin_m collapses geofence_polygon_ned")
    return result


def flight_envelope(settings: ExecutorSettings) -> dict[str, Any]:
    """Operator-facing limits: inset geofence (polygon NED or radius), altitude, speed, leg length."""
    envelope: dict[str, Any] = {
        "altitude_m": [settings.min_altitude_m, settings.max_altitude_m],
        "max_speed_m_s": settings.max_speed_m_s,
        "max_vertical_speed_m_s": settings.max_vertical_speed_m_s,
        "max_distance_m": settings.max_distance_m,
    }
    if settings.geofence_polygon_ned is None:
        envelope["geofence_radius_m"] = settings.geofence_radius_m
    else:
        inset = _inset_polygon(settings.geofence_polygon_ned, settings.geofence_margin_m)
        envelope["geofence_polygon_ned"] = [[round(north, 3), round(east, 3)] for north, east in inset]
    return envelope


def _nearest_polygon_point(point: _Point, polygon: tuple[_Point, ...]) -> _Point:
    if _point_in_polygon(point, polygon):
        return point
    nearest: _Point | None = None
    nearest_distance_squared = math.inf
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        edge = (end[0] - start[0], end[1] - start[1])
        length_squared = edge[0] * edge[0] + edge[1] * edge[1]
        projection = (
            0.0 if length_squared == 0.0 else
            max(0.0, min(1.0, ((point[0] - start[0]) * edge[0] + (point[1] - start[1]) * edge[1]) / length_squared))
        )
        candidate = (start[0] + projection * edge[0], start[1] + projection * edge[1])
        distance_squared = (
            (point[0] - candidate[0]) ** 2 + (point[1] - candidate[1]) ** 2
        )
        if distance_squared < nearest_distance_squared:
            nearest, nearest_distance_squared = candidate, distance_squared
    if nearest is None:
        raise ValueError("geofence_polygon_ned contains no projectable edge")
    return nearest


class ExecutorState(str, Enum):
    IDLE = "IDLE"
    EXECUTING = "EXECUTING"
    HOLDING = "HOLDING"
    RTH = "RTH"


class _TargetNotVisible(RuntimeError):
    """A tracked ID is temporarily absent from the current camera frame."""



@dataclass(slots=True)
class SkillTask:
    task_id: str
    skill: str
    arguments: dict[str, Any]
    requested_target: Any
    executed_target: Any
    clamp_reason: str | None
    created_at: str
    created_monotonic: float
    expires_monotonic: float
    status: str = "queued"
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_requested: bool = False
    _phase: str = "start"
    _waypoints: list[Pose] = field(default_factory=list)
    _waypoint_index: int = 0
    skipped_waypoints: list[dict[str, Any]] = field(default_factory=list)
    _scan_progress_reference_distance_m: float | None = None
    _scan_progress_since_monotonic: float | None = None
    _scan_turn_since_monotonic: float | None = None
    _target_pose: Pose | None = None
    _arm_requested_monotonic: float = 0.0
    tracking: dict[str, Any] = field(default_factory=dict)
    _target_lost_at: float | None = None
    _target_lost_at_sim_s: float | None = None


class HorusExecutor:
    """Serialize skills and continuously command bounded velocity from current telemetry."""

    SKILLS = {"take_off", "goto", "approach", "follow", "orbit", "scan"}
    ARM_RETRY_INTERVAL_S = 2.0
    HEADING_TOLERANCE_DEG = 5.0
    PRECISION_HOME_RADIUS_M = 3.0
    # Below this altitude the tag overflows the camera view; hand the centred vehicle to vertical LAND.
    PRECISION_HANDOFF_ALTITUDE_M = 0.6
    PRECISION_FRAME_MAX_AGE_S = 1.0
    PRECISION_GIMBAL_SETTLE_S = 0.5


    def __init__(
        self,
        flight: Any,
        settings: ExecutorSettings,
        perception: Any | None = None,
        *,
        autostart: bool = True,
    ):
        numeric_limits = (
            settings.control_hz,
            settings.command_ttl_s,
            settings.max_speed_m_s,
            settings.max_vertical_speed_m_s,
            settings.max_distance_m,
            settings.min_altitude_m,
            settings.max_altitude_m,
            settings.geofence_radius_m,
            settings.geofence_margin_m,
            settings.arrival_tolerance_m,
            settings.scan_progress_threshold_m,
            settings.scan_progress_timeout_s,
            settings.camera_horizontal_fov_deg,
            settings.target_match_threshold,
            settings.link_loss_rth_delay_s,
        )
        if not all(math.isfinite(value) for value in numeric_limits):
            raise ValueError("executor limits must be finite")
        if settings.geofence_polygon_ned is None:
            self._geofence_polygon = None
        else:
            self._geofence_polygon = _inset_polygon(
                settings.geofence_polygon_ned, settings.geofence_margin_m
            )
        if not 10.0 <= settings.control_hz <= 20.0:
            raise ValueError("control_hz must be between 10 and 20 Hz")
        if settings.command_ttl_s <= 0 or settings.max_speed_m_s <= 0:
            raise ValueError("command_ttl_s and max_speed_m_s must be positive")
        if settings.max_vertical_speed_m_s <= 0 or settings.arrival_tolerance_m <= 0:
            raise ValueError("vertical speed and arrival tolerance must be positive")
        if settings.scan_progress_threshold_m <= 0 or settings.scan_progress_timeout_s <= 0:
            raise ValueError("scan progress threshold and timeout must be positive")
        if settings.geofence_radius_m < 0.5 or settings.max_distance_m < 0.5:
            raise ValueError("geofence_radius_m and max_distance_m must be at least 0.5 m")
        if settings.min_altitude_m < 0 or settings.max_altitude_m < settings.min_altitude_m:
            raise ValueError("altitude limits must satisfy 0 <= min_altitude_m <= max_altitude_m")
        if settings.target_width_m is not None and (
            not math.isfinite(settings.target_width_m) or settings.target_width_m <= 0
        ):
            raise ValueError("target_width_m must be positive when configured")
        if not 1.0 <= settings.camera_horizontal_fov_deg <= 179.0:
            raise ValueError("camera_horizontal_fov_deg must be between 1 and 179 degrees")
        if settings.link_loss_rth_delay_s <= 0:
            raise ValueError("link_loss_rth_delay_s must be positive")
        if not 0.0 <= settings.target_match_threshold <= 1.0:
            raise ValueError("target_match_threshold must be in [0, 1]")
        self.flight = flight
        self.settings = settings
        self.perception = perception
        self._lock = threading.RLock()
        self._state = ExecutorState.IDLE
        self._tasks: dict[str, SkillTask] = {}
        self._queue: deque[str] = deque()
        self._active_id: str | None = None
        self._stop = threading.Event()
        self._holding_since: float | None = None
        self._auto_rth_after_hold = False
        self._precision_controller = AprilTagPrecisionLandingController()
        self._precision_action: str | None = None
        self._precision_active = False
        self._precision_gimbal_requested_at: float | None = None
        self._precision_unavailable = False
        self._precision_stats: dict[str, Any] = {
            "tag_detections": 0, "acquisitions": 0, "completed_landings": 0, "last_fallback": None,
        }
        self._worker: threading.Thread | None = None
        if autostart:
            self.start()

    @property
    def state(self) -> ExecutorState:
        with self._lock:
            return self._state

    def start(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(target=self._run, name="horus-executor", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        period = 1.0 / self.settings.control_hz
        while not self._stop.is_set():
            started = time.monotonic()
            self.tick()
            self._stop.wait(max(0.0, period - (time.monotonic() - started)))

    def close(self) -> None:
        self._stop.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=1.5)
        if self.state in (ExecutorState.EXECUTING, ExecutorState.HOLDING) or self._precision_active:
            self._safe_hold()
            self._precision_active = False

    def _telemetry(self) -> Any:
        source = getattr(self.flight, "telemetry", None)
        return source() if callable(source) else source

    def get_state(self) -> dict[str, Any]:
        telemetry = self._telemetry()
        with self._lock:
            active = None if self._active_id is None else self._tasks[self._active_id]
            snapshot = {
                "state": self._state.value,
                "active_task_id": None if active is None else active.task_id,
                "active_skill": None if active is None else active.skill,
                "observed_at": getattr(telemetry, "observed_at", datetime.now(timezone.utc).isoformat()),
                "precision_landing": {"active": self._precision_active, **self._precision_stats},
            }
        snapshot["vehicle"] = telemetry.as_dict() if hasattr(telemetry, "as_dict") else telemetry
        return snapshot

    def submit_skill(
        self, skill: str, arguments: Mapping[str, Any] | None = None, ttl_s: float | None = None
    ) -> dict[str, Any]:
        if skill == "hold":
            return self.hold()
        if skill == "rth":
            return self.rth()
        if skill == "land":
            return self.land()
        if skill not in self.SKILLS:
            raise ValueError(f"unknown skill {skill!r}")
        ttl = self.settings.command_ttl_s if ttl_s is None else float(ttl_s)
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("ttl_s must be a finite positive number")
        args = dict(arguments or {})
        requested, executed, reason = self._prepare_target(skill, args)
        now = time.monotonic()
        task_id = uuid.uuid4().hex[:12]
        task = SkillTask(
            task_id=task_id,
            skill=skill,
            arguments=args,
            requested_target=requested,
            executed_target=executed,
            clamp_reason=reason,
            created_at=datetime.now(timezone.utc).isoformat(),
            created_monotonic=now,
            expires_monotonic=now + ttl,
        )
        if skill == "follow":
            task.tracking = {
                "reference_id": args["id"],
                "reacquired": False,
                "matched_id": None,
                "time_to_reacquire_s": None,
                "embedding_validated": False,
                "embedding_similarity": None,
                "id_preserved": None,
                "id_rematched": None,
            }
        with self._lock:
            self._tasks[task_id] = task
            self._holding_since = None
            self._queue.append(task_id)
        return self._task_dict(task)

    def _prepare_target(self, skill: str, args: dict[str, Any]) -> tuple[Any, Any, str | None]:
        if skill == "goto":
            if "pose" not in args:
                raise ValueError("goto requires pose")
            requested = self._parse_pose(args["pose"])
            executed, reasons = self._clamp_pose(requested)
            requested_target, executed_target = requested.as_dict(), executed.as_dict()
            heading = args.get("heading_deg")
            if heading is not None:
                heading = self._finite_argument(heading, "heading_deg")
                requested_target["heading_deg"] = heading
                executed_target["heading_deg"] = heading % 360.0
            return requested_target, executed_target, "; ".join(reasons) or None
        if skill == "take_off":
            altitude = float(args.get("altitude_m", args.get("altitude", 2.0)))
            if not math.isfinite(altitude):
                raise ValueError("altitude_m must be finite")
            bounded = min(self.settings.max_altitude_m, max(self.settings.min_altitude_m, altitude))
            requested: dict[str, Any] = {"altitude_m": altitude}
            executed: dict[str, Any] = {"altitude_m": bounded}
            heading = args.get("heading_deg")
            if heading is not None:
                heading = self._finite_argument(heading, "heading_deg")
                requested["heading_deg"] = heading
                executed["heading_deg"] = heading % 360.0
            return requested, executed, (
                "altitude clamped to configured flight envelope" if bounded != altitude else None
            )
        if skill == "approach":
            self._require_target_id(args)
            distance = self._finite_argument(args.get("dist_m", args.get("dist", 2.0)), "dist_m")
            bounded = min(self.settings.max_distance_m, max(0.5, distance))
            reason = "approach distance clamped to [0.5 m, max_distance_m]" if bounded != distance else None
            return {"id": args["id"], "dist_m": distance}, {"id": args["id"], "dist_m": bounded}, reason
        if skill == "follow":
            self._require_target_id(args)
            distance = self._finite_argument(args.get("dist_m", self.settings.follow_distance_m), "dist_m")
            bounded = min(self.settings.max_distance_m, max(0.5, distance))
            reason = "follow distance clamped to [0.5 m, max_distance_m]" if bounded != distance else None
            return {"id": args["id"], "dist_m": distance}, {"id": args["id"], "dist_m": bounded}, reason
        if skill == "orbit":
            self._require_target_id(args)
            radius = self._finite_argument(args.get("radius_m", args.get("dist_m", 2.0)), "radius_m")
            bounded = min(self.settings.max_distance_m, max(0.75, radius))
            reason = "orbit radius clamped to [0.75 m, max_distance_m]" if bounded != radius else None
            return {"id": args["id"], "radius_m": radius}, {"id": args["id"], "radius_m": bounded}, reason
        if skill == "scan":
            if not isinstance(args.get("area"), Mapping):
                raise ValueError("scan requires a rectangular area mapping")
            requested, waypoints, reasons = self._scan_waypoints(args["area"])
            return requested, [point.as_dict() for point in waypoints], "; ".join(reasons) or None
        raise ValueError(f"cannot prepare {skill}")

    @staticmethod
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

    @staticmethod
    def _finite_argument(value: Any, name: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} must be a finite number") from exc
        if not math.isfinite(number):
            raise ValueError(f"{name} must be a finite number")
        return number

    @staticmethod
    def _parse_pose(value: Any) -> Pose:
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

    def _current_pose(self) -> Pose | None:
        value = getattr(self._telemetry(), "pose", None)
        if isinstance(value, Pose):
            return value
        if isinstance(value, Mapping):
            return self._parse_pose(value)
        return None

    def _clamp_pose(self, requested: Pose, current: Pose | None = None) -> tuple[Pose, list[str]]:
        reasons: list[str] = []
        current = current or self._current_pose() or Pose(0.0, 0.0, 0.0)
        north, east, up = requested.north_m, requested.east_m, requested.up_m
        altitude = min(self.settings.max_altitude_m, max(self.settings.min_altitude_m, up))
        if altitude != up:
            reasons.append("altitude clamped to configured flight envelope")
        distance_from_current = math.hypot(north - current.north_m, east - current.east_m)
        if distance_from_current > self.settings.max_distance_m:
            scale = self.settings.max_distance_m / distance_from_current
            north = current.north_m + (north - current.north_m) * scale
            east = current.east_m + (east - current.east_m) * scale
            reasons.append("travel distance clamped to max_distance_m")
        if self._geofence_polygon is not None:
            before_geofence = (north, east)
            north, east = _nearest_polygon_point(before_geofence, self._geofence_polygon)
            if (north, east) != before_geofence:
                reasons.append(
                    f"horizontal target clamped to polygon geofence "
                    f"(inset margin {self.settings.geofence_margin_m:g} m)"
                )
        else:
            radius = math.hypot(north, east)
            if radius > self.settings.geofence_radius_m:
                scale = self.settings.geofence_radius_m / radius
                north *= scale
                east *= scale
                reasons.append("horizontal target clamped to geofence radius")
        return Pose(north, east, altitude), reasons

    def _scan_waypoints(self, area: Mapping[str, Any]) -> tuple[dict[str, Any], list[Pose], list[str]]:
        required = ("north_min_m", "north_max_m", "east_min_m", "east_max_m")
        if any(key not in area for key in required):
            raise ValueError("scan area needs north_min_m, north_max_m, east_min_m, east_max_m")
        n0, n1, e0, e1 = (float(area[key]) for key in required)
        altitude = float(area.get("altitude_m", 2.0))
        spacing = float(area.get("spacing_m", 2.0))
        if not all(math.isfinite(v) for v in (n0, n1, e0, e1, altitude, spacing)) or spacing <= 0:
            raise ValueError("scan area coordinates must be finite and spacing_m positive")
        n0, n1 = sorted((n0, n1))
        e0, e1 = sorted((e0, e1))
        requested = {
            "north_min_m": n0,
            "north_max_m": n1,
            "east_min_m": e0,
            "east_max_m": e1,
            "altitude_m": altitude,
            "spacing_m": spacing,
        }
        reasons: list[str] = []
        extent = (
            self.settings.max_distance_m
            if self._geofence_polygon is not None
            else min(self.settings.max_distance_m, self.settings.geofence_radius_m)
        )
        bounded = tuple(max(-extent, min(extent, value)) for value in (n0, n1, e0, e1))
        if bounded != (n0, n1, e0, e1):
            reasons.append("scan bounds clamped to max-distance/geofence envelope")
        n0, n1 = sorted((bounded[0], bounded[1]))
        e0, e1 = sorted((bounded[2], bounded[3]))
        safe_spacing = min(max(0.5, spacing), self.settings.max_distance_m)
        if safe_spacing != spacing:
            reasons.append("scan spacing clamped to safe range")
        spacing = safe_spacing
        rows = max(1, math.ceil((n1 - n0) / spacing))
        waypoints: list[Pose] = []
        for row in range(rows + 1):
            north = min(n1, n0 + row * spacing)
            east_pair = (e0, e1) if row % 2 == 0 else (e1, e0)
            waypoints.extend((Pose(north, east_pair[0], altitude), Pose(north, east_pair[1], altitude)))
        safe: list[Pose] = []
        for point in waypoints:
            clamped, why = self._clamp_pose(point, current=Pose(0.0, 0.0, 0.0))
            safe.append(clamped)
            reasons.extend(why)
        if len(set(safe)) < 2:
            raise ValueError("scan area collapses inside configured geofence/distance limits")
        return requested, safe, list(dict.fromkeys(reasons))

    def tick(self) -> None:
        """Perform one controller step; public for deterministic tests and embedded loops."""
        with self._lock:
            if self._active_id is None:
                while self._queue:
                    task_id = self._queue.popleft()
                    task = self._tasks[task_id]
                    if task.status != "queued":
                        continue
                    if time.monotonic() >= task.expires_monotonic:
                        self._finish(task, "expired", "command TTL expired before execution")
                        continue
                    self._active_id = task_id
                    task.status = "running"
                    self._state = ExecutorState.EXECUTING
                    self._holding_since = None
                    self._precision_action = None
                    self._precision_active = False
                    self._precision_gimbal_requested_at = None
                    self._precision_unavailable = False
                    break
            task = None if self._active_id is None else self._tasks[self._active_id]
            rth_due = (
                task is None
                and self._state == ExecutorState.HOLDING
                and self._auto_rth_after_hold
                and self._holding_since is not None
                and time.monotonic() - self._holding_since >= self.settings.link_loss_rth_delay_s
            )
        if task is None:
            if self._precision_action is not None:
                self._precision_landing_step(self._precision_action)
            elif rth_due:
                self.rth()
            elif self.state == ExecutorState.HOLDING:
                self._command_zero_velocity()
            return
        if task.cancel_requested:
            self._safe_hold()
            with self._lock:
                self._finish(task, "cancelled", "task cancelled")
            return
        if time.monotonic() >= task.expires_monotonic:
            self._safe_hold()
            with self._lock:
                self._finish(task, "expired", "command TTL expired; executor entered GUIDED hold")
            return
        try:
            completed = self._step(task)
        except Exception as exc:
            self._safe_hold()
            with self._lock:
                self._finish(task, "failed", str(exc))
            return
        if completed:
            self._safe_hold()
            with self._lock:
                self._finish(task, "succeeded", None)

    def _step(self, task: SkillTask) -> bool:
        if task._phase == "start":
            self.flight.set_guided()
            if task.skill == "take_off":
                # Missions search forward; undo any downward gimbal left by a precision landing.
                self._level_gimbal()
                altitude = float(task.executed_target["altitude_m"])
                pose = self._current_pose()
                task._target_pose = Pose(
                    0.0 if pose is None else pose.north_m,
                    0.0 if pose is None else pose.east_m,
                    altitude,
                )
                task._phase = "guiding"
                return False
            elif task.skill == "goto":
                task._target_pose = self._parse_pose(task.executed_target)
            elif task.skill == "scan":
                task._waypoints = [self._parse_pose(point) for point in task.executed_target]
            task._phase = "control"
        if task.skill == "take_off" and task._phase == "guiding":
            if str(getattr(self._telemetry(), "mode", "")).upper() != "GUIDED":
                return False
            self.flight.arm()
            task._arm_requested_monotonic = time.monotonic()
            task._phase = "arming"
            return False
        if task.skill == "take_off" and task._phase == "arming":
            if not bool(getattr(self._telemetry(), "armed", False)):
                # ArduPilot rejects arming while pre-arm checks settle (e.g. "Accels inconsistent"
                # after a cold start); keep asking until armed or the task TTL expires.
                now = time.monotonic()
                if now - task._arm_requested_monotonic >= self.ARM_RETRY_INTERVAL_S:
                    self.flight.arm()
                    task._arm_requested_monotonic = now
                return False
            self.flight.takeoff(float(task.executed_target["altitude_m"]))
            task._phase = "takeoff"
            return False
        if task.skill == "take_off" and task._phase == "takeoff":
            current = self._current_pose()
            if current is None:
                raise RuntimeError("local vehicle pose unavailable; refusing open-loop takeoff")
            if current.up_m < task._target_pose.up_m - self.settings.arrival_tolerance_m:
                return False
            task._phase = "control"
        if task.skill in ("take_off", "goto"):
            arrived = self._goto_step(task._target_pose)
            heading = task.executed_target.get("heading_deg")
            if arrived and heading is not None:
                current = float(getattr(self._telemetry(), "heading_deg", heading) or 0.0)
                error = (heading - current + 180.0) % 360.0 - 180.0
                if abs(error) > self.HEADING_TOLERANCE_DEG:
                    # Hover in place while turning to the requested heading.
                    self.flight.set_velocity_ned(0.0, 0.0, 0.0, yaw_deg=heading)
                    return False
            return arrived
        if task.skill == "scan":
            while task._waypoint_index < len(task._waypoints):
                waypoint_index = task._waypoint_index
                target, reasons = self._clamp_pose(task._waypoints[waypoint_index])
                if reasons:
                    task.clamp_reason = "; ".join(filter(None, [task.clamp_reason, *reasons]))
                    task._waypoints[waypoint_index] = target

                telemetry = self._telemetry()
                current = self._current_pose()
                if getattr(telemetry, "armed", None) is False:
                    position = "unavailable" if current is None else (
                        f"north={current.north_m:.3f}, east={current.east_m:.3f}, "
                        f"up={current.up_m:.3f}"
                    )
                    raise RuntimeError(
                        f"scan vehicle is disarmed at waypoint {waypoint_index + 1}/"
                        f"{len(task._waypoints)} (index {waypoint_index}), vehicle position "
                        f"{position}; "
                        "possible crash or vehicle never took off"
                    )
                if current is None:
                    raise RuntimeError("local vehicle pose unavailable; refusing open-loop movement")
                pose_updated = getattr(telemetry, "pose_updated_monotonic", None)
                age = math.inf if pose_updated is None else max(
                    0.0, time.monotonic() - float(pose_updated)
                )
                if age > 1.5:
                    raise RuntimeError(f"vehicle pose is stale ({age:.2f}s); entering hold")

                horizontal = math.hypot(
                    target.north_m - current.north_m,
                    target.east_m - current.east_m,
                )
                vertical = abs(target.up_m - current.up_m)
                if (
                    horizontal <= self.settings.arrival_tolerance_m
                    and vertical <= self.settings.arrival_tolerance_m
                ):
                    self._command_zero_velocity()
                    task._waypoint_index += 1
                    task._scan_progress_reference_distance_m = None
                    task._scan_progress_since_monotonic = None
                    task._scan_turn_since_monotonic = None
                    continue

                north = target.north_m - current.north_m
                east = target.east_m - current.east_m
                bearing = (
                    math.degrees(math.atan2(east, north)) % 360.0
                    if horizontal > 1e-6
                    else None
                )
                now = time.monotonic()
                if horizontal > self.settings.arrival_tolerance_m and bearing is not None:
                    heading_value = getattr(telemetry, "heading_deg", None)
                    try:
                        heading = float(heading_value)
                    except (TypeError, ValueError, OverflowError):
                        heading = math.nan
                    if math.isfinite(heading):
                        heading_error = (bearing - heading + 180.0) % 360.0 - 180.0
                        if abs(heading_error) > self.HEADING_TOLERANCE_DEG:
                            if task._scan_turn_since_monotonic is None:
                                task._scan_turn_since_monotonic = now
                                task._scan_progress_reference_distance_m = None
                                task._scan_progress_since_monotonic = None
                            elif (
                                now - task._scan_turn_since_monotonic
                                >= self.settings.scan_progress_timeout_s
                            ):
                                turn_s = now - task._scan_turn_since_monotonic
                                reason = (
                                    f"could not turn toward waypoint within {turn_s:.1f} s"
                                )
                                self._skip_scan_waypoint(
                                    task, waypoint_index, target, current,
                                    max(horizontal, vertical), turn_s, reason,
                                )
                                continue
                            self.flight.set_velocity_ned(0.0, 0.0, 0.0, yaw_deg=bearing)
                            return False

                task._scan_turn_since_monotonic = None
                # This max metric reaches the same tolerance boundary as _goto_step's two checks.
                distance = max(horizontal, vertical)
                if task._scan_progress_reference_distance_m is None:
                    task._scan_progress_reference_distance_m = distance
                    task._scan_progress_since_monotonic = now
                elif (
                    task._scan_progress_reference_distance_m - distance
                    >= self.settings.scan_progress_threshold_m
                ):
                    task._scan_progress_reference_distance_m = distance
                    task._scan_progress_since_monotonic = now
                elif (
                    task._scan_progress_since_monotonic is not None
                    and now - task._scan_progress_since_monotonic
                    >= self.settings.scan_progress_timeout_s
                ):
                    stuck_s = now - task._scan_progress_since_monotonic
                    reason = (
                        f"no progress toward waypoint for {stuck_s:.1f} s; "
                        "likely blocked by obstacle avoidance"
                    )
                    self._skip_scan_waypoint(
                        task, waypoint_index, target, current, distance, stuck_s, reason
                    )
                    continue
                self._goto_step(target, yaw_deg=bearing)
                return False
            return True
        return self._target_step(task)

    def _skip_scan_waypoint(
        self,
        task: SkillTask,
        waypoint_index: int,
        target: Pose,
        current: Pose,
        distance: float,
        elapsed_s: float,
        reason: str,
    ) -> None:
        task.skipped_waypoints.append(
            {
                "index": waypoint_index,
                "waypoint": target.as_dict(),
                "vehicle_pose": current.as_dict(),
                "distance_m": round(distance, 3),
                "stuck_s": round(elapsed_s, 1),
                "reason": reason,
            }
        )
        logger.warning(
            "scan task %s skipped waypoint %d target=(north=%.3f, east=%.3f, up=%.3f) "
            "vehicle=(north=%.3f, east=%.3f, up=%.3f), remaining_distance=%.3f m, reason=%s",
            task.task_id,
            waypoint_index,
            target.north_m,
            target.east_m,
            target.up_m,
            current.north_m,
            current.east_m,
            current.up_m,
            distance,
            reason,
        )
        self._command_zero_velocity()
        task._waypoint_index += 1
        task._scan_progress_reference_distance_m = None
        task._scan_progress_since_monotonic = None
        task._scan_turn_since_monotonic = None
        if len(task.skipped_waypoints) == len(task._waypoints):
            all_no_progress = all(
                "no progress" in item["reason"] for item in task.skipped_waypoints
            )
            all_turn_failures = all(
                "could not turn" in item["reason"] for item in task.skipped_waypoints
            )
            if all_no_progress:
                raise RuntimeError(
                    f"scan skipped all {len(task._waypoints)} waypoints due to no progress "
                    "toward any waypoint; likely blocked by obstacle avoidance"
                )
            if all_turn_failures:
                raise RuntimeError(
                    f"scan skipped all {len(task._waypoints)} waypoints because it could not "
                    "turn toward any waypoint"
                )
            raise RuntimeError(
                f"scan skipped all {len(task._waypoints)} waypoints because it could not "
                "turn toward or make progress to any waypoint"
            )

    def _goto_step(self, target: Pose, *, yaw_deg: float | None = None) -> bool:
        current = self._current_pose()
        if current is None:
            raise RuntimeError("local vehicle pose unavailable; refusing open-loop movement")
        telemetry = self._telemetry()
        pose_updated = getattr(telemetry, "pose_updated_monotonic", None)
        age = math.inf if pose_updated is None else max(0.0, time.monotonic() - float(pose_updated))
        if age > 1.5:
            raise RuntimeError(f"vehicle pose is stale ({age:.2f}s); entering hold")
        north, east, up = target.north_m - current.north_m, target.east_m - current.east_m, target.up_m - current.up_m
        horizontal = math.hypot(north, east)
        vertical = abs(up)
        if horizontal <= self.settings.arrival_tolerance_m and vertical <= self.settings.arrival_tolerance_m:
            self._command_zero_velocity()
            return True
        scale = min(1.0, self.settings.max_speed_m_s / horizontal) if horizontal > 1e-6 else 0.0
        vn = north * scale
        ve = east * scale
        vu = max(-self.settings.max_vertical_speed_m_s, min(self.settings.max_vertical_speed_m_s, up))
        # Velocity-only SET_POSITION_TARGET_LOCAL_NED is the only translation command.
        if yaw_deg is None:
            self.flight.set_velocity_ned(vn, ve, -vu)
        else:
            self.flight.set_velocity_ned(vn, ve, -vu, yaw_deg=yaw_deg)
        return False

    def _candidate(self, target_id: Any) -> Any | None:
        if self.perception is None:
            return None
        getter = getattr(self.perception, "get_candidate", None)
        if callable(getter):
            return getter(target_id)
        getter = getattr(self.perception, "candidate", None)
        return getter(target_id) if callable(getter) else None

    @staticmethod
    def _value(candidate: Any, name: str, default: Any = None) -> Any:
        if isinstance(candidate, Mapping):
            return candidate.get(name, default)
        return getattr(candidate, name, default)

    def _target_geometry(self, target_id: Any) -> tuple[float, float | None, float]:
        if self.perception is None:
            raise RuntimeError("perception identity revalidation is required before target action")
        candidate_validator = getattr(self.perception, "validate_candidate", None)
        if callable(candidate_validator):
            candidate = candidate_validator(target_id)
            if candidate is None:
                raise _TargetNotVisible(
                    f"tracked target {target_id!r} failed appearance-embedding revalidation"
                )
        else:
            candidate = self._candidate(target_id)
            if candidate is None:
                raise _TargetNotVisible(f"tracked target {target_id!r} is not currently available")
            validator = getattr(self.perception, "validate_id", None)
            if not callable(validator) or not validator(target_id):
                raise _TargetNotVisible(
                    f"tracked target {target_id!r} failed appearance-embedding revalidation"
                )
        profile_score = self._value(candidate, "target_match_confidence")
        if profile_score is not None:
            profile_score = float(profile_score)
            if not math.isfinite(profile_score) or profile_score < self.settings.target_match_threshold:
                raise RuntimeError("target did not meet the configured profile-match confidence")
        bearing = self._value(candidate, "bearing_rad", self._value(candidate, "bearing"))
        if bearing is None:
            bearing_deg = self._value(candidate, "bearing_deg")
            if bearing_deg is not None:
                bearing = math.radians(float(bearing_deg))
            else:
                box = self._value(candidate, "box", self._value(candidate, "bbox"))
                if box is None:
                    raise RuntimeError("candidate has no box or bearing")
                if isinstance(box, Mapping):
                    x1 = float(box.get("x1", box.get("left")))
                    x2 = float(box.get("x2", box.get("right")))
                else:
                    x1, x2 = float(box[0]), float(box[2])
                image_width = float(self._value(candidate, "frame_width", 640.0))
                center = (x1 + x2) * 0.5
                hfov = math.radians(float(self._value(
                    candidate, "horizontal_fov_deg", self.settings.camera_horizontal_fov_deg
                )))
                focal_px = image_width / (2.0 * math.tan(hfov / 2.0))
                bearing = math.atan2(center - image_width * 0.5, focal_px)
        else:
            bearing = float(bearing)
        range_m = self._value(candidate, "distance_m", self._value(candidate, "range_m"))
        if range_m is None and self.settings.target_width_m:
            box = self._value(candidate, "box", self._value(candidate, "bbox"))
            if box is not None:
                if isinstance(box, Mapping):
                    width = float(box.get("x2", box.get("right"))) - float(box.get("x1", box.get("left")))
                else:
                    width = float(box[2]) - float(box[0])
                image_width = float(self._value(candidate, "frame_width", 640.0))
                hfov = math.radians(self.settings.camera_horizontal_fov_deg)
                focal_px = image_width / (2.0 * math.tan(hfov / 2.0))
                if width > 1.0:
                    range_m = self.settings.target_width_m * focal_px / width
        if range_m is None and self.settings.target_height_m:
            box = self._value(candidate, "box", self._value(candidate, "bbox"))
            if box is not None:
                if isinstance(box, Mapping):
                    height = float(box.get("y2", box.get("bottom"))) - float(box.get("y1", box.get("top")))
                else:
                    height = float(box[3]) - float(box[1])
                image_width = float(self._value(candidate, "frame_width", 640.0))
                hfov = math.radians(self.settings.camera_horizontal_fov_deg)
                focal_px = image_width / (2.0 * math.tan(hfov / 2.0))  # square pixels
                if height > 1.0:
                    range_m = self.settings.target_height_m * focal_px / height
        confidence = float(self._value(candidate, "confidence", 1.0))
        if not math.isfinite(bearing) or not math.isfinite(confidence) or confidence < 0.2:
            raise RuntimeError("target identity/confidence did not pass on-arrival validation")
        if range_m is not None:
            range_m = float(range_m)
            if not math.isfinite(range_m) or range_m <= 0:
                range_m = None
        return bearing, range_m, confidence

    def _target_step(self, task: SkillTask) -> bool:
        target_id = task.arguments["id"]
        try:
            geometry = self._target_geometry(target_id)
        except _TargetNotVisible:
            if task.skill != "follow":
                raise
            if task._target_lost_at is None:
                task._target_lost_at = time.monotonic()
                task._target_lost_at_sim_s = self._sim_time_s()
            match = self._reidentify_target(task.tracking["reference_id"])
            if match is None:
                logger.debug("follow target %s not visible and not re-identified; holding", target_id)
                self._command_zero_velocity()
                return False
            target_id, similarity, threshold = match
            try:
                geometry = self._target_geometry(target_id)
            except _TargetNotVisible:
                self._command_zero_velocity()
                return False
            task.arguments["id"] = target_id
            task.executed_target["id"] = target_id
            self._record_reacquisition(task, target_id, similarity, threshold)
        else:
            if task.skill == "follow" and task._target_lost_at is not None:
                match = self._reidentify_target(task.tracking["reference_id"])
                if match is None:
                    self._command_zero_velocity()
                    return False
                target_id, similarity, threshold = match
                if target_id != task.arguments["id"]:
                    geometry = self._target_geometry(target_id)
                task.arguments["id"] = target_id
                task.executed_target["id"] = target_id
                self._record_reacquisition(task, target_id, similarity, threshold)

        bearing, distance, _ = geometry
        telemetry = self._telemetry()
        heading_deg = float(getattr(telemetry, "heading_deg", 0.0) or 0.0)
        heading = math.radians(heading_deg) + bearing
        facing_deg = math.degrees(heading) % 360.0
        logger.debug(
            "%s target=%s bearing_deg=%.1f range_m=%s facing_deg=%.1f",
            task.skill, task.arguments["id"], math.degrees(bearing),
            "none" if distance is None else f"{distance:.2f}", facing_deg,
        )
        if task.skill == "follow":
            desired = float(task.executed_target["dist_m"])
            if distance is None:
                # Safe visual following: turn toward the target's bearing without translating
                # until a calibrated range estimate is available.
                self.flight.set_velocity_ned(0.0, 0.0, 0.0, yaw_deg=facing_deg)
                return False
            self._range_velocity(heading, distance - desired, yaw_deg=facing_deg)
            return False
        if task.skill == "approach":
            if distance is None:
                raise RuntimeError("approach needs a calibrated target range; refusing unbounded forward motion")
            desired = float(task.executed_target["dist_m"])
            if distance <= desired + self.settings.arrival_tolerance_m:
                self.flight.set_velocity_ned(0.0, 0.0, 0.0, yaw_deg=facing_deg)
                return True
            self._range_velocity(heading, distance - desired, yaw_deg=facing_deg)
            return False
        if task.skill == "orbit":
            if distance is None:
                raise RuntimeError("orbit needs a calibrated target range; refusing open-loop orbit")
            radius = float(task.executed_target["radius_m"])
            self._orbit_velocity(heading, distance, radius)
            return False
        raise RuntimeError(f"unsupported target skill {task.skill}")

    def _reidentify_target(
        self, reference_id: int
    ) -> tuple[int, float, float] | None:
        reidentify = getattr(self.perception, "reidentify_candidate", None)
        if not callable(reidentify):
            return None
        match = reidentify(reference_id)
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
        return candidate_id, min(1.0, similarity), threshold

    def _record_reacquisition(
        self, task: SkillTask, candidate_id: int, similarity: float, threshold: float
    ) -> None:
        if task._target_lost_at is None:
            return
        # Occlusions are timed in simulation/autopilot time when available (SYSTEM_TIME), so the
        # measured gap matches the scenario's design at any real-time factor; wall time otherwise.
        now_sim = self._sim_time_s()
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
        task._target_lost_at = None
        task._target_lost_at_sim_s = None

    def _sim_time_s(self) -> float | None:
        boot_ms = getattr(self._telemetry(), "time_boot_ms", None)
        try:
            value = float(boot_ms) / 1000.0
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    def _range_velocity(self, heading: float, range_error: float, *, yaw_deg: float | None = None) -> None:
        speed = min(self.settings.max_speed_m_s, max(0.0, abs(range_error) * 0.5))
        direction = 1.0 if range_error > 0 else -1.0
        self.flight.set_velocity_ned(
            direction * speed * math.cos(heading),
            direction * speed * math.sin(heading),
            0.0,
            yaw_deg=yaw_deg,
        )

    def _orbit_velocity(self, bearing: float, distance: float, radius: float) -> None:
        radial_speed = min(self.settings.max_speed_m_s * 0.5, abs(distance - radius) * 0.5)
        radial_sign = 1.0 if distance > radius else -1.0
        tangent_speed = min(self.settings.max_speed_m_s * 0.5, max(0.2, radius * 0.35))
        # Move toward target when outside the orbit, away when inside; add a tangent.
        toward_n = math.cos(bearing) * radial_sign * radial_speed
        toward_e = math.sin(bearing) * radial_sign * radial_speed
        tangent_n = math.cos(bearing + math.pi / 2.0) * tangent_speed
        tangent_e = math.sin(bearing + math.pi / 2.0) * tangent_speed
        north, east = toward_n + tangent_n, toward_e + tangent_e
        magnitude = math.hypot(north, east)
        if magnitude > self.settings.max_speed_m_s:
            north *= self.settings.max_speed_m_s / magnitude
            east *= self.settings.max_speed_m_s / magnitude
        self.flight.set_velocity_ned(north, east, 0.0)

    def _command_zero_velocity(self) -> None:
        self.flight.set_velocity_ned(0.0, 0.0, 0.0)

    def _precision_frame(self) -> Any | None:
        if self.perception is None:
            return None
        get_frame = getattr(self.perception, "get_frame", None)
        if not callable(get_frame):
            return None
        frame = get_frame()
        if frame is None or getattr(frame, "image", None) is None:
            return None
        try:
            timestamp = float(getattr(frame, "timestamp"))
        except (TypeError, ValueError, OverflowError):
            return None
        age = time.monotonic() - timestamp
        if not math.isfinite(timestamp) or age < -0.25 or age > self.PRECISION_FRAME_MAX_AGE_S:
            return None
        return frame

    def _precision_landing_step(self, action: str) -> None:
        try:
            self._precision_landing_step_impl(action)
        except Exception as exc:
            logger.warning("precision landing error: %s", exc, exc_info=True)
            self._precision_stats["last_fallback"] = f"error: {exc}"
            if self._precision_active:
                self._precision_controller_failed()
            else:
                # A detector or gimbal failure leaves the existing RTL/LAND mode in control.
                self._precision_unavailable = True

    def _precision_landing_step_impl(self, action: str) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        pose_value = getattr(telemetry, "pose", None)
        if isinstance(pose_value, Pose):
            current = pose_value
        elif isinstance(pose_value, Mapping):
            current = self._parse_pose(pose_value)
        else:
            return
        if not all(math.isfinite(value) for value in (current.north_m, current.east_m, current.up_m)):
            return
        pose_updated = getattr(telemetry, "pose_updated_monotonic", None)
        if pose_updated is None:
            return
        age = time.monotonic() - float(pose_updated)
        if not math.isfinite(age) or age > 1.5:
            if self._precision_active:
                self._precision_controller_failed()
            return
        if getattr(telemetry, "armed", None) is False:
            self._precision_landing_complete()
            return
        if self._precision_active and current.up_m <= self.PRECISION_HANDOFF_ALTITUDE_M:
            self._precision_stats["completed_landings"] += 1
            logger.info("precision landing reached hand-off altitude; handing touchdown to LAND")
            self._command_zero_velocity()
            getattr(self.flight, "land")()
            self._precision_landing_complete()
            return
        home_distance = math.hypot(current.north_m, current.east_m)
        if home_distance > self.PRECISION_HOME_RADIUS_M:
            if self._precision_active:
                getattr(self.flight, action)()
                self._precision_active = False
            return
        if self._precision_unavailable:
            return

        frame = self._precision_frame()
        if frame is None:
            if self._precision_active:
                self.flight.set_velocity_ned(0.0, 0.0, 0.0)
            return
        if self._precision_gimbal_requested_at is None:
            set_gimbal = getattr(self.flight, "set_gimbal_pitch_yaw", None)
            if not callable(set_gimbal):
                self._precision_stats["last_fallback"] = "no gimbal control"
                self._precision_unavailable = True
                return
            set_gimbal(-90.0, 0.0)
            self._precision_gimbal_requested_at = time.monotonic()
            return
        if frame.timestamp < self._precision_gimbal_requested_at + self.PRECISION_GIMBAL_SETTLE_S:
            if self._precision_active:
                self.flight.set_velocity_ned(0.0, 0.0, 0.0)
            return

        heading = getattr(telemetry, "heading_deg", None)
        if heading is None or not math.isfinite(float(heading)):
            if self._precision_active:
                self.flight.set_velocity_ned(0.0, 0.0, 0.0)
            return
        detection, velocity = self._precision_controller.observe(frame.image, float(heading))
        if detection is None:
            if self._precision_active:
                self.flight.set_velocity_ned(0.0, 0.0, 0.0)
            return
        self._precision_stats["tag_detections"] += 1
        if not self._precision_active:
            self._precision_active = True
            self._precision_stats["acquisitions"] += 1
            logger.info("precision landing acquired AprilTag at %.2f m from home, %.2f m up",
                        home_distance, current.up_m)
            self.flight.set_guided()
        self.flight.set_velocity_ned(*velocity)

    def _level_gimbal(self) -> None:
        set_gimbal = getattr(self.flight, "set_gimbal_pitch_yaw", None)
        if callable(set_gimbal):
            try:
                set_gimbal(0.0, 0.0)
            except Exception as exc:  # the camera orientation must never block flight
                logger.warning("could not level gimbal: %s", exc)

    def _precision_landing_complete(self) -> None:
        if self._precision_gimbal_requested_at is not None:
            self._level_gimbal()
        self._precision_action = None
        self._precision_active = False
        self._precision_gimbal_requested_at = None
        self._precision_unavailable = False
        self._auto_rth_after_hold = False
        self._holding_since = None
        with self._lock:
            self._state = ExecutorState.IDLE

    def _precision_controller_failed(self) -> None:
        logger.warning("precision landing lost control; holding")
        if self._precision_stats["last_fallback"] is None:
            self._precision_stats["last_fallback"] = "controller failed (stale pose or error)"
        try:
            self._safe_hold()
        except Exception:
            pass
        self._precision_action = None
        self._precision_active = False
        self._precision_gimbal_requested_at = None
        self._precision_unavailable = True
        self._auto_rth_after_hold = False
        self._holding_since = None
        with self._lock:
            self._state = ExecutorState.HOLDING


    def _safe_hold(self) -> None:
        try:
            self.flight.hold()
        except AttributeError:
            self.flight.set_guided()
            self._command_zero_velocity()

    def _finish(self, task: SkillTask, status: str, error: str | None) -> None:
        if task.skill == "scan":
            task.executed_target = [point.as_dict() for point in task._waypoints]
        task.status = status
        task.error = error
        task.result = {
            "success": status == "succeeded",
            "status": status,
            "requested_target": task.requested_target,
            "executed_target": task.executed_target,
            "clamp_reason": task.clamp_reason,
            "error": error,
        }
        if task.skill == "follow":
            task.result["tracking"] = dict(task.tracking)
        if task.skill == "scan":
            task.result["skipped_waypoints"] = list(task.skipped_waypoints)
        if self._active_id == task.task_id:
            self._active_id = None
        if status in ("succeeded", "cancelled", "expired", "failed"):
            self._holding_since = time.monotonic()
            self._auto_rth_after_hold = status in ("succeeded", "cancelled", "expired", "failed")
            self._state = ExecutorState.HOLDING

    @staticmethod
    def _task_dict(task: SkillTask) -> dict[str, Any]:
        result = {
            "task_id": task.task_id,
            "skill": task.skill,
            "status": task.status,
            "created_at": task.created_at,
            "requested_target": task.requested_target,
            "executed_target": task.executed_target,
            "clamp_reason": task.clamp_reason,
            "result": task.result,
            "error": task.error,
        }
        if task.skill == "follow":
            result["tracking"] = dict(task.tracking)
        if task.skill == "scan":
            result["skipped_waypoints"] = list(task.skipped_waypoints)
        return result

    def get_task_status(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                task = self._tasks[task_id]
            except KeyError as exc:
                raise KeyError(f"unknown task_id {task_id!r}") from exc
            return self._task_dict(task)

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"unknown task_id {task_id!r}")
            if task.status in ("succeeded", "failed", "cancelled", "expired"):
                return self._task_dict(task)
            if task.task_id == self._active_id:
                task.cancel_requested = True
            else:
                task.status = "cancelled"
                task.error = "task cancelled before execution"
                task.result = {
                    "success": False,
                    "status": "cancelled",
                    "requested_target": task.requested_target,
                    "executed_target": task.executed_target,
                    "clamp_reason": task.clamp_reason,
                    "error": task.error,
                }
                if task.skill == "scan":
                    task.result["skipped_waypoints"] = list(task.skipped_waypoints)
            return self._task_dict(task)

    def _emergency(self, action: str) -> dict[str, Any]:
        with self._lock:
            for task in self._tasks.values():
                if task.status in ("queued", "running"):
                    task.status = "cancelled"
                    task.error = f"preempted by emergency {action}"
                    task.result = {
                        "success": False,
                        "status": "cancelled",
                        "requested_target": task.requested_target,
                        "executed_target": task.executed_target,
                        "clamp_reason": task.clamp_reason,
                        "error": task.error,
                    }
                    if task.skill == "scan":
                        task.result["skipped_waypoints"] = list(task.skipped_waypoints)
            self._active_id = None
            self._queue.clear()
            self._holding_since = time.monotonic()
            self._auto_rth_after_hold = action == "hold"
            if action in ("rth", "land"):
                self._precision_action = action
                self._precision_active = False
                self._precision_gimbal_requested_at = None
                self._precision_unavailable = False
            else:
                self._precision_action = None
                self._precision_active = False
                self._precision_gimbal_requested_at = None
                self._precision_unavailable = False
            if action == "rth":
                self._state = ExecutorState.RTH
            else:
                self._state = ExecutorState.HOLDING
        if action == "hold":
            self._safe_hold()
        elif action == "rth":
            self.flight.rth()
        elif action == "land":
            self.flight.land()
        return {"action": action, "state": self.state.value, "preempted": True}

    def hold(self) -> dict[str, Any]:
        return self._emergency("hold")

    def rth(self) -> dict[str, Any]:
        return self._emergency("rth")

    def land(self) -> dict[str, Any]:
        return self._emergency("land")
