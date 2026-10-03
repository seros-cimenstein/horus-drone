"""Convert Gazebo LaserScan samples into injected MAVLink DISTANCE_SENSOR sends."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Callable, Protocol
SUPPORTED_ORIENTATIONS = frozenset((*range(8), 25))

# The generated-world payload (sim/models/horus_sensor_payload_procedural) replaces the six
# single-beam proximity ToFs with one 8-beam horizontal ring at the payload origin. Each beam
# stands in for one of the directions the single-beam payload covered: the MAVLink clockwise
# 45-degree orientation, the DISTANCE_SENSOR id and the distance from the payload origin to
# the face of the sensor that used to sit there. Subtracting that radial offset turns the
# ring's centre-based range back into the old sensor's face-to-obstacle reading.
_FRONT_DIAGONAL_OFFSET_M = math.hypot(0.30, 0.30)
RING_BEAM_ANGLE_TOLERANCE_DEG = 1.0


@dataclass(frozen=True, slots=True)
class RingBeam:
    """One ring beam: SDF angle (counter-clockwise/left positive), MAVLink identity, old face."""

    angle_deg: float
    orientation: int
    sensor_id: int
    radial_offset_m: float

    def __post_init__(self) -> None:
        if isinstance(self.angle_deg, bool) or not isinstance(self.angle_deg, (int, float)) \
                or not math.isfinite(self.angle_deg):
            raise ValueError("angle_deg must be a finite number of degrees")
        TofBridgeConfig(orientation=self.orientation, sensor_id=self.sensor_id)
        if isinstance(self.radial_offset_m, bool) or not isinstance(self.radial_offset_m, (int, float)) \
                or not math.isfinite(self.radial_offset_m) or self.radial_offset_m < 0:
            raise ValueError("radial_offset_m must be a finite non-negative number of metres")


class LaserScanLike(Protocol):
    """The small subset of a gz.msgs10 LaserScan used by the bridge."""

    range_min: float
    range_max: float
    ranges: Iterable[float]


class RingScanLike(Protocol):
    """The gz.msgs10 LaserScan subset needed to fan out one reading per ring beam.

    ``angle_step`` (or the ``angle_max``/range-count fallback) describes where each return was
    aimed; the beam angles are never hard-coded.
    """

    range_min: float
    range_max: float
    angle_min: float
    angle_step: float
    ranges: Iterable[float]


@dataclass(frozen=True, slots=True)
class DistanceSensorReading:
    """Bounded DISTANCE_SENSOR payload in MAVLink units (distances in cm)."""

    time_boot_ms: int
    min_distance: int
    max_distance: int
    current_distance: int
    sensor_type: int
    sensor_id: int
    orientation: int
    covariance: int = 0


@dataclass(frozen=True, slots=True)
class TofBridgeConfig:
    """Output settings for one Gazebo lidar topic and one MAVLink sensor ID."""

    rate_hz: float = 20.0
    orientation: int = 0
    sensor_id: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.rate_hz, bool)
            or not math.isfinite(self.rate_hz)
            or not 10 <= self.rate_hz <= 20
        ):
            raise ValueError("rate_hz must be between 10 and 20 Hz")
        if (
            isinstance(self.orientation, bool)
            or not isinstance(self.orientation, int)
            or self.orientation not in SUPPORTED_ORIENTATIONS
        ):
            raise ValueError(
                "orientation must be a MAVLink horizontal value 0..7 or "
                "downward PITCH_270 value 25"
            )
        if (
            isinstance(self.sensor_id, bool)
            or not isinstance(self.sensor_id, int)
            or not 0 <= self.sensor_id <= 255
        ):
            raise ValueError("sensor_id must be an integer from 0 through 255")


# The six directions that existed keep their old sensor IDs (0 front, 1 front-right, 2 right,
# 3 rear, 4 left, 5 front-left); the two rear diagonals are new and take the next free IDs 7
# (rear-right, orientation 3) and 8 (rear-left, orientation 5). range_down keeps id 6.
RING_BEAMS: tuple[RingBeam, ...] = (
    RingBeam(-180.0, 4, 3, 0.32),                     # proximity_rear
    RingBeam(-135.0, 3, 7, 0.32),                     # new rear-right diagonal
    RingBeam(-90.0, 2, 2, 0.32),                      # proximity_right
    RingBeam(-45.0, 1, 1, _FRONT_DIAGONAL_OFFSET_M),  # proximity_front_right
    RingBeam(0.0, 0, 0, 0.24),                        # proximity_front
    RingBeam(45.0, 7, 5, _FRONT_DIAGONAL_OFFSET_M),   # proximity_front_left
    RingBeam(90.0, 6, 4, 0.32),                       # proximity_left
    RingBeam(135.0, 5, 8, 0.32),                      # new rear-left diagonal
)


@dataclass(frozen=True, slots=True)
class SensorSpec:
    """One Gazebo LaserScan source and its MAVLink DISTANCE_SENSOR identity."""

    topic: str
    orientation: int
    sensor_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.topic, str) or not self.topic.strip():
            raise ValueError("topic must be a non-empty Gazebo LaserScan topic")
        TofBridgeConfig(orientation=self.orientation, sensor_id=self.sensor_id)


@dataclass(frozen=True, slots=True)
class RingSensorSpec:
    """One ring LaserScan topic fanned out to one MAVLink sensor per beam."""

    topic: str
    beams: tuple[RingBeam, ...] = RING_BEAMS

    def __post_init__(self) -> None:
        if not isinstance(self.topic, str) or not self.topic.strip():
            raise ValueError("topic must be a non-empty Gazebo LaserScan topic")
        if not isinstance(self.beams, tuple) or not self.beams \
                or any(not isinstance(beam, RingBeam) for beam in self.beams):
            raise ValueError("beams must be a non-empty tuple of RingBeam values")
        if len({beam.sensor_id for beam in self.beams}) != len(self.beams):
            raise ValueError("ring beam sensor IDs must be unique")
        if len({beam.orientation for beam in self.beams}) != len(self.beams):
            raise ValueError("ring beam orientations must be unique")


def _finite_float(value: object, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"LaserScan {field} is not a number") from exc
    if not math.isfinite(result):
        raise ValueError(f"LaserScan {field} must be finite")
    return result


def laser_scan_to_distance_sensor(
    scan: LaserScanLike,
    *,
    time_boot_ms: int,
    orientation: int = 0,
    sensor_id: int = 0,
    radial_offset_m: float = 0.0,
) -> DistanceSensorReading | None:
    """Convert nearest return; positive-infinite/out-of-range beams mean clear range.

    Gazebo ranges are metres. DISTANCE_SENSOR limits and returns are rounded to
    centimetres and saturated to MAVLink's uint16 range. Orientation is the
    MAV_SENSOR_ORIENTATION horizontal values 0..7 and downward PITCH_270 (25).

    ``radial_offset_m`` is the distance from the lidar's origin to the face of the sensor the
    scan stands in for (a ring beam replacing a single-beam ToF). It is subtracted from a
    measured return and the result clamped to the sensor's [min, max] centimetre range, so
    readings keep the old face-to-obstacle meaning. Clear beams (+inf or beyond range_max) report
    one centimetre past ``max_distance``: ArduPilot's MAVLink proximity backend stores any reading
    with ``min <= distance <= max`` as an obstacle, so a clear beam at exactly the maximum range
    put a phantom obstacle 4 m away in every sector and throttled/deflected guided velocity.
    """
    if isinstance(orientation, bool) or not isinstance(orientation, int) or orientation not in SUPPORTED_ORIENTATIONS:
        raise ValueError("orientation must be a MAVLink horizontal value 0..7 or downward PITCH_270 value 25")
    if isinstance(sensor_id, bool) or not isinstance(sensor_id, int) or not 0 <= sensor_id <= 255:
        raise ValueError("sensor_id must be an integer from 0 through 255")
    if isinstance(time_boot_ms, bool) or not isinstance(time_boot_ms, int) or not 0 <= time_boot_ms <= 0xFFFFFFFF:
        raise ValueError("time_boot_ms must be an unsigned 32-bit integer")
    if isinstance(radial_offset_m, bool):
        raise ValueError("radial_offset_m must be a finite non-negative number of metres")
    try:
        radial_offset_m = float(radial_offset_m)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("radial_offset_m must be a finite non-negative number of metres") from exc
    if not math.isfinite(radial_offset_m) or radial_offset_m < 0:
        raise ValueError("radial_offset_m must be a finite non-negative number of metres")

    try:
        range_min_m = _finite_float(scan.range_min, "range_min")
        range_max_m = _finite_float(scan.range_max, "range_max")
        ranges = scan.ranges
    except AttributeError as exc:
        raise ValueError("LaserScan is missing range_min, range_max, or ranges") from exc
    if range_min_m < 0 or range_max_m <= 0 or range_min_m > range_max_m:
        raise ValueError("LaserScan range_min/range_max must satisfy 0 <= range_min <= range_max and range_max > 0")

    max_centimeters = 0xFFFF
    min_distance = int(round(min(range_min_m, max_centimeters / 100) * 100))
    max_distance = int(round(min(range_max_m, max_centimeters / 100) * 100))
    if max_distance < min_distance:
        raise ValueError("LaserScan range limits cannot be represented as ordered MAVLink centimetres")

    nearest_m: float | None = None
    has_clear_beam = False
    try:
        for sample in ranges:
            try:
                distance_m = float(sample)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isinf(distance_m) and distance_m > 0:
                has_clear_beam = True
            elif math.isfinite(distance_m) and range_min_m <= distance_m <= range_max_m:
                if nearest_m is None or distance_m < nearest_m:
                    nearest_m = distance_m
            elif math.isfinite(distance_m) and distance_m > range_max_m:
                has_clear_beam = True
    except TypeError as exc:
        raise ValueError("LaserScan ranges must be an iterable of distances") from exc
    if nearest_m is None:
        if not has_clear_beam:
            return None
        current_distance = min(max_distance + 1, max_centimeters)
    else:
        current_distance = int(round(min(nearest_m - radial_offset_m, max_centimeters / 100) * 100))
        current_distance = max(min_distance, min(max_distance, current_distance))
    return DistanceSensorReading(
        time_boot_ms=time_boot_ms,
        min_distance=min_distance,
        max_distance=max_distance,
        current_distance=current_distance,
        sensor_type=0,  # MAV_DISTANCE_SENSOR_LASER
        sensor_id=sensor_id,
        orientation=orientation,
        covariance=0,  # Unknown measurement variance.
    )


@dataclass(frozen=True, slots=True)
class _SingleBeamScan:
    """One ring return presented as a one-sample LaserScan for the shared converter."""

    range_min: float
    range_max: float
    ranges: tuple[float, ...]


def _scan_angle_step(scan: RingScanLike, angle_min: float, count: int) -> float:
    """Beam step in radians, from ``angle_step`` or the ``angle_max``/count fallback."""
    step = getattr(scan, "angle_step", None)
    if step is not None:
        step = _finite_float(step, "angle_step")
        if step > 0:
            return step
    angle_max = getattr(scan, "angle_max", None)
    if angle_max is None or count <= 1:
        raise ValueError("LaserScan needs a positive angle_step or angle_max with more than one range")
    step = (_finite_float(angle_max, "angle_max") - angle_min) / (count - 1)
    if not step > 0:
        raise ValueError("LaserScan angles must increase from angle_min to angle_max")
    return step


def _beam_at_angle(angle_deg: float, beams: Sequence[RingBeam]) -> RingBeam | None:
    """The beam whose 45-degree direction ``angle_deg`` lands on, or None outside tolerance."""
    normalized = (angle_deg + 180.0) % 360.0 - 180.0
    for beam in beams:
        delta = (normalized - beam.angle_deg + 180.0) % 360.0 - 180.0
        if abs(delta) <= RING_BEAM_ANGLE_TOLERANCE_DEG:
            return beam
    return None


def ring_scan_to_distance_sensors(
    scan: RingScanLike,
    *,
    time_boot_ms: int,
    beams: Sequence[RingBeam] = RING_BEAMS,
) -> list[DistanceSensorReading]:
    """Fan one ring LaserScan out to one DISTANCE_SENSOR reading per mapped beam.

    Beam angles come from the scan itself (``angle_min + index * angle_step``, falling back to
    ``angle_max`` with the range count); a beam whose angle misses its configured 45-degree
    direction by more than :data:`RING_BEAM_ANGLE_TOLERANCE_DEG` is skipped. Each matched beam
    is converted through :func:`laser_scan_to_distance_sensor` with its direction's old radial
    offset, so no-return/out-of-range handling matches the seven-sensor path exactly.
    """
    try:
        ranges = list(scan.ranges)
    except AttributeError as exc:
        raise ValueError("LaserScan is missing ranges") from exc
    except TypeError as exc:
        raise ValueError("LaserScan ranges must be an iterable of distances") from exc
    if not ranges:
        return []
    try:
        range_min = scan.range_min
        range_max = scan.range_max
    except AttributeError as exc:
        raise ValueError("LaserScan is missing range_min or range_max") from exc

    angle_min = _finite_float(getattr(scan, "angle_min", None), "angle_min")
    angle_step = _scan_angle_step(scan, angle_min, len(ranges))

    readings: list[DistanceSensorReading] = []
    for index, sample in enumerate(ranges):
        beam = _beam_at_angle(math.degrees(angle_min + index * angle_step), beams)
        if beam is None:
            continue
        reading = laser_scan_to_distance_sensor(
            _SingleBeamScan(range_min, range_max, (sample,)),
            time_boot_ms=time_boot_ms,
            orientation=beam.orientation,
            sensor_id=beam.sensor_id,
            radial_offset_m=beam.radial_offset_m,
        )
        if reading is not None:
            readings.append(reading)
    return readings


class DistanceSensorBridge:
    """Rate-limit scans and deliver readings through a callable sender.

    The configured rate is an upper bound: a DISTANCE_SENSOR message is sent
    only when a fresh LaserScan arrives, so a stalled sensor is not replayed as
    fresh data. A sender receives one :class:`DistanceSensorReading` per send.
    """

    def __init__(
        self,
        sender: Callable[[DistanceSensorReading], None],
        config: TofBridgeConfig | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not callable(sender):
            raise TypeError("sender must be callable and accept a DistanceSensorReading")
        self.config = config or TofBridgeConfig()
        self._sender = sender
        self._monotonic = monotonic
        self._clock_ms = clock_ms or (lambda: int(self._monotonic() * 1000))
        self._interval_s = 1.0 / self.config.rate_hz
        self._last_sent_at: float | None = None
        self._lock = threading.Lock()

    def on_scan(self, scan: LaserScanLike) -> DistanceSensorReading | None:
        """Send a scan's nearest valid range unless rate-limited or out of range."""
        now = self._monotonic()
        with self._lock:
            if self._last_sent_at is not None and now - self._last_sent_at < self._interval_s:
                return None

        reading = laser_scan_to_distance_sensor(
            scan,
            time_boot_ms=self._clock_ms() & 0xFFFFFFFF,
            orientation=self.config.orientation,
            sensor_id=self.config.sensor_id,
        )
        if reading is None:
            return None

        with self._lock:
            send_at = self._monotonic()
            if self._last_sent_at is not None and send_at - self._last_sent_at < self._interval_s:
                return None
            self._sender(reading)
            self._last_sent_at = self._monotonic()
        return reading


