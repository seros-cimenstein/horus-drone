"""Bounded skill executor; every autonomous translation is a closed-loop NED velocity command."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, NamedTuple

from horus.config import ExecutorSettings
from horus.executor.precision_landing import AprilTagPrecisionLandingController
from horus.events import EventBus
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


class _TargetGeometry(NamedTuple):
    """Validated target geometry, the range source used, and the detector label seen."""

    bearing_rad: float
    range_m: float | None
    confidence: float
    range_source: str | None
    range_note: str | None
    label: str


class _Preempted(BaseException):
    """An emergency superseded the controller tick that was about to command the vehicle.

    A BaseException, like asyncio.CancelledError, so no step or precision-landing error handler
    can swallow it and fall back to its own hold/finish path.
    """


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
    _target_pose: Pose | None = None
    _arm_requested_monotonic: float = 0.0
    tracking: dict[str, Any] = field(default_factory=dict)
    _target_lost_at: float | None = None
    _target_lost_at_sim_s: float | None = None
    _range_note_warned: bool = False
    _finished_event_published: bool = False
    _search_frame_id: int | None = None


# Skills whose status/result carries a ``tracking`` record.
_TRACKING_SKILLS = frozenset({"approach", "follow", "orbit", "search"})


class HorusExecutor:
    """Serialize skills and continuously command bounded velocity from current telemetry."""

    SKILLS = {"take_off", "goto", "approach", "follow", "orbit", "scan", "search"}
    ARM_RETRY_INTERVAL_S = 2.0
    HEADING_TOLERANCE_DEG = 5.0
    LINK_EVENT_DEBOUNCE_S = 5.0
    PRECISION_HOME_RADIUS_M = 3.0
    # Below this altitude the tag overflows the camera view; hand the centred vehicle to vertical LAND.
    PRECISION_HANDOFF_ALTITUDE_M = 0.6
    PRECISION_FRAME_MAX_AGE_S = 1.0
    PRECISION_GIMBAL_SETTLE_S = 0.5
    # Pose, heading, forward-range and frame samples older than ExecutorSettings.telemetry_max_age_s
    # (default 1.5 s wall) never drive translation; the flight link streams the control messages
    # fast enough in autopilot time that the wall gap stays well below it at low sim RTF.


    def __init__(
        self,
        flight: Any,
        settings: ExecutorSettings,
        perception: Any | None = None,
        *,
        event_bus: EventBus | None = None,
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
            settings.camera_horizontal_fov_deg,
            settings.target_match_threshold,
            settings.link_loss_rth_delay_s,
            settings.precision_landing_stall_s,
            settings.precision_landing_min_descent_m_s,
            settings.precision_landing_target_grace_s,
            settings.telemetry_max_age_s,
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
        if settings.precision_landing_stall_s <= 0 or settings.precision_landing_min_descent_m_s <= 0:
            raise ValueError(
                "precision_landing_stall_s and precision_landing_min_descent_m_s must be positive"
            )
        if settings.precision_landing_target_grace_s <= 0:
            raise ValueError("precision_landing_target_grace_s must be positive")
        if settings.telemetry_max_age_s <= 0:
            raise ValueError("telemetry_max_age_s must be positive")
        if not 0.0 <= settings.target_match_threshold <= 1.0:
            raise ValueError("target_match_threshold must be in [0, 1]")
        self.flight = flight
        self.settings = settings
        self.perception = perception
        self.event_bus = event_bus
        self._lock = threading.RLock()
        # Lock order: _command_lock, then _lock. _command_lock covers only autopilot command
        # operations, never perception or model work, so an emergency waits at most for one
        # guarded command operation in progress (a hold is Guided plus a zero-velocity setpoint).
        self._command_lock = threading.Lock()
        # Bumped by every emergency; a worker command is sent only if the epoch its tick
        # snapshotted (kept per thread in _tick_context) is still current.
        self._emergency_epoch = 0
        self._tick_context = threading.local()
        self._state = ExecutorState.IDLE
        self._tasks: dict[str, SkillTask] = {}
        self._queue: deque[str] = deque()
        self._active_id: str | None = None
        self._stop = threading.Event()
        self._holding_since: float | None = None
        self._auto_rth_after_hold = False
        # Any MCP call (including the commander's event long-poll) proves the supervisory link.
        self._last_supervisor_contact = time.monotonic()
        self._heartbeat_state: bool | None = None
        self._heartbeat_change_since: float | None = None
        self._previous_armed: bool | None = None
        self._precision_controller = AprilTagPrecisionLandingController()
        self._precision_action: str | None = None
        self._precision_active = False
        self._precision_gimbal_requested_at: float | None = None
        self._precision_unavailable = False
        self._precision_stall_started_monotonic: float | None = None
        self._precision_stall_start_altitude_m: float | None = None
        self._precision_stall_start_sim_s: float | None = None
        self._precision_target_monotonic: float | None = None
        self._precision_target_velocity: tuple[float, float, float] | None = None
        self._precision_last_log_monotonic = 0.0
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
            with self._command_lock:
                self._flight_hold()
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
        if skill in {"approach", "follow", "orbit"}:
            # Every target skill holds and re-identifies a temporarily invisible target; the
            # reacquisition record is shared so approach/orbit report exactly like follow, and
            # the hold is bounded by the task TTL that bounds follow's.
            task.tracking = {
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
            task.tracking = {
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
            requested = {"id": args["id"], "dist_m": distance}
            executed = {"id": args["id"], "dist_m": bounded}
            reason = "approach distance clamped to [0.5 m, max_distance_m]" if bounded != distance else None
            self._prepare_target_size(args, requested, executed)
            return requested, executed, reason
        if skill == "follow":
            self._require_target_id(args)
            distance = self._finite_argument(args.get("dist_m", self.settings.follow_distance_m), "dist_m")
            bounded = min(self.settings.max_distance_m, max(0.5, distance))
            requested = {"id": args["id"], "dist_m": distance}
            executed = {"id": args["id"], "dist_m": bounded}
            reason = "follow distance clamped to [0.5 m, max_distance_m]" if bounded != distance else None
            self._prepare_target_size(args, requested, executed)
            return requested, executed, reason
        if skill == "orbit":
            self._require_target_id(args)
            radius = self._finite_argument(args.get("radius_m", args.get("dist_m", 2.0)), "radius_m")
            bounded = min(self.settings.max_distance_m, max(0.75, radius))
            requested = {"id": args["id"], "radius_m": radius}
            executed = {"id": args["id"], "radius_m": bounded}
            reason = "orbit radius clamped to [0.75 m, max_distance_m]" if bounded != radius else None
            self._prepare_target_size(args, requested, executed)
            return requested, executed, reason

        if skill in ("scan", "search"):
            if not isinstance(args.get("area"), Mapping):
                raise ValueError(f"{skill} requires a rectangular area mapping")
            if skill == "search":
                self._prepare_search_target(args)
            requested, waypoints, reasons = self._scan_waypoints(args["area"])
            return requested, [point.as_dict() for point in waypoints], "; ".join(reasons) or None
        raise ValueError(f"cannot prepare {skill}")

    def _prepare_search_target(self, args: dict[str, Any]) -> None:
        """Search matches either a whole public cue (label + optional attribute) or a lost ID."""
        has_class = args.get("class_name") is not None
        has_reference = args.get("reference_id") is not None
        if has_class == has_reference:
            raise ValueError("search needs exactly one of class_name or reference_id")
        if self.perception is None:
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

    def _prepare_target_size(
        self, args: dict[str, Any], requested: dict[str, Any], executed: dict[str, Any]
    ) -> None:
        if args.get("target_size_m") is None:
            return
        target_size = self._finite_argument(args["target_size_m"], "target_size_m")
        if target_size <= 0.0:
            raise ValueError("target_size_m must be positive")
        args["target_size_m"] = target_size
        requested["target_size_m"] = target_size
        executed["target_size_m"] = target_size

    def _publish_event(self, kind: str, summary: str, **data: Any) -> None:
        if self.event_bus is not None:
            self.event_bus.publish(kind, summary, **data)

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

    def _observe_vehicle_events(self) -> None:
        heartbeat_check = getattr(self.flight, "heartbeat_ok", None)
        if callable(heartbeat_check):
            try:
                heartbeat_ok = bool(heartbeat_check())
            except Exception:
                heartbeat_ok = None
            if heartbeat_ok is not None:
                now = time.monotonic()
                if self._heartbeat_state is None:
                    self._heartbeat_state = heartbeat_ok
                elif heartbeat_ok == self._heartbeat_state:
                    self._heartbeat_change_since = None
                elif self._heartbeat_change_since is None:
                    self._heartbeat_change_since = now
                elif now - self._heartbeat_change_since >= self.LINK_EVENT_DEBOUNCE_S:
                    # Only a change that persists is an event; a slow sim or bursty link must
                    # not flap link_lost/link_restored at the commander every few seconds.
                    self._heartbeat_state = heartbeat_ok
                    self._heartbeat_change_since = None
                    kind = "link_restored" if heartbeat_ok else "link_lost"
                    summary = "Autopilot heartbeat restored" if heartbeat_ok else "Autopilot heartbeat lost"
                    self._publish_event(kind, summary)

        telemetry = self._telemetry()
        armed = getattr(telemetry, "armed", None)
        if not isinstance(armed, bool):
            return
        if self._previous_armed is True and not armed:
            pose = self._current_pose()
            if (
                pose is not None
                and math.isfinite(pose.up_m)
                and pose.up_m <= self.PRECISION_HANDOFF_ALTITUDE_M
            ):
                self._publish_event(
                    "landed",
                    "Vehicle disarmed at low altitude",
                    position=pose.as_dict(),
                )
        self._previous_armed = armed

    def tick(self) -> None:
        """Perform one controller step; public for deterministic tests and embedded loops."""
        self._observe_vehicle_events()
        restore_default_pitch = False
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
                    # A skill preempting RTH/LAND precision landing must get the camera back.
                    restore_default_pitch = (
                        self._precision_gimbal_requested_at is not None
                    )
                    self._precision_action = None
                    self._precision_active = False
                    self._precision_gimbal_requested_at = None
                    self._precision_unavailable = False
                    self._reset_precision_watch()
                    break
            task = None if self._active_id is None else self._tasks[self._active_id]
            rth_due = (
                task is None
                and self._state == ExecutorState.HOLDING
                and self._auto_rth_after_hold
                and self._holding_since is not None
                and time.monotonic() - max(self._holding_since, self._last_supervisor_contact)
                >= self.settings.link_loss_rth_delay_s
            )
            precision_action = self._precision_action
            holding = self._state == ExecutorState.HOLDING
            epoch = self._emergency_epoch
            self._tick_context.epoch = epoch
        try:
            if restore_default_pitch:
                self._command(self._set_default_gimbal_pitch)
            if task is None:
                if precision_action is not None:
                    self._precision_landing_step(precision_action)
                elif rth_due:
                    if self._emergency("rth", source=None, expected_epoch=epoch) is not None:
                        self._publish_event("auto_rth", "Automatic return-to-home triggered")
                elif holding:
                    self._command_zero_velocity()
                return
            if task.cancel_requested:
                self._safe_hold()
                self._finish_running(task, "cancelled", "task cancelled")
                return
            if time.monotonic() >= task.expires_monotonic:
                self._safe_hold()
                self._finish_running(task, "expired", "command TTL expired; executor entered GUIDED hold")
                return
            try:
                completed = self._step(task)
            except Exception as exc:
                self._safe_hold()
                self._finish_running(task, "failed", str(exc))
                return
            if completed:
                self._safe_hold()
                self._finish_running(task, "succeeded", None)
        except _Preempted:
            # The emergency already recorded the task as cancelled and owns vehicle and state.
            logger.info("controller tick superseded by an emergency command")
        finally:
            self._tick_context.epoch = None

    def _command(self, send: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Send a worker-thread autopilot command unless an emergency superseded this tick."""
        with self._command_lock:
            epoch = getattr(self._tick_context, "epoch", None)
            if epoch is None:
                raise RuntimeError("executor flight commands must be issued from a controller tick")
            if epoch != self._emergency_epoch:
                raise _Preempted
            return send(*args, **kwargs)

    def _require_current_tick(self) -> None:
        """Refuse a worker state write after an emergency; call with _lock held."""
        if getattr(self._tick_context, "epoch", None) != self._emergency_epoch:
            raise _Preempted

    def _finish_running(self, task: SkillTask, status: str, error: str | None) -> None:
        with self._lock:
            # An emergency may have cancelled this task after its tick started; keep that outcome.
            if task.status == "running":
                self._finish(task, status, error)

    def _step(self, task: SkillTask) -> bool:
        if task._phase == "start":
            self._command(self.flight.set_guided)
            if task.skill == "take_off":
                # Set the configured search angle after any prior precision landing.
                self._command(self._set_default_gimbal_pitch)
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
            elif task.skill in ("scan", "search"):
                task._waypoints = [self._parse_pose(point) for point in task.executed_target]
            task._phase = "control"
        if task.skill == "take_off" and task._phase == "guiding":
            if str(getattr(self._telemetry(), "mode", "")).upper() != "GUIDED":
                return False
            self._command(self.flight.arm)
            task._arm_requested_monotonic = time.monotonic()
            task._phase = "arming"
            return False
        if task.skill == "take_off" and task._phase == "arming":
            if not bool(getattr(self._telemetry(), "armed", False)):
                # ArduPilot rejects arming while pre-arm checks settle (e.g. "Accels inconsistent"
                # after a cold start); keep asking until armed or the task TTL expires.
                now = time.monotonic()
                if now - task._arm_requested_monotonic >= self.ARM_RETRY_INTERVAL_S:
                    self._command(self.flight.arm)
                    task._arm_requested_monotonic = now
                return False
            self._command(self.flight.takeoff, float(task.executed_target["altitude_m"]))
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
                    self._command(self.flight.set_velocity_ned, 0.0, 0.0, 0.0, yaw_deg=heading)
                    return False
            return arrived
        if task.skill == "search":
            state = self._search_step(task)
            if state == "found":
                return True
            if state == "camera_stale":
                # Never fly the raster blind: hover until fresh frames return or the TTL expires.
                self._command_zero_velocity()
                return False
        if task.skill in ("scan", "search"):
            while task._waypoint_index < len(task._waypoints):
                target, reasons = self._clamp_pose(task._waypoints[task._waypoint_index])
                if reasons:
                    task.clamp_reason = "; ".join(filter(None, [task.clamp_reason, *reasons]))
                    task._waypoints[task._waypoint_index] = target
                if self._goto_step(target):
                    task._waypoint_index += 1
                    continue
                return False
            if task.skill == "search":
                raise RuntimeError("search covered the area without a validated match")
            return True
        return self._target_step(task)

    def _search_step(self, task: SkillTask) -> str | None:
        """Check one newly processed, fresh frame; return ``"found"``, ``"camera_stale"`` or None.

        Matches come from that frame's own candidates and must validate on that same frame, so a
        newer frame arriving mid-check cannot be attributed to the checked one. Every validated
        whole-cue match in the frame is recorded; ``found_id`` is set only when exactly one is
        visible, otherwise the result is ``ambiguous`` (never a guess).
        """
        observation = self.perception.latest_observation()
        if observation is None:
            return "camera_stale"
        frame_id = getattr(observation, "frame_id", None)
        timestamp = getattr(observation, "timestamp", None)
        if timestamp is None or time.monotonic() - float(timestamp) > self.settings.telemetry_max_age_s:
            return "camera_stale"
        if frame_id == task._search_frame_id:
            return None
        task._search_frame_id = frame_id
        tracking = task.tracking
        if tracking["mode"] == "reidentify":
            match = self._reidentify_target(int(tracking["reference_id"]))
            visible = {self._value(candidate, "id"): candidate for candidate in observation.candidates}
            if match is not None and match[0] in visible:
                candidate = visible[match[0]]
                matches = [(match[0], None, [float(v) for v in self._value(candidate, "bbox", ())])]
            else:
                matches = []
            similarity = None if not matches else match[1]
        else:
            matches = self._search_cue_matches(
                observation, tracking["class_name"], tracking["attribute"], tracking["equals"]
            )
            similarity = None
        with self._lock:
            self._require_current_tick()
            tracking["frames_checked"] += 1
        if not matches:
            return None
        self._command_zero_velocity()
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
        # Record and publish under the lock an emergency takes, so a preempted search never
        # reports a find after the emergency.
        with self._lock:
            self._require_current_tick()
            tracking.update(
                found=True,
                found_id=found_id,
                matching_ids=matching_ids,
                ambiguous=found_id is None,
                similarity=similarity,
                found_at_waypoint=task._waypoint_index,
                found_frame_id=frame_id,
            )
            self._publish_event(
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
        return "found"

    def _search_cue_matches(
        self, observation: Any, class_name: str, attribute: str | None, equals: Any
    ) -> list[tuple[int, str, list[float]]]:
        """The frame's candidates with this label (and attribute value) validated on that frame."""
        wanted = class_name.casefold()
        frame_id = getattr(observation, "frame_id", None)
        validate = getattr(self.perception, "validate_candidate", None)
        matches = []
        for candidate in observation.candidates:
            if str(self._value(candidate, "class_name", "")).casefold() != wanted:
                continue
            if attribute is not None:
                attributes = self._value(candidate, "attributes", {})
                if not isinstance(attributes, Mapping) or attributes.get(attribute) != equals:
                    continue
            candidate_id = int(self._value(candidate, "id"))
            if callable(validate):
                validated = validate(candidate_id)
                if validated is None or self._value(validated, "frame_id", frame_id) != frame_id:
                    continue
            bbox = [float(value) for value in self._value(candidate, "bbox", ())]
            matches.append((candidate_id, str(self._value(candidate, "class_name", "")), bbox))
        return sorted(matches)

    def _goto_step(self, target: Pose) -> bool:
        current = self._current_pose()
        if current is None:
            raise RuntimeError("local vehicle pose unavailable; refusing open-loop movement")
        self._require_fresh(self._telemetry(), "pose_updated_monotonic", "pose")
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
        self._command(self.flight.set_velocity_ned, vn, ve, -vu)
        return False

    def _require_fresh(self, telemetry: Any, field_name: str, label: str) -> None:
        updated = getattr(telemetry, field_name, None)
        age = math.inf if updated is None else max(0.0, time.monotonic() - float(updated))
        if age > self.settings.telemetry_max_age_s:
            raise RuntimeError(f"vehicle {label} is stale ({age:.2f}s); entering hold")

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

    def _target_geometry(
        self, target_id: Any, target_size_m: float | None = None
    ) -> _TargetGeometry:
        if self.perception is None:
            raise RuntimeError("perception identity revalidation is required before target action")
        candidate_validator = getattr(self.perception, "validate_candidate", None)
        if callable(candidate_validator):
            candidate = candidate_validator(target_id)
            if candidate is None:
                raise _TargetNotVisible(self._target_not_visible_message(target_id))
        else:
            candidate = self._candidate(target_id)
            if candidate is None:
                raise _TargetNotVisible(
                    f"tracked target {target_id!r} is not visible in the current frame"
                )
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
        if range_m is not None:
            try:
                range_m = float(range_m)
            except (TypeError, ValueError, OverflowError):
                range_m = None
            if range_m is not None and (not math.isfinite(range_m) or range_m <= 0.0):
                range_m = None
        range_source = "candidate" if range_m is not None else None
        class_name = str(
            self._value(candidate, "class_name", self._value(candidate, "label", ""))
        ).strip().lower()
        class_id = self._value(candidate, "class_id")
        box = self._value(candidate, "box", self._value(candidate, "bbox"))
        image_width = float(self._value(candidate, "frame_width", 640.0))
        focal_px = image_width / (
            2.0 * math.tan(math.radians(self.settings.camera_horizontal_fov_deg) / 2.0)
        )

        def size_range(size_m: float | None, *, vertical: bool) -> float | None:
            if size_m is None or box is None:
                return None
            if isinstance(box, Mapping):
                pixels = (
                    float(box.get("y2", box.get("bottom"))) - float(box.get("y1", box.get("top")))
                    if vertical else
                    float(box.get("x2", box.get("right"))) - float(box.get("x1", box.get("left")))
                )
            else:
                pixels = float(box[3] - box[1]) if vertical else float(box[2] - box[0])
            return size_m * focal_px / pixels if pixels > 1.0 else None

        range_note: str | None = None
        # Ground-plane geometry: with a known height above ground, camera pitch and frame height,
        # the ray through the box's ground-contact row intersects the ground plane. It is
        # posture-agnostic and measured accurate (1% median error on 52 logged campaign boxes
        # against the true camera-to-torso slant, versus +24% median for the configured person
        # height), so it also replaces the image-only bearing for translation and the ToF gate.
        ground_geometry = self._ground_plane_geometry(box, candidate)
        if ground_geometry is not None:
            bearing = ground_geometry[1]
        # A fresh measured forward ToF range outranks every assumed-height estimate: an assumed
        # height can be wrong by the object's true size, while the ToF measures the distance to
        # what is actually ahead inside the 8 deg bearing gate. It is only usable when the
        # target's ray stays on the beam's plane (see _forward_tof_applies): a single-ray lidar
        # at flight altitude otherwise returns whatever is behind a target that is below it.
        if (
            range_m is None
            and abs(bearing) <= math.radians(8.0)
            and self._forward_tof_applies(box, candidate)
        ):
            range_m = self._forward_proximity_range()
            if range_m is not None:
                range_source = "forward_tof"
        # The ground-plane estimate outranks the size-based estimates: the operator's declared
        # size is a height, and a prone or foreshortened box is not the object's height, so
        # size-based ranges are posture-dependent while the ground-plane ray is not.
        if range_m is None and ground_geometry is not None:
            range_m = ground_geometry[0]
            range_source = "ground_plane"
            range_note = (
                f"ground-plane ray from {ground_geometry[3]:.2f} m above ground at "
                f"pitch {ground_geometry[4]:.1f} deg: {ground_geometry[2]:.2f} m horizontal"
            )
        # target_size_m is the operator's declared height for this specific target.
        if range_m is None and target_size_m is not None:
            range_m = size_range(target_size_m, vertical=True)
            if range_m is not None:
                range_source = "target_size"
        # target_height_m is the configured height for these target skills (config/horus.yaml:
        # "known target height for monocular range"). It is only a person-height assumption, so it
        # may not silently range an arbitrary object: it applies when the target is a person (the
        # detector label, a classless candidate, or track-level person evidence -- a person-only
        # shirt_colour attribute, or a track whose history shows a person label, which survives a
        # frame that mislabels a person as "kite"). A target with a non-person label and no person
        # evidence keeps refusing below, and its error names the label so the operator can pass
        # target_size_m. Any disagreement is recorded in tracking and logged, never silent.
        attributes = self._value(candidate, "attributes")
        source = str(self._value(candidate, "source") or "")
        # An open-vocabulary ("grounder") seed may name a person for selection and display, but
        # a text-grounding guess is not a detector person: ranged from the 1.7 m person height a
        # false seed (bush, rock) would fly the vehicle closer than the commanded standoff, and a
        # prone body's box height is not a standing person's anyway. Such a target therefore keeps
        # refusing below (the error names the label) until the operator passes target_size_m, or
        # until the detector labels the track person or the shirt-colour vote adds evidence.
        label_is_person = (class_name == "person" or class_id == 0) and source != "open_vocab"
        person_evidence = bool(self._value(candidate, "person_evidence", False)) or (
            # shirt_colour is classified only for person-class detections (perception pipeline).
            isinstance(attributes, Mapping) and attributes.get("shirt_colour") is not None
        )
        target_height_applies = (
            label_is_person
            or person_evidence
            or (not class_name and class_id is None)
        )
        if range_m is None and self.settings.target_height_m is not None and target_height_applies:
            range_m = size_range(self.settings.target_height_m, vertical=True)
            if range_m is not None:
                range_source = "person_height"
                if not label_is_person and class_name:
                    range_note = (
                        f"configured target height {self.settings.target_height_m:g} m used for a "
                        f"target labeled {class_name!r} with person evidence"
                    )
        if range_m is None and self.settings.target_width_m is not None:
            range_m = size_range(self.settings.target_width_m, vertical=False)
            if range_m is not None:
                range_source = "configured_target_width"

        confidence = float(self._value(candidate, "confidence", 1.0))
        if not math.isfinite(bearing) or not math.isfinite(confidence) or confidence < 0.2:
            raise RuntimeError("target identity/confidence did not pass on-arrival validation")
        if range_m is not None:
            range_m = float(range_m)
            if not math.isfinite(range_m) or range_m <= 0:
                range_m = None
                range_source = None
                range_note = None
        return _TargetGeometry(bearing, range_m, confidence, range_source, range_note, class_name)

    def _target_not_visible_message(self, target_id: Any) -> str:
        """Distinguish a gone/stale candidate from a reference-appearance mismatch.

        Both causes used to raise one message, hiding which part of revalidation failed. The
        perception layer's failure reason is used when it is available; otherwise the current
        frame's candidate table still separates a candidate that is gone from one that is present
        but failed validation.
        """
        failure = getattr(self.perception, "validate_candidate_failure", None)
        reason = failure(target_id) if callable(failure) else None
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
        if self._candidate(target_id) is None:
            return f"tracked target {target_id!r} is not visible in the current frame"
        return f"tracked target {target_id!r} failed appearance-embedding revalidation"

    def _height_above_ground(self) -> float | None:
        """Measured downward range when fresh and unsaturated, else the pose altitude above home.

        ``up_m`` is relative to the home point, so over terrain relief the flat-ground
        assumption is approximate; the payload's downward rangefinder (orientation 25) is
        preferred whenever it is fresh and in range. A reading at or near the sensor's maximum
        (or minimum) is not a measurement - the outdoor payload saturates at 4.0 m - and must
        fall back to the pose altitude instead of reporting the saturation as the height.
        """
        telemetry = self._telemetry()
        now = time.monotonic()
        ground = getattr(telemetry, "ground_distance", None)
        if ground is not None:
            updated = self._value(ground, "updated_monotonic")
            distance = self._value(ground, "distance_m")
            minimum = self._value(ground, "min_distance_m")
            maximum = self._value(ground, "max_distance_m")
            values = (distance, minimum, maximum)
            if (
                updated is not None
                and now - float(updated) <= self.settings.telemetry_max_age_s
                and all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    for value in values
                )
            ):
                distance, minimum, maximum = (float(value) for value in values)
                # A reading at the bound means "at least max" / "at most min", not a height.
                margin = max(0.05, 0.02 * maximum)
                if minimum + margin < distance < maximum - margin:
                    return distance
        pose = getattr(telemetry, "pose", None)
        altitude = None if pose is None else self._value(pose, "up_m")
        if (
            isinstance(altitude, (int, float))
            and not isinstance(altitude, bool)
            and math.isfinite(float(altitude))
            and float(altitude) > 0.0
        ):
            return float(altitude)
        return None

    def _camera_pitch_deg(self) -> float | None:
        """Fresh mount pitch when available, else the configured default.

        Both are earth-frame pitch angles, so the vehicle's own attitude must NOT be added:
        ArduPilot reports the mount/gimbal attitude in the earth frame (MOUNT_ORIENTATION.pitch
        is documented "in global frame" and the modern GIMBAL_DEVICE_ATTITUDE_STATUS carries an
        earth-frame quaternion, with its flags only distinguishing the yaw frame), and
        ``FlightLink.set_gimbal_pitch_yaw`` documents its pitch argument as earth-frame too.
        Copter pitches 5-15 deg nose-down at cruise, so adding it would shift the ground-plane
        range by that much of depression.
        """
        telemetry = self._telemetry()
        now = time.monotonic()
        mount = getattr(telemetry, "mount_status", None)
        if mount is not None:
            updated = getattr(mount, "updated_monotonic", None)
            pitch = getattr(mount, "pitch_deg", None)
            if (
                updated is not None
                and pitch is not None
                and now - float(updated) <= self.settings.telemetry_max_age_s
                and math.isfinite(float(pitch))
            ):
                return float(pitch)
        return float(self.settings.default_gimbal_pitch_deg)

    def _target_ray_depression_deg(self, box: Any, candidate: Any) -> float | None:
        """Depression of the ray through the box centre in the vehicle frame, or None.

        Needs only the camera pitch, the frame height and the box row, so it also gates the
        forward ToF where the ground-plane estimate itself is unavailable.
        """
        frame_height = self._value(candidate, "frame_height")
        if frame_height is None or box is None:
            return None
        try:
            image_height = float(frame_height)
            image_width = float(self._value(candidate, "frame_width", 640.0))
        except (TypeError, ValueError, OverflowError):
            return None
        if image_height <= 0.0 or image_width <= 0.0:
            return None
        pitch_deg = self._camera_pitch_deg()
        if pitch_deg is None:
            return None
        if isinstance(box, Mapping):
            y1 = float(box.get("y1", box.get("top")))
            y2 = float(box.get("y2", box.get("bottom")))
        else:
            y1, y2 = float(box[1]), float(box[3])
        if not all(math.isfinite(value) for value in (y1, y2)):
            return None
        hfov = math.radians(float(self._value(
            candidate, "horizontal_fov_deg", self.settings.camera_horizontal_fov_deg
        )))
        focal_px = image_width / (2.0 * math.tan(hfov / 2.0))
        theta = math.radians(-pitch_deg)
        vertical = ((y1 + y2) * 0.5 - image_height * 0.5) / focal_px
        down = vertical * math.cos(theta) + math.sin(theta)
        north = math.cos(theta) - vertical * math.sin(theta)
        if abs(north) < 1e-9 and abs(down) < 1e-9:
            return None
        return math.degrees(math.atan2(down, abs(north)))

    def _forward_tof_applies(self, box: Any, candidate: Any) -> bool:
        """Whether the single-ray forward lidar can measure this target at all.

        The payload's forward lidars are mounted level with the airframe and have a zero vertical
        field of view, so they return the first surface on the vehicle's horizontal plane at
        flight altitude: indoors (level gimbal, vehicle near person height) that is the target,
        outdoors at -45 deg the beam passes over the person and measures a trunk or building
        behind it. Without the frame geometry to place the ray the historical bearing-only gate
        is kept.
        """
        depression = self._target_ray_depression_deg(box, candidate)
        if depression is None:
            return True
        return abs(depression) <= self.settings.forward_tof_vertical_half_fov_deg

    def _ground_plane_geometry(
        self, box: Any, candidate: Any
    ) -> tuple[float, float, float, float, float] | None:
        """(camera-to-target slant, ground bearing, horizontal, height, pitch) from the ground plane.

        The ray through the box's ground-contact row (the bottom edge for an upright target, the
        centre row for a prone target, i.e. a box wider than it is tall) is intersected with the
        ground plane at the measured height above ground. This is posture-agnostic: on 52 logged
        campaign boxes it tracks the true camera-to-torso slant to a 1% median error, while the
        configured person height (blind to the camera pitch's foreshortening) is +24% median and
        +65% at p90. Refuses when the ray is too shallow (a small pitch error dominates) or when
        the height, frame height or camera pitch is unknown.
        """
        frame_height = self._value(candidate, "frame_height")
        if frame_height is None:
            return None
        try:
            image_height = float(frame_height)
            image_width = float(self._value(candidate, "frame_width", 640.0))
        except (TypeError, ValueError, OverflowError):
            return None
        if image_height <= 0.0 or image_width <= 0.0:
            return None
        height_above_ground = self._height_above_ground()
        pitch_deg = self._camera_pitch_deg()
        if height_above_ground is None or pitch_deg is None:
            return None
        if isinstance(box, Mapping):
            x1 = float(box.get("x1", box.get("left")))
            y1 = float(box.get("y1", box.get("top")))
            x2 = float(box.get("x2", box.get("right")))
            y2 = float(box.get("y2", box.get("bottom")))
        else:
            x1, y1, x2, y2 = (float(value) for value in box)
        if not all(math.isfinite(value) for value in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
            return None
        hfov = math.radians(float(self._value(
            candidate, "horizontal_fov_deg", self.settings.camera_horizontal_fov_deg
        )))
        focal_px = image_width / (2.0 * math.tan(hfov / 2.0))
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

        contact = ray((y1 + y2) * 0.5 if prone else y2)
        north, east, down = contact
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
        return slant, math.atan2(east, north), horizontal, height_above_ground, pitch_deg

    def _forward_proximity_range(self) -> float | None:
        telemetry = self._telemetry()
        now = time.monotonic()
        for sensor in getattr(telemetry, "proximity", ()):
            if self._value(sensor, "orientation") != 0:
                continue
            updated = self._value(sensor, "updated_monotonic")
            # The link keeps the last in-range reading; a stalled range bridge must not supply it.
            if updated is None or now - float(updated) > self.settings.telemetry_max_age_s:
                continue
            try:
                distance = float(self._value(sensor, "distance_m"))
                minimum = float(self._value(sensor, "min_distance_m"))
                maximum = float(self._value(sensor, "max_distance_m"))
            except (TypeError, ValueError, OverflowError):
                continue
            if (
                math.isfinite(distance)
                and math.isfinite(minimum)
                and math.isfinite(maximum)
                and minimum <= distance < maximum
            ):
                return distance + 0.24
        return None

    def _target_step(self, task: SkillTask) -> bool:
        target_id = task.arguments["id"]
        target_size_m = task.executed_target.get("target_size_m")
        try:
            geometry = self._target_geometry(target_id, target_size_m)
        except _TargetNotVisible as exc:
            # approach/follow/orbit all treat a missing or unvalidated frame as a hold: command
            # zero velocity and let the stored reference re-identify the target, exactly like
            # follow. One dropped frame never ends the task; the hold lasts only as long as the
            # task TTL, so a target that never returns expires like any other follow loss.
            if task.skill not in ("approach", "follow", "orbit"):
                raise
            if task._target_lost_at is None:
                task._target_lost_at = time.monotonic()
                task._target_lost_at_sim_s = self._sim_time_s()
                logger.info("%s target %s lost: %s", task.skill, target_id, exc)
                self._publish_event(
                    "target_lost",
                    f"{task.skill.capitalize()} target lost: {exc}",
                    task_id=task.task_id,
                    reference_id=task.tracking["reference_id"],
                )
            match = self._reidentify_target(task.tracking["reference_id"])
            if match is None:
                logger.debug(
                    "%s target %s not visible and not re-identified; holding", task.skill, target_id
                )
                self._command_zero_velocity()
                return False
            target_id, similarity, threshold = match
            try:
                geometry = self._target_geometry(target_id, target_size_m)
            except _TargetNotVisible:
                self._command_zero_velocity()
                return False
            task.arguments["id"] = target_id
            task.executed_target["id"] = target_id
            self._record_reacquisition(task, target_id, similarity, threshold)
        else:
            if task._target_lost_at is not None:
                match = self._reidentify_target(task.tracking["reference_id"])
                if match is None:
                    self._command_zero_velocity()
                    return False
                target_id, similarity, threshold = match
                if target_id != task.arguments["id"]:
                    geometry = self._target_geometry(target_id, target_size_m)
                task.arguments["id"] = target_id
                task.executed_target["id"] = target_id
                self._record_reacquisition(task, target_id, similarity, threshold)

        bearing, distance, range_source, range_note = (
            geometry.bearing_rad, geometry.range_m, geometry.range_source, geometry.range_note
        )
        task.tracking.update({"range_m": distance, "range_source": range_source})
        if range_note is not None:
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
        else:
            task.tracking.pop("range_source_note", None)
        telemetry = self._telemetry()
        # Target motion rotates the camera bearing by the vehicle heading; both must be current.
        self._require_fresh(telemetry, "pose_updated_monotonic", "pose")
        self._require_fresh(telemetry, "heading_updated_monotonic", "heading")
        heading_deg = float(getattr(telemetry, "heading_deg", 0.0) or 0.0)
        heading = math.radians(heading_deg) + bearing
        facing_deg = math.degrees(heading) % 360.0
        logger.debug(
            "%s target=%s bearing_deg=%.1f range_m=%s source=%s facing_deg=%.1f",
            task.skill, task.arguments["id"], math.degrees(bearing),
            "none" if distance is None else f"{distance:.2f}", range_source, facing_deg,
        )
        if task.skill == "follow":
            desired = float(task.executed_target["dist_m"])
            if distance is None:
                # Safe visual following: turn toward the target's bearing without translating
                # until a calibrated range estimate is available.
                self._command(self.flight.set_velocity_ned, 0.0, 0.0, 0.0, yaw_deg=facing_deg)
                return False
            self._range_velocity(heading, distance - desired, yaw_deg=facing_deg)
            return False
        if task.skill in ("approach", "orbit") and distance is None:
            if abs(bearing) > math.radians(8.0):
                self._command(self.flight.set_velocity_ned, 0.0, 0.0, 0.0, yaw_deg=facing_deg)
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
            if distance <= desired + self.settings.arrival_tolerance_m:
                self._command(self.flight.set_velocity_ned, 0.0, 0.0, 0.0, yaw_deg=facing_deg)
                return True
            self._range_velocity(heading, distance - desired, yaw_deg=facing_deg)
            return False
        if task.skill == "orbit":
            radius = float(task.executed_target["radius_m"])
            self._orbit_velocity(heading, distance, radius, yaw_deg=facing_deg)
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
        self._publish_event(
            "target_reacquired",
            f"{task.skill.capitalize()} target reacquired",
            task_id=task.task_id,
            reference_id=reference_id,
            matched_id=candidate_id,
            similarity=similarity,
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
        self._command(
            self.flight.set_velocity_ned,
            direction * speed * math.cos(heading),
            direction * speed * math.sin(heading),
            0.0,
            yaw_deg=yaw_deg,
        )

    def _orbit_velocity(
        self, bearing: float, distance: float, radius: float, *, yaw_deg: float | None = None
    ) -> None:
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
        # Face the target while circling, like follow and approach: without the yaw the vehicle
        # translates sideways out of its own camera's field of view, the detector loses the
        # target, and the orbit ratchets between circling and holding (Koko orbit trials: the
        # vehicle held one circle point with a -0.5 deg heading sweep and the target never
        # gained a second reference view).
        self._command(self.flight.set_velocity_ned, north, east, 0.0, yaw_deg=yaw_deg)

    def _command_zero_velocity(self) -> None:
        self._command(self.flight.set_velocity_ned, 0.0, 0.0, 0.0)

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
                with self._lock:
                    self._require_current_tick()
                    self._precision_unavailable = True

    def _reset_precision_watch(self) -> None:
        """Forget the in-progress descent window and tag setpoint; call when precision control ends."""
        self._precision_stall_started_monotonic = None
        self._precision_stall_start_altitude_m = None
        self._precision_stall_start_sim_s = None
        self._precision_target_monotonic = None
        self._precision_target_velocity = None

    def _precision_descent_watch(self, current: Pose, armed: bool) -> None:
        """Hand back to autopilot LAND when precision control stops making descent progress.

        While precision landing is active HORUS owns the descent with GUIDED velocity setpoints,
        so an autopilot precision-landing failsafe (or a lost tag) that ignores those setpoints
        leaves an armed vehicle hovering above the pad indefinitely: no autopilot failsafe runs in
        GUIDED. The window is wall time (a bounded response) but the required progress is scaled
        by the autopilot clock when it is fresh, because the commanded descent is in autopilot
        metres per autopilot second: at sim RTF 0.18 a legitimate 0.15 m/s descent is only
        0.027 m/s of wall time, which a wall-time rate check would misread as a stall. Without a
        fresh vehicle clock the wall-time rate is used. On a stall the same LAND hand-off as the
        low-altitude hand-off takes over and the stall is reported (event, stats, log).
        """
        if not self._precision_active or not armed:
            return
        now = time.monotonic()
        if self._precision_stall_started_monotonic is None:
            self._precision_stall_started_monotonic = now
            self._precision_stall_start_altitude_m = current.up_m
            self._precision_stall_start_sim_s = self._fresh_sim_time_s()
            return
        elapsed = now - self._precision_stall_started_monotonic
        if elapsed < self.settings.precision_landing_stall_s:
            return
        descended_m = self._precision_stall_start_altitude_m - current.up_m
        now_sim = self._fresh_sim_time_s()
        if (
            now_sim is not None
            and self._precision_stall_start_sim_s is not None
            and now_sim > self._precision_stall_start_sim_s
        ):
            progress_basis_s = now_sim - self._precision_stall_start_sim_s
            progress_basis = "autopilot_time_boot"
        else:
            progress_basis_s = elapsed
            progress_basis = "wall"
        required_m = self.settings.precision_landing_min_descent_m_s * progress_basis_s
        if descended_m >= required_m:
            # Meaningful progress: start a new window from the current altitude.
            self._precision_stall_started_monotonic = now
            self._precision_stall_start_altitude_m = current.up_m
            self._precision_stall_start_sim_s = now_sim
            return
        descent_m_s = descended_m / progress_basis_s
        summary = (
            f"precision landing made no descent progress for {elapsed:.1f}s wall "
            f"({descended_m:.2f} m over {progress_basis_s:.1f}s of {progress_basis}); "
            "falling back to LAND"
        )
        logger.warning(
            "precision landing stalled at %.2f m: %.2f m of descent in %.1fs of %s "
            "(%.3f m/s < %.2f m/s); falling back to LAND",
            current.up_m, descended_m, progress_basis_s, progress_basis, descent_m_s,
            self.settings.precision_landing_min_descent_m_s,
        )
        self._command_zero_velocity()
        self._command(self.flight.land)
        with self._lock:
            self._require_current_tick()
            self._precision_stats["last_fallback"] = summary
            self._precision_action = None
            self._precision_active = False
            self._precision_gimbal_requested_at = None
            self._precision_unavailable = True
            self._auto_rth_after_hold = False
            self._holding_since = None
            self._state = ExecutorState.IDLE
            self._reset_precision_watch()
        self._publish_event(
            "precision_landing_stalled",
            summary,
            altitude_m=current.up_m,
            elapsed_s=elapsed,
            descent_m=descended_m,
            descent_rate_m_s=descent_m_s,
            progress_basis=progress_basis,
        )

    def _fresh_sim_time_s(self) -> float | None:
        """Autopilot time in seconds, or ``None`` when the SYSTEM_TIME sample is stale/absent."""
        telemetry = self._telemetry()
        updated = getattr(telemetry, "time_boot_updated_monotonic", None)
        if updated is None:
            return None
        age = time.monotonic() - float(updated)
        if not math.isfinite(age) or age > self.settings.telemetry_max_age_s:
            return None
        return self._sim_time_s()

    def _log_precision_control(
        self,
        telemetry: Any,
        current: Pose,
        detection: Any,
        velocity: tuple[float, float, float],
    ) -> None:
        """Log tag pixel error, gimbal pitch and commanded velocity, at most once a second."""
        now = time.monotonic()
        if now - self._precision_last_log_monotonic < 1.0:
            return
        self._precision_last_log_monotonic = now
        mount = getattr(telemetry, "mount_status", None)
        gimbal_pitch = None if mount is None else getattr(mount, "pitch_deg", None)
        logger.info(
            "precision landing: tag_px=(%.1f,%.1f) error_px=(%.1f,%.1f) gimbal_pitch_deg=%s "
            "commanded_ned=(%.2f,%.2f,%.2f) altitude_m=%.2f",
            detection.center_x,
            detection.center_y,
            detection.center_x - detection.image_width * 0.5,
            detection.center_y - detection.image_height * 0.5,
            gimbal_pitch,
            velocity[0],
            velocity[1],
            velocity[2],
            current.up_m,
        )

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
        if not math.isfinite(age) or age > self.settings.telemetry_max_age_s:
            if self._precision_active:
                self._precision_controller_failed()
            return
        armed = getattr(telemetry, "armed", None)
        if armed is False:
            self._precision_landing_complete()
            return
        self._precision_descent_watch(current, armed is True)
        if self._precision_active and current.up_m <= self.PRECISION_HANDOFF_ALTITUDE_M:
            self._precision_stats["completed_landings"] += 1
            logger.info("precision landing reached hand-off altitude; handing touchdown to LAND")
            self._command_zero_velocity()
            self._command(self.flight.land)
            self._precision_landing_complete()
            return
        home_distance = math.hypot(current.north_m, current.east_m)
        if home_distance > self.PRECISION_HOME_RADIUS_M:
            if self._precision_active:
                self._command(getattr(self.flight, action))
                self._precision_active = False
                self._reset_precision_watch()
            return
        if self._precision_unavailable:
            return

        frame = self._precision_frame()
        if frame is None:
            self._precision_hold_or_coast("no fresh frame")
            return
        if self._precision_gimbal_requested_at is None:
            set_gimbal = getattr(self.flight, "set_gimbal_pitch_yaw", None)
            if not callable(set_gimbal):
                self._precision_stats["last_fallback"] = "no gimbal control"
                with self._lock:
                    self._require_current_tick()
                    self._precision_unavailable = True
                return
            self._command(set_gimbal, -90.0, 0.0)
            with self._lock:
                self._require_current_tick()
                self._precision_gimbal_requested_at = time.monotonic()
            return
        if frame.timestamp < self._precision_gimbal_requested_at + self.PRECISION_GIMBAL_SETTLE_S:
            if self._precision_active:
                self._command_zero_velocity()
            return

        heading = getattr(telemetry, "heading_deg", None)
        heading_updated = getattr(telemetry, "heading_updated_monotonic", None)
        if (
            heading is None
            or not math.isfinite(float(heading))
            or heading_updated is None
            or time.monotonic() - float(heading_updated) > self.settings.telemetry_max_age_s
        ):
            # The tag offset is rotated into NED by the heading; never steer on a stale one.
            if self._precision_active:
                self._command_zero_velocity()
            return
        detection, velocity = self._precision_controller.observe(frame.image, float(heading))
        if detection is None:
            self._precision_hold_or_coast("tag not detected in the current frame")
            return
        self._precision_stats["tag_detections"] += 1
        if not self._precision_active:
            self._command(self.flight.set_guided)
            with self._lock:
                self._require_current_tick()
                self._precision_active = True
                self._reset_precision_watch()
            self._precision_stats["acquisitions"] += 1
            logger.info("precision landing acquired AprilTag at %.2f m from home, %.2f m up",
                        home_distance, current.up_m)
        self._precision_target_monotonic = time.monotonic()
        self._precision_target_velocity = (float(velocity[0]), float(velocity[1]), float(velocity[2]))
        self._command(self.flight.set_velocity_ned, *self._precision_target_velocity)
        self._log_precision_control(telemetry, current, detection, self._precision_target_velocity)

    def _precision_hold_or_coast(self, reason: str) -> None:
        """Keep the last validated tag setpoint briefly, otherwise hold at zero velocity.

        The AprilTag detector succeeds on only a fraction of frames at range (the campaign saw
        ~16% of ticks with a detection). Re-deriving the setpoint from scratch every tick and
        commanding zero on each miss pulsed the descent (0.15 m/s and 0 on alternating ticks),
        which the autopilot's velocity controller averages into a near-hover: the vehicle stalled
        at 24.8 m and 9.6 m while the instrumentation showed a commanded 0.15 m/s descent. A
        bounded grace keeps the last centring/descent setpoint alive across detection gaps; a
        sustained loss (or no fresh frame) still holds, and the stall watchdog remains the
        last-resort hand-off to LAND.
        """
        if not self._precision_active:
            return
        now = time.monotonic()
        last = self._precision_target_monotonic
        velocity = self._precision_target_velocity
        if (
            last is not None
            and velocity is not None
            and now - last <= self.settings.precision_landing_target_grace_s
        ):
            logger.debug(
                "precision landing: %s; coasting the last tag setpoint (age %.2fs)",
                reason, now - last,
            )
            self._command(self.flight.set_velocity_ned, *velocity)
            return
        self._command_zero_velocity()
        if last is not None:
            logger.debug(
                "precision landing: %s; tag setpoint expired after %.2fs, holding",
                reason, now - last,
            )

    def _set_default_gimbal_pitch(self) -> None:
        set_gimbal = getattr(self.flight, "set_gimbal_pitch_yaw", None)
        if callable(set_gimbal):
            try:
                set_gimbal(self.settings.default_gimbal_pitch_deg, 0.0)
            except Exception as exc:  # camera aiming must never block flight
                logger.warning("could not set gimbal pitch: %s", exc)

    def _precision_landing_complete(self) -> None:
        if self._precision_gimbal_requested_at is not None:
            self._command(self._set_default_gimbal_pitch)
        with self._lock:
            self._require_current_tick()
            self._precision_action = None
            self._precision_active = False
            self._precision_gimbal_requested_at = None
            self._precision_unavailable = False
            self._auto_rth_after_hold = False
            self._holding_since = None
            self._state = ExecutorState.IDLE
            self._reset_precision_watch()

    def _precision_controller_failed(self) -> None:
        """Hand a failed precision descent back to LAND instead of hovering armed in GUIDED.

        Only called while precision control owns the vehicle (an acquisition happened). A stale
        pose sample or a detector/gimbal error used to leave the state HOLDING in GUIDED with no
        automatic way out: the campaign's forest pre-follow RTH ended at 11 m above the pad,
        armed, mode GUIDED, pose 1.9 s stale, and never disarmed. LAND takes over exactly like
        the stall watchdog hand-off: the autopilot owns the descent and its own failsafes.
        """
        summary = "precision landing lost control (stale pose or error); falling back to LAND"
        logger.warning("precision landing lost control; falling back to LAND")
        if self._precision_stats["last_fallback"] is None:
            self._precision_stats["last_fallback"] = "controller failed (stale pose or error)"
        self._command_zero_velocity()
        self._command(self.flight.land)
        with self._lock:
            self._require_current_tick()
            self._precision_action = None
            self._precision_active = False
            self._precision_gimbal_requested_at = None
            self._precision_unavailable = True
            self._auto_rth_after_hold = False
            self._holding_since = None
            self._state = ExecutorState.IDLE
            self._reset_precision_watch()
        self._publish_event("precision_landing_stalled", summary)

    def _safe_hold(self) -> None:
        self._command(self._flight_hold)

    def _flight_hold(self) -> None:
        """GUIDED zero-velocity hold sent directly; callers own command ordering."""
        try:
            self.flight.hold()
        except AttributeError:
            self.flight.set_guided()
            self.flight.set_velocity_ned(0.0, 0.0, 0.0)

    def _publish_task_finished(self, task: SkillTask) -> None:
        if task._finished_event_published:
            return
        task._finished_event_published = True
        self._publish_event(
            "task_finished",
            f"{task.skill} task {task.status}",
            task_id=task.task_id,
            skill=task.skill,
            status=task.status,
            error=task.error,
        )

    def _finish(self, task: SkillTask, status: str, error: str | None) -> None:
        if task.skill in ("scan", "search"):
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
        if task.skill in _TRACKING_SKILLS:
            task.result["tracking"] = dict(task.tracking)
        if self._active_id == task.task_id:
            self._active_id = None
        if status in ("succeeded", "cancelled", "expired", "failed"):
            self._holding_since = time.monotonic()
            self._auto_rth_after_hold = status in ("succeeded", "cancelled", "expired", "failed")
            self._state = ExecutorState.HOLDING
            self._publish_task_finished(task)

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
        if task.skill in _TRACKING_SKILLS:
            result["tracking"] = dict(task.tracking)
        return result

    def get_task_status(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                task = self._tasks[task_id]
            except KeyError as exc:
                raise KeyError(f"unknown task_id {task_id!r}") from exc
            return self._task_dict(task)

    def note_supervisor_contact(self) -> None:
        """Record supervisory-link activity; idle-hold auto-RTH counts from the last contact."""
        self._last_supervisor_contact = time.monotonic()

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
                if task.skill in _TRACKING_SKILLS:
                    task.result["tracking"] = dict(task.tracking)
                self._publish_task_finished(task)
            return self._task_dict(task)

    def _emergency(
        self,
        action: str,
        *,
        source: str | None = "executor",
        expected_epoch: int | None = None,
    ) -> dict[str, Any] | None:
        """Preempt all work and command ``action``; ``expected_epoch`` makes it conditional.

        The epoch bump and the autopilot command happen under one _command_lock hold, so any worker
        command validated against the old epoch was sent before this one and cannot follow it.
        """
        preempted: list[SkillTask] = []
        with self._command_lock:
            with self._lock:
                if expected_epoch is not None and expected_epoch != self._emergency_epoch:
                    return None
                self._emergency_epoch += 1
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
                        if task.skill in _TRACKING_SKILLS:
                            task.result["tracking"] = dict(task.tracking)
                        preempted.append(task)
                self._active_id = None
                self._queue.clear()
                self._holding_since = time.monotonic()
                self._auto_rth_after_hold = action == "hold"
                restore_default_pitch = (
                    action == "hold" and self._precision_gimbal_requested_at is not None
                )
                self._precision_action = action if action in ("rth", "land") else None
                self._precision_active = False
                self._precision_gimbal_requested_at = None
                self._precision_unavailable = False
                self._reset_precision_watch()
                state = ExecutorState.RTH if action == "rth" else ExecutorState.HOLDING
                self._state = state
            if action == "hold":
                self._flight_hold()
                if restore_default_pitch:
                    self._set_default_gimbal_pitch()
            elif action == "rth":
                self.flight.rth()
            elif action == "land":
                self.flight.land()
        for task in preempted:
            self._publish_task_finished(task)
        if source is not None:
            self._publish_event(
                "emergency",
                f"Emergency {action} requested",
                action=action,
                source=source,
            )
        return {"action": action, "state": state.value, "preempted": True}

    def hold(self, *, source: str | None = "executor") -> dict[str, Any]:
        return self._emergency("hold", source=source)

    def rth(self, *, source: str | None = "executor") -> dict[str, Any]:
        return self._emergency("rth", source=source)

    def land(self, *, source: str | None = "executor") -> dict[str, Any]:
        return self._emergency("land", source=source)

