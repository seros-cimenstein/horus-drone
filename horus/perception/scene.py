"""Camera-relative scene intelligence over processed tracker candidates.

``SceneMonitor`` consumes the candidate boxes of every processed frame and maintains two
bounded views: a short per-track image-motion history and named image-region watches that
emit enter/exit/dwell transitions per tracker ID. It is pure standard-library geometry: it
never runs detectors, embeddings or models, and it is agnostic to detector/grounder classes.

Coordinates are fractions of the processed frame (x right, y down, 0..1). Everything is
image-relative. Image motion mixes target motion with camera ego-motion and is never a
metric or ground speed; each motion report classifies the measured camera movement and lists
camera components it cannot measure (e.g. zoom without field-of-view telemetry; even a
"within_tolerance" camera is not proven still). A region watch is a fixed-camera watch: it
records the camera state (position, heading, gimbal attitude, optional vehicle attitude and
field of view, frame size) when created and only produces transitions
while the camera stays within tolerance of that reference. When the camera moves, its state
is unavailable/stale, or the frame geometry changes, the watch pauses; after the camera is
back and steady it resynchronizes occupancy without inventing transitions for the gap.

Clock contract: frame timestamps are monotonic seconds from the same clock as ``clock``
(``time.monotonic`` for camera frames). Motion rates and watch timers use frame timestamps;
staleness compares the newest frame timestamp with ``clock()``. Watch timers advance only by
observed inter-frame intervals; a longer interval than ``STREAM_GAP_S`` or a clock reset is a
stream discontinuity that advances nothing and resynchronizes every watch.

Frame pose contract (``observe(pose=...)``): a mapping with ``north_m``, ``east_m``,
``up_m`` and ``pose_age_s``; ``heading_deg`` and ``heading_age_s``; ``gimbal_pitch_deg``,
``gimbal_yaw_deg`` and ``gimbal_age_s``; optional ``gimbal_roll_deg`` (same sample as the gimbal
group), ``vehicle_roll_deg``/``vehicle_pitch_deg`` with ``attitude_age_s``, and
``camera_hfov_deg``. Ages are measured when the frame was
captured. A missing or older-than-``CAMERA_STATE_MAX_AGE_S`` required field (or a reported
attitude going missing/stale) makes the camera state unavailable; it is never assumed
stationary. Unreported optional components are listed as unmeasured, never assumed unchanged.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
import json
import math
import threading
import time
from typing import Any, NamedTuple

# Motion history: samples inside this window (ending at a track's newest sample) are kept.
MOTION_WINDOW_S = 2.0
MAX_SAMPLES_PER_TRACK = 32
MIN_MOTION_SAMPLES = 3
MIN_MOTION_SPAN_S = 0.25
# Tracks unseen this long (frame time) are forgotten; at most MAX_TRACKS histories are kept.
TRACK_RETENTION_S = 10.0
MAX_TRACKS = 256
# The newest processed frame is stale when older than this (wall monotonic clock).
STALE_AFTER_S = 1.0
# Inter-frame intervals longer than this are stream discontinuities: no timer crosses them.
STREAM_GAP_S = 1.0
# Motion fit confidence halves when the centre fit residual reaches this fraction of the frame.
RESIDUAL_HALF_CONFIDENCE = 0.02

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
# A paused watch resumes only after the camera matched its reference this long.
WATCH_RESUME_STABLE_S = 1.0
# Frame timestamps are floats; thresholds tolerate accumulated rounding of this size.
_TIME_EPSILON_S = 1e-6

MAX_WATCHES = 16
MAX_OCCUPANTS_PER_WATCH = 64
# Per-watch event token bucket in frame time: bursts of EVENT_BURST, then EVENT_REFILL_PER_S.
EVENT_BURST = 10
EVENT_REFILL_PER_S = 2.0
MAX_ABSENCE_S = 30.0
MAX_DWELL_S = 3600.0
MAX_TTL_S = 86400.0
MAX_TEXT_LENGTH = 64

REGION_EVENT_KINDS = (
    "region_enter",
    "region_exit",
    "region_dwell",
    "region_watch_paused",
    "region_watch_resumed",
    "region_watch_expired",
)

EventSink = Callable[..., Any]
_Entry = tuple[int, str, str, float, float]


def quote_label(label: Any) -> str:
    """JSON-quote a detector/grounder label (bounded) so it is always data, never instruction."""
    return json.dumps(str(label)[:MAX_TEXT_LENGTH], ensure_ascii=False)


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
        # Same MOUNT_STATUS sample (and age) as gimbal pitch/yaw.
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


class _Sample(NamedTuple):
    timestamp: float
    x: float
    y: float
    width: float
    height: float
    camera: CameraState | None
    camera_reason: str | None


class _TrackHistory:
    __slots__ = ("samples", "class_name", "last_frame_id", "last_timestamp", "epoch")

    def __init__(self, epoch: int) -> None:
        self.samples: deque[_Sample] = deque(maxlen=MAX_SAMPLES_PER_TRACK)
        self.class_name = ""
        self.last_frame_id = 0
        self.last_timestamp = 0.0
        self.epoch = epoch


class _Occupant:
    __slots__ = (
        "target_id",
        "class_name",
        "center",
        "visible",
        "occupied_s",
        "dwell_clock_s",
        "missing_since",
        "dwell_reported",
        "initial",
    )

    def __init__(self, target_id: int, class_name: str, center: tuple[float, float], initial: bool) -> None:
        self.target_id = target_id
        self.class_name = class_name
        self.center = center
        self.visible = True
        # Observed time from entry (or resync) through the latest frame; stalls excluded.
        self.occupied_s = 0.0
        # Observed time inside between consecutive frames that both saw the target inside.
        self.dwell_clock_s = 0.0
        # Frame timestamp of the first frame that no longer saw the target inside.
        self.missing_since: float | None = None
        self.dwell_reported = False
        self.initial = initial


class _Watch:
    __slots__ = (
        "watch_id",
        "name",
        "box",
        "class_name",
        "class_fold",
        "dwell_s",
        "absence_s",
        "ttl_s",
        "created_at",
        "reference",
        "reference_size",
        "reference_frame_id",
        "paused",
        "pause_reason",
        "pause_detail",
        "settle_since",
        "occupants",
        "tokens",
        "pending_suppressed",
        "suppressed_total",
        "events_emitted",
        "overflow_skipped",
        "occupancy_frame_id",
    )

    def __init__(
        self,
        watch_id: str,
        name: str,
        box: tuple[float, float, float, float],
        class_name: str | None,
        dwell_s: float | None,
        absence_s: float,
        ttl_s: float | None,
        created_at: float,
        reference: CameraState,
        reference_size: tuple[int, int],
        reference_frame_id: int,
    ) -> None:
        self.watch_id = watch_id
        self.name = name
        self.box = box
        self.class_name = class_name
        self.class_fold = class_name.casefold() if class_name is not None else None
        self.dwell_s = dwell_s
        self.absence_s = absence_s
        self.ttl_s = ttl_s
        self.created_at = created_at
        self.reference = reference
        self.reference_size = reference_size
        self.reference_frame_id = reference_frame_id
        self.paused = False
        self.pause_reason: str | None = None
        self.pause_detail: dict[str, Any] | None = None
        self.settle_since: float | None = None
        self.occupants: dict[int, _Occupant] = {}
        self.tokens = float(EVENT_BURST)
        self.pending_suppressed = 0
        self.suppressed_total = 0
        self.events_emitted = 0
        self.overflow_skipped = 0
        self.occupancy_frame_id = reference_frame_id

    def contains(self, entry: _Entry) -> bool:
        _, _, class_fold, x, y = entry
        if self.class_fold is not None and class_fold != self.class_fold:
            return False
        x1, y1, x2, y2 = self.box
        return x1 <= x <= x2 and y1 <= y <= y2

    def reseed(self, entries: Sequence[_Entry]) -> None:
        """Replace occupancy by targets inside now, as initial occupants without enter events."""
        self.occupants = {}
        for entry in entries:
            if not self.contains(entry):
                continue
            if len(self.occupants) >= MAX_OCCUPANTS_PER_WATCH:
                self.overflow_skipped += 1
                continue
            target_id, class_name, _, x, y = entry
            self.occupants[target_id] = _Occupant(target_id, class_name, (x, y), initial=True)


class SceneMonitor:
    """Bounded camera-relative motion history and fixed-camera region watches.

    ``event_sink(kind, summary, **data)`` receives region events (for example
    ``EventBus.publish``). It is called while the monitor lock is held, so once
    ``remove_region_watch`` returns no event from that watch can follow.
    """

    def __init__(
        self,
        event_sink: EventSink | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sink = event_sink
        self._clock = clock
        self._lock = threading.Lock()
        self._tracks: OrderedDict[int, _TrackHistory] = OrderedDict()
        self._watches: dict[str, _Watch] = {}
        self._next_watch = 1
        self._frame_id: int | None = None
        self._timestamp: float | None = None
        self._frame_size: tuple[int, int] | None = None
        self._camera: CameraState | None = None
        self._camera_reason: str | None = "no_frames"
        self._entries: tuple[_Entry, ...] = ()
        # Motion samples are comparable only within one epoch; a discontinuity starts a new one.
        self._epoch = 0
        self._discontinuity: dict[str, Any] | None = None

    def observe(
        self,
        frame_id: int,
        timestamp: float,
        width: int,
        height: int,
        candidates: Sequence[Any],
        pose: Any = None,
    ) -> None:
        """Consume one processed frame's visible candidates (objects with ``id/bbox/class_name``)."""
        width, height = int(width), int(height)
        timestamp = float(timestamp)
        if width <= 0 or height <= 0 or not math.isfinite(timestamp):
            return
        camera, camera_reason = camera_state_from_pose(pose)
        with self._lock:
            previous = self._timestamp
            discontinuity = None
            gap_s = None
            if previous is not None:
                if timestamp < previous:
                    discontinuity = "time_reset"
                elif timestamp - previous > STREAM_GAP_S:
                    discontinuity = "stream_gap"
                    gap_s = timestamp - previous
                elif self._frame_size != (width, height):
                    discontinuity = "frame_size_changed"
                elif (
                    camera is not None
                    and self._camera is not None
                    and camera.hfov_deg != self._camera.hfov_deg
                    and (
                        camera.hfov_deg is None
                        or self._camera.hfov_deg is None
                        or abs(camera.hfov_deg - self._camera.hfov_deg) > CAMERA_HFOV_TOLERANCE_DEG
                    )
                ):
                    discontinuity = "field_of_view_changed"
            if discontinuity is not None:
                self._epoch += 1
                self._discontinuity = {
                    "reason": discontinuity,
                    "frame_id": int(frame_id),
                    "timestamp": timestamp,
                    "gap_s": None if gap_s is None else round(gap_s, 3),
                }
                if discontinuity == "time_reset":
                    # Timestamps from the old clock would corrupt retention and ages.
                    self._tracks.clear()
            # Timers advance only by observed intervals; nothing advances across a discontinuity.
            dt = 0.0 if previous is None or discontinuity is not None else timestamp - previous
            self._frame_id = int(frame_id)
            self._timestamp = timestamp
            self._frame_size = (width, height)
            self._camera = camera
            self._camera_reason = camera_reason

            entries = []
            for candidate in candidates:
                x1, y1, x2, y2 = candidate.bbox
                x = (x1 + x2) / (2.0 * width)
                y = (y1 + y2) / (2.0 * height)
                target_id = int(candidate.id)
                class_name = str(candidate.class_name)
                entries.append((target_id, class_name, class_name.casefold(), x, y))
                history = self._tracks.get(target_id)
                if history is None:
                    history = self._tracks[target_id] = _TrackHistory(self._epoch)
                    if len(self._tracks) > MAX_TRACKS:
                        self._tracks.popitem(last=False)
                else:
                    self._tracks.move_to_end(target_id)
                samples = history.samples
                if history.epoch != self._epoch:
                    samples.clear()
                    history.epoch = self._epoch
                sample = _Sample(
                    timestamp,
                    x,
                    y,
                    max(0.0, (x2 - x1) / width),
                    max(0.0, (y2 - y1) / height),
                    camera,
                    camera_reason,
                )
                if samples and samples[-1].timestamp == timestamp:
                    samples[-1] = sample
                else:
                    samples.append(sample)
                while samples[0].timestamp < timestamp - MOTION_WINDOW_S:
                    samples.popleft()
                history.class_name = class_name
                history.last_frame_id = self._frame_id
                history.last_timestamp = timestamp
            self._entries = tuple(entries)
            while self._tracks:
                oldest = next(iter(self._tracks.values()))
                if timestamp - oldest.last_timestamp <= TRACK_RETENTION_S:
                    break
                self._tracks.popitem(last=False)

            self._expire_watches()
            for watch in tuple(self._watches.values()):
                self._update_watch(watch, dt, discontinuity)

    def candidate_motion(self, track_id: int) -> dict[str, Any]:
        """Estimate a track's recent normalized image motion or state why it is unavailable."""
        track_id = int(track_id)
        with self._lock:
            now = self._clock()
            result: dict[str, Any] = {
                "id": track_id,
                "available": False,
                "reason": None,
                "reference": "camera_image_normalized",
            }
            if self._timestamp is None:
                result["reason"] = "no_frames"
                return result
            frame_age = max(0.0, now - self._timestamp)
            result["frame_id"] = self._frame_id
            result["frame_age_s"] = round(frame_age, 3)
            result["stream_discontinuity"] = self._discontinuity_state(now)
            history = self._tracks.get(track_id)
            if history is None or not history.samples:
                result["reason"] = "unknown_track"
                return result
            samples = history.samples
            last = samples[-1]
            result["untrusted_detector_label"] = quote_label(history.class_name)
            result["last_seen_frame_id"] = history.last_frame_id
            result["observation_age_s"] = round(max(0.0, now - history.last_timestamp), 3)
            result["center"] = [round(last.x, 4), round(last.y, 4)]
            result["size"] = [round(last.width, 4), round(last.height, 4)]
            if frame_age > STALE_AFTER_S:
                result["reason"] = "stale_frames"
                return result
            if history.last_frame_id != self._frame_id:
                result["reason"] = "not_visible"
                return result
            span = last.timestamp - samples[0].timestamp
            result["samples"] = len(samples)
            result["span_s"] = round(span, 3)
            if len(samples) < MIN_MOTION_SAMPLES or span < MIN_MOTION_SPAN_S:
                result["reason"] = "insufficient_samples"
                result["required"] = {"samples": MIN_MOTION_SAMPLES, "span_s": MIN_MOTION_SPAN_S}
                return result
            result.update(_fit_motion(samples, span))
            result["camera_motion"] = _camera_motion(samples)
            result["available"] = True
            return result

    def watch_region(
        self,
        name: str,
        box: Sequence[float],
        *,
        class_name: str | None = None,
        dwell_s: float | None = None,
        absence_s: float = 1.0,
        ttl_s: float | None = None,
    ) -> dict[str, Any]:
        """Create or replace (same name) a fixed-camera region watch on the latest fresh frame.

        Raises ``ValueError`` for invalid arguments or a full watch table and ``RuntimeError``
        when no fresh frame or no fresh camera state exists to anchor the watch.
        """
        name = _bounded_text(name, "name")
        region = _normalized_box(box)
        if class_name is not None:
            class_name = _bounded_text(class_name, "class_name")
        dwell = None if dwell_s is None else _bounded_seconds(dwell_s, "dwell_s", 0.0, MAX_DWELL_S, True)
        absence = _bounded_seconds(absence_s, "absence_s", 0.0, MAX_ABSENCE_S, False)
        ttl = None if ttl_s is None else _bounded_seconds(ttl_s, "ttl_s", 0.0, MAX_TTL_S, True)
        with self._lock:
            now = self._clock()
            self._expire_watches()
            if self._timestamp is None or now - self._timestamp > STALE_AFTER_S:
                raise RuntimeError("scene unavailable: no fresh processed camera frame to anchor the watch")
            if self._camera is None:
                raise RuntimeError(
                    f"scene unavailable: camera state is {self._camera_reason}; a fixed-camera watch "
                    "needs fresh position, heading and gimbal attitude"
                )
            replaced = next((watch for watch in self._watches.values() if watch.name == name), None)
            if replaced is None and len(self._watches) >= MAX_WATCHES:
                raise ValueError(
                    f"at most {MAX_WATCHES} region watches may be active; remove one with remove_region_watch"
                )
            if replaced is not None:
                # Replacement is a commander decision: the old watch ends silently, without exits.
                del self._watches[replaced.watch_id]
            watch_id = f"watch-{self._next_watch}"
            self._next_watch += 1
            watch = _Watch(
                watch_id,
                name,
                region,
                class_name,
                dwell,
                absence,
                ttl,
                now,
                self._camera,
                self._frame_size,  # type: ignore[arg-type]
                self._frame_id,  # type: ignore[arg-type]
            )
            watch.reseed(self._entries)
            self._watches[watch_id] = watch
            result = self._watch_state(watch, now)
            result["replaced_watch_id"] = replaced.watch_id if replaced is not None else None
            result["initial_occupants"] = sorted(watch.occupants)
            return result

    def list_region_watches(self) -> dict[str, Any]:
        """Return every active watch with parameters, occupancy, timers and stream freshness."""
        with self._lock:
            now = self._clock()
            self._expire_watches()
            return {
                "stream": self._stream_state(now),
                "max_watches": MAX_WATCHES,
                "camera_tolerances": camera_tolerances(),
                "watches": [self._watch_state(watch, now) for watch in self._watches.values()],
            }

    def remove_region_watch(self, watch_id: str) -> dict[str, Any]:
        """Remove a watch; unknown or already expired IDs raise ``LookupError``."""
        key = str(watch_id)
        with self._lock:
            self._expire_watches()
            watch = self._watches.pop(key, None)
            if watch is None:
                raise LookupError(f"unknown region watch {key!r}; active watches: {sorted(self._watches)}")
            return {
                "removed": watch.watch_id,
                "name": watch.name,
                "occupants_at_removal": sorted(watch.occupants),
                "events_emitted": watch.events_emitted,
            }

    def _discontinuity_state(self, now: float) -> dict[str, Any] | None:
        if self._discontinuity is None:
            return None
        state = dict(self._discontinuity)
        state["age_s"] = round(max(0.0, now - state.pop("timestamp")), 3)
        return state

    def _stream_state(self, now: float) -> dict[str, Any]:
        if self._timestamp is None:
            return {
                "status": "waiting_for_frames",
                "frame_id": None,
                "frame_age_s": None,
                "camera_state": None,
                "last_discontinuity": None,
            }
        age = max(0.0, now - self._timestamp)
        return {
            "status": "stale" if age > STALE_AFTER_S else "live",
            "frame_id": self._frame_id,
            "frame_age_s": round(age, 3),
            "camera_state": "available" if self._camera is not None else self._camera_reason,
            "last_discontinuity": self._discontinuity_state(now),
        }

    def _watch_state(self, watch: _Watch, now: float) -> dict[str, Any]:
        age = max(0.0, now - watch.created_at)
        stale = self._timestamp is None or now - self._timestamp > STALE_AFTER_S
        if watch.paused:
            status = "paused"
        elif stale:
            status = "stale"
        else:
            status = "active"
        reference = watch.reference
        return {
            "watch_id": watch.watch_id,
            "name": watch.name,
            "box": list(watch.box),
            "class_filter": watch.class_name,
            "dwell_s": watch.dwell_s,
            "absence_s": watch.absence_s,
            "ttl_s": watch.ttl_s,
            "age_s": round(age, 3),
            "expires_in_s": None if watch.ttl_s is None else round(max(0.0, watch.ttl_s - age), 3),
            # active: occupancy confirmed on the latest fresh frame; stale: last confirmed
            # occupancy, no fresh frame; paused: camera left the reference view, no transitions.
            "status": status,
            "pause_reason": watch.pause_reason,
            "pause_detail": watch.pause_detail,
            "occupancy_as_of_frame_id": watch.occupancy_frame_id,
            "camera_reference": {
                "frame_id": watch.reference_frame_id,
                "frame_size": list(watch.reference_size),
                "north_m": round(reference.north_m, 3),
                "east_m": round(reference.east_m, 3),
                "up_m": round(reference.up_m, 3),
                "heading_deg": round(reference.heading_deg, 2),
                "gimbal_pitch_deg": round(reference.gimbal_pitch_deg, 2),
                "gimbal_yaw_deg": round(reference.gimbal_yaw_deg, 2),
                "hfov_deg": reference.hfov_deg,
                "vehicle_roll_deg": reference.vehicle_roll_deg,
                "vehicle_pitch_deg": reference.vehicle_pitch_deg,
                "gimbal_roll_deg": reference.gimbal_roll_deg,
            },
            "unmeasured_camera_components": unmeasured_camera_components(reference),
            "occupant_count": len(watch.occupants),
            "occupants": [
                {
                    "target_id": occupant.target_id,
                    "untrusted_detector_label": quote_label(occupant.class_name),
                    "state": "inside" if occupant.visible else "pending_exit",
                    "occupied_s": round(occupant.occupied_s, 3),
                    "dwell_clock_s": round(occupant.dwell_clock_s, 3),
                    "missing_s": (
                        0.0
                        if occupant.missing_since is None or self._timestamp is None
                        else round(self._timestamp - occupant.missing_since, 3)
                    ),
                    "dwell_reported": occupant.dwell_reported,
                    "initial": occupant.initial,
                    "center": [round(occupant.center[0], 4), round(occupant.center[1], 4)],
                }
                for occupant in sorted(watch.occupants.values(), key=lambda item: item.target_id)
            ],
            "events_emitted": watch.events_emitted,
            "suppressed_events": watch.suppressed_total,
            "overflow_skipped": watch.overflow_skipped,
        }

    def expire_watches(self) -> None:
        """End watches whose TTL elapsed; call periodically so expiry needs no frames or queries."""
        with self._lock:
            self._expire_watches()

    def _expire_watches(self) -> None:
        now = self._clock()
        for watch in tuple(self._watches.values()):
            if watch.ttl_s is None or now - watch.created_at < watch.ttl_s:
                continue
            del self._watches[watch.watch_id]
            if self._sink is not None:
                self._sink(
                    "region_watch_expired",
                    f"Region watch {quote_label(watch.name)} ({watch.watch_id}) expired after {watch.ttl_s:g} s",
                    watch_id=watch.watch_id,
                    watch_name=watch.name,
                    ttl_s=watch.ttl_s,
                    occupants_at_expiry=sorted(watch.occupants),
                )

    def _update_watch(self, watch: _Watch, dt: float, discontinuity: str | None) -> None:
        watch.tokens = min(float(EVENT_BURST), watch.tokens + dt * EVENT_REFILL_PER_S)
        pause_reason = None
        pause_detail: dict[str, Any] | None = None
        if self._frame_size != watch.reference_size:
            pause_reason = "frame_size_changed"
            pause_detail = {"frame_size": list(self._frame_size or ())}
        elif self._camera is None:
            pause_reason = "camera_state_unavailable"
            pause_detail = {"camera_state": self._camera_reason}
        else:
            deviation, exceeded = camera_deviation(watch.reference, self._camera)
            if exceeded:
                pause_reason = "camera_moved"
                pause_detail = {"deviation": deviation, "exceeded": exceeded}
        if pause_reason is not None:
            watch.settle_since = None
            watch.pause_detail = pause_detail
            if not watch.paused or watch.pause_reason != pause_reason:
                watch.paused = True
                watch.pause_reason = pause_reason
                self._emit_watch(
                    watch,
                    "region_watch_paused",
                    f"Region watch {quote_label(watch.name)} ({watch.watch_id}) paused: "
                    f"{pause_reason.replace('_', ' ')}; no enter/exit/dwell until the camera is back",
                    reason=pause_reason,
                    detail=pause_detail,
                    last_confirmed_occupants=sorted(watch.occupants),
                    last_confirmed_frame_id=watch.occupancy_frame_id,
                )
            return
        if watch.paused:
            if watch.settle_since is None or discontinuity is not None:
                watch.settle_since = self._timestamp
            watch.pause_reason = "camera_settling"
            watch.pause_detail = {"required_stable_s": WATCH_RESUME_STABLE_S}
            if self._timestamp - watch.settle_since < WATCH_RESUME_STABLE_S - _TIME_EPSILON_S:  # type: ignore[operator]
                return
            self._resync(watch, "camera_returned")
            return
        if discontinuity is not None:
            self._resync(watch, discontinuity)
            return
        self._track_occupancy(watch, dt)

    def _resync(self, watch: _Watch, reason: str) -> None:
        """Adopt current occupancy after an unobserved interval without inventing transitions."""
        before = set(watch.occupants)
        watch.reseed(self._entries)
        watch.paused = False
        watch.pause_reason = None
        watch.pause_detail = None
        watch.settle_since = None
        watch.occupancy_frame_id = self._frame_id  # type: ignore[assignment]
        after = set(watch.occupants)
        self._emit_watch(
            watch,
            "region_watch_resumed",
            f"Region watch {quote_label(watch.name)} ({watch.watch_id}) resumed after "
            f"{reason.replace('_', ' ')} with {len(after)} occupant(s); changes during the unobserved "
            "interval are unknown and produce no enter/exit events",
            reason=reason,
            occupants=sorted(after),
            no_longer_inside=sorted(before - after),
            newly_inside=sorted(after - before),
        )

    def _track_occupancy(self, watch: _Watch, dt: float) -> None:
        timestamp = self._timestamp
        watch.occupancy_frame_id = self._frame_id  # type: ignore[assignment]
        inside = {entry[0]: entry for entry in self._entries if watch.contains(entry)}
        present: dict[int, _Entry] | None = None
        exits = []
        dwells = []
        for occupant in watch.occupants.values():
            entry = inside.get(occupant.target_id)
            occupant.occupied_s += dt
            if entry is not None:
                if occupant.visible:
                    occupant.dwell_clock_s += dt
                occupant.visible = True
                occupant.missing_since = None
                occupant.class_name = entry[1]
                occupant.center = (entry[3], entry[4])
                if (
                    watch.dwell_s is not None
                    and not occupant.dwell_reported
                    and occupant.dwell_clock_s >= watch.dwell_s - _TIME_EPSILON_S
                ):
                    occupant.dwell_reported = True
                    dwells.append(occupant)
                continue
            occupant.visible = False
            if occupant.missing_since is None:
                occupant.missing_since = timestamp
            if timestamp - occupant.missing_since >= watch.absence_s - _TIME_EPSILON_S:  # type: ignore[operator]
                if present is None:
                    present = {item[0]: item for item in self._entries}
                seen = present.get(occupant.target_id)
                if seen is not None:
                    occupant.center = (seen[3], seen[4])
                exits.append((occupant, "left_region" if seen is not None else "disappeared"))
        for occupant, reason in sorted(exits, key=lambda item: item[0].target_id):
            del watch.occupants[occupant.target_id]
            missing = timestamp - occupant.missing_since  # type: ignore[operator]
            occupied = max(0.0, occupant.occupied_s - missing)
            region = f"region {quote_label(watch.name)} ({watch.watch_id})"
            if reason == "left_region":
                action = f"left {region}: observed outside after {occupied:.1f} s inside"
            else:
                action = (
                    f"is no longer seen in {region}: track not visible for {missing:.1f} s after "
                    f"{occupied:.1f} s inside (exit not observed)"
                )
            self._emit_target(
                watch,
                "region_exit",
                occupant,
                action,
                reason=reason,
                observed_outside=reason == "left_region",
                occupied_s=round(occupied, 3),
                missing_s=round(missing, 3),
            )
        for target_id in sorted(inside):
            if target_id in watch.occupants:
                continue
            if len(watch.occupants) >= MAX_OCCUPANTS_PER_WATCH:
                watch.overflow_skipped += 1
                continue
            entry = inside[target_id]
            occupant = _Occupant(target_id, entry[1], (entry[3], entry[4]), initial=False)
            watch.occupants[target_id] = occupant
            self._emit_target(
                watch,
                "region_enter",
                occupant,
                f"entered region {quote_label(watch.name)} ({watch.watch_id})",
            )
        for occupant in sorted(dwells, key=lambda item: item.target_id):
            self._emit_target(
                watch,
                "region_dwell",
                occupant,
                f"has stayed in region {quote_label(watch.name)} ({watch.watch_id}) "
                f"for {occupant.dwell_clock_s:.1f} s",
                dwell_clock_s=round(occupant.dwell_clock_s, 3),
                occupied_s=round(occupant.occupied_s, 3),
                dwell_s=watch.dwell_s,
                initial=occupant.initial,
            )

    def _take_token(self, watch: _Watch) -> int | None:
        if watch.tokens < 1.0:
            watch.pending_suppressed += 1
            watch.suppressed_total += 1
            return None
        watch.tokens -= 1.0
        watch.events_emitted += 1
        suppressed = watch.pending_suppressed
        watch.pending_suppressed = 0
        return suppressed

    def _emit_watch(self, watch: _Watch, kind: str, summary: str, **extra: Any) -> None:
        suppressed = self._take_token(watch)
        if suppressed is None or self._sink is None:
            return
        self._sink(
            kind,
            summary,
            watch_id=watch.watch_id,
            watch_name=watch.name,
            frame_id=self._frame_id,
            frame_timestamp=self._timestamp,
            suppressed_before=suppressed,
            **extra,
        )

    def _emit_target(self, watch: _Watch, kind: str, occupant: _Occupant, action: str, **extra: Any) -> None:
        suppressed = self._take_token(watch)
        if suppressed is None or self._sink is None:
            return
        label = quote_label(occupant.class_name)
        self._sink(
            kind,
            f"Target {occupant.target_id} {label} {action}",
            watch_id=watch.watch_id,
            watch_name=watch.name,
            target_id=occupant.target_id,
            untrusted_detector_label=label,
            frame_id=self._frame_id,
            frame_timestamp=self._timestamp,
            center=[round(occupant.center[0], 4), round(occupant.center[1], 4)],
            suppressed_before=suppressed,
            **extra,
        )


