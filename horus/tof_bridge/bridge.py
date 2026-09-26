"""Convert Gazebo LaserScan samples into injected MAVLink DISTANCE_SENSOR sends."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable, Protocol
SUPPORTED_ORIENTATIONS = frozenset((*range(8), 25))


class LaserScanLike(Protocol):
    """The small subset of a gz.msgs10 LaserScan used by the bridge."""

    range_min: float
    range_max: float
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
) -> DistanceSensorReading | None:
    """Convert nearest return; positive-infinite/out-of-range beams mean clear range.

    Gazebo ranges are metres. DISTANCE_SENSOR limits and returns are rounded to
    centimetres and saturated to MAVLink's uint16 range. Orientation is the
    MAV_SENSOR_ORIENTATION horizontal values 0..7 and downward PITCH_270 (25).
    """
    if isinstance(orientation, bool) or not isinstance(orientation, int) or orientation not in SUPPORTED_ORIENTATIONS:
        raise ValueError("orientation must be a MAVLink horizontal value 0..7 or downward PITCH_270 value 25")
    if isinstance(sensor_id, bool) or not isinstance(sensor_id, int) or not 0 <= sensor_id <= 255:
        raise ValueError("sensor_id must be an integer from 0 through 255")
    if isinstance(time_boot_ms, bool) or not isinstance(time_boot_ms, int) or not 0 <= time_boot_ms <= 0xFFFFFFFF:
        raise ValueError("time_boot_ms must be an unsigned 32-bit integer")

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
        nearest_m = range_max_m

    current_distance = int(round(min(nearest_m, max_centimeters / 100) * 100))
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


class MultiSensorBridge:
    """Route multiple LaserScan topics through one shared MAVLink sender."""

    def __init__(
        self,
        sender: Callable[[DistanceSensorReading], None],
        sensors: Iterable[SensorSpec],
        rate_hz: float = 20.0,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        specs = tuple(sensors)
        if not specs:
            raise ValueError("at least one LaserScan sensor is required")
        if any(not isinstance(spec, SensorSpec) for spec in specs):
            raise TypeError("sensors must contain SensorSpec values")
        topics = tuple(spec.topic for spec in specs)
        if len(set(topics)) != len(topics):
            raise ValueError("sensor topics must be unique")
        sensor_ids = tuple(spec.sensor_id for spec in specs)
        if len(set(sensor_ids)) != len(sensor_ids):
            raise ValueError("sensor IDs must be unique")
        self.sensors = specs
        self._bridges = {
            spec.topic: DistanceSensorBridge(
                sender,
                TofBridgeConfig(rate_hz, spec.orientation, spec.sensor_id),
                monotonic=monotonic,
                clock_ms=clock_ms,
            )
            for spec in specs
        }

    def on_scan(self, topic: str, scan: LaserScanLike) -> DistanceSensorReading | None:
        """Forward a fresh scan using the identity configured for its topic."""
        try:
            bridge = self._bridges[topic]
        except KeyError as exc:
            raise ValueError(f"no sensor configured for LaserScan topic {topic!r}") from exc
        return bridge.on_scan(scan)