class RingSensorBridge:
    """Rate-limit a ring scan and deliver one reading per beam through a callable sender.

    Mirrors :class:`DistanceSensorBridge`: the configured rate is an upper bound, each
    DISTANCE_SENSOR id is rate-limited independently, and a stalled ring is never replayed
    as fresh data.
    """

    def __init__(
        self,
        sender: Callable[[DistanceSensorReading], None],
        spec: RingSensorSpec,
        rate_hz: float = 20.0,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not callable(sender):
            raise TypeError("sender must be callable and accept a DistanceSensorReading")
        if not isinstance(spec, RingSensorSpec):
            raise TypeError("spec must be a RingSensorSpec")
        self.spec = spec
        self.config = TofBridgeConfig(rate_hz=rate_hz)
        self._sender = sender
        self._monotonic = monotonic
        self._clock_ms = clock_ms or (lambda: int(self._monotonic() * 1000))
        self._interval_s = 1.0 / self.config.rate_hz
        self._last_sent_at: dict[int, float] = {}
        self._lock = threading.Lock()

    def on_scan(self, scan: RingScanLike) -> list[DistanceSensorReading]:
        """Send every fresh beam reading, subject to the per-sensor rate limit."""
        readings = ring_scan_to_distance_sensors(
            scan,
            time_boot_ms=self._clock_ms() & 0xFFFFFFFF,
            beams=self.spec.beams,
        )
        sent: list[DistanceSensorReading] = []
        with self._lock:
            for reading in readings:
                now = self._monotonic()
                last = self._last_sent_at.get(reading.sensor_id)
                if last is not None and now - last < self._interval_s:
                    continue
                self._sender(reading)
                self._last_sent_at[reading.sensor_id] = self._monotonic()
                sent.append(reading)
        return sent


def sensor_ids(spec: SensorSpec | RingSensorSpec) -> tuple[int, ...]:
    """The DISTANCE_SENSOR ids a spec emits (one per beam for a ring spec)."""
    if isinstance(spec, RingSensorSpec):
        return tuple(beam.sensor_id for beam in spec.beams)
    return (spec.sensor_id,)


class MultiSensorBridge:
    """Route multiple LaserScan topics through one shared MAVLink sender."""

    def __init__(
        self,
        sender: Callable[[DistanceSensorReading], None],
        sensors: Iterable[SensorSpec | RingSensorSpec],
        rate_hz: float = 20.0,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        specs = tuple(sensors)
        if not specs:
            raise ValueError("at least one LaserScan sensor is required")
        if any(not isinstance(spec, (SensorSpec, RingSensorSpec)) for spec in specs):
            raise TypeError("sensors must contain SensorSpec or RingSensorSpec values")
        topics = tuple(spec.topic for spec in specs)
        if len(set(topics)) != len(topics):
            raise ValueError("sensor topics must be unique")
        ids = tuple(sensor_id for spec in specs for sensor_id in sensor_ids(spec))
        if len(set(ids)) != len(ids):
            raise ValueError("sensor IDs must be unique")
        self.sensors = specs
        self._bridges: dict[str, DistanceSensorBridge | RingSensorBridge] = {}
        for spec in specs:
            if isinstance(spec, RingSensorSpec):
                self._bridges[spec.topic] = RingSensorBridge(
                    sender, spec, rate_hz, monotonic=monotonic, clock_ms=clock_ms)
            else:
                self._bridges[spec.topic] = DistanceSensorBridge(
                    sender,
                    TofBridgeConfig(rate_hz, spec.orientation, spec.sensor_id),
                    monotonic=monotonic,
                    clock_ms=clock_ms,
                )

    def on_scan(
        self, topic: str, scan: LaserScanLike | RingScanLike
    ) -> DistanceSensorReading | list[DistanceSensorReading] | None:
        """Forward a fresh scan using the identity configured for its topic."""
        try:
            bridge = self._bridges[topic]
        except KeyError as exc:
            raise ValueError(f"no sensor configured for LaserScan topic {topic!r}") from exc
        return bridge.on_scan(scan)