def _fit_motion(samples: Sequence[_Sample], span: float) -> dict[str, Any]:
    """Least-squares rates of centre, size and log-scale over the retained samples."""
    count = len(samples)
    t_mean = sum(sample.timestamp for sample in samples) / count
    variance = sum((sample.timestamp - t_mean) ** 2 for sample in samples)

    def slope(values: Sequence[float]) -> tuple[float, float]:
        mean = sum(values) / count
        rate = sum((sample.timestamp - t_mean) * (value - mean) for sample, value in zip(samples, values))
        return rate / variance, mean

    xs = [sample.x for sample in samples]
    ys = [sample.y for sample in samples]
    vx, mean_x = slope(xs)
    vy, mean_y = slope(ys)
    vw, _ = slope([sample.width for sample in samples])
    vh, _ = slope([sample.height for sample in samples])
    scale_rate, _ = slope(
        [0.5 * math.log(max(sample.width, 1e-6) * max(sample.height, 1e-6)) for sample in samples]
    )
    residual = math.sqrt(
        sum(
            (x - (mean_x + vx * (sample.timestamp - t_mean))) ** 2
            + (y - (mean_y + vy * (sample.timestamp - t_mean))) ** 2
            for sample, x, y in zip(samples, xs, ys)
        )
        / count
    )
    confidence = min(1.0, (count - 1) / 5.0) * min(1.0, span) / (1.0 + residual / RESIDUAL_HALF_CONFIDENCE)
    return {
        "center_velocity_per_s": [round(vx, 4), round(vy, 4)],
        "center_speed_per_s": round(math.hypot(vx, vy), 4),
        "size_velocity_per_s": [round(vw, 4), round(vh, 4)],
        "scale_rate_per_s": round(scale_rate, 4),
        "residual_rms": round(residual, 4),
        "fit_confidence": round(confidence, 3),
    }


def _camera_motion(samples: Sequence[_Sample]) -> dict[str, Any]:
    """Classify measured camera movement over the motion window; it never proves a still camera."""
    for sample in samples:
        if sample.camera is None:
            return {
                "status": "unknown",
                "reason": sample.camera_reason,
                "interpretation": "camera state unavailable for part of the window: the image motion "
                "may be partly or entirely camera ego-motion",
            }
    reference = samples[0].camera
    worst: dict[str, float] = {}
    exceeded: set[str] = set()
    unmeasured: set[str] = set()
    for sample in samples:
        deviation, over = camera_deviation(reference, sample.camera)  # type: ignore[arg-type]
        exceeded.update(over)
        unmeasured.update(unmeasured_camera_components(sample.camera))  # type: ignore[arg-type]
        for name, value in deviation.items():
            worst[name] = max(worst.get(name, 0.0), value)
    common = {"max_deviation": worst, "tolerances": camera_tolerances(), "unmeasured": sorted(unmeasured)}
    if exceeded:
        return {
            "status": "moving",
            "exceeded": sorted(exceeded),
            **common,
            "interpretation": "the camera moved during the window: the image-relative rates include "
            "camera ego-motion and are not the target's own motion",
        }
    return {
        "status": "within_tolerance",
        **common,
        "interpretation": "measured camera components stayed within tolerance; sub-tolerance "
        "ego-motion and any unmeasured components can still move the image, so the rates remain "
        "image-relative approximations of target motion, never a metric speed",
    }


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _wrap_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def _bounded_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"{field} must be at most {MAX_TEXT_LENGTH} characters")
    return text


def _bounded_seconds(value: Any, field: str, low: float, high: float, exclusive_low: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number of seconds")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds > high or seconds < low or (exclusive_low and seconds == low):
        interval = f"({low:g}, {high:g}]" if exclusive_low else f"[{low:g}, {high:g}]"
        raise ValueError(f"{field} must be within {interval} seconds")
    return seconds


def _normalized_box(value: Any) -> tuple[float, float, float, float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 4:
        raise ValueError("box must be [x1, y1, x2, y2] as fractions of frame width/height")
    try:
        x1, y1, x2, y2 = (float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError("box values must be numbers") from exc
    if not all(math.isfinite(item) for item in (x1, y1, x2, y2)):
        raise ValueError("box values must be finite")
    if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
        raise ValueError("box must satisfy 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1")
    return x1, y1, x2, y2
