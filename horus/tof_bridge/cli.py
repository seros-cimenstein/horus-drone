"""Command-line Gazebo LaserScan to MAVLink DISTANCE_SENSOR bridge."""

import argparse
import math
import queue
import sys
import threading
import time
from collections.abc import Sequence
from typing import Any

from .bridge import (
    DistanceSensorReading,
    MultiSensorBridge,
    RING_BEAMS,
    RingSensorSpec,
    SensorSpec,
    validate_rate_hz,
)
from .transport import subscribe_laser_scan

BRIDGE_SOURCE_SYSTEM = 254
BRIDGE_SOURCE_COMPONENT = 196  # MAV_COMP_ID_OBSTACLE_AVOIDANCE

# The downward rangefinder (MAVLink PITCH_270), identical in both payload layouts.
PAYLOAD_DOWN_SENSOR = ("range_down", 25, 6)
# The standard HORUS ToF payload (sim/models/horus_sensor_payload): sensor name, MAVLink
# DISTANCE_SENSOR orientation and sensor ID, in id order. The horizontal sensors are the named
# RING_BEAMS directions; the payload SDF yaws each beam to the matching body direction.
PAYLOAD_SENSORS: tuple[tuple[str, int, int], ...] = (
    *sorted(((beam.name, beam.orientation, beam.sensor_id) for beam in RING_BEAMS if beam.name),
            key=lambda sensor: sensor[2]),
    PAYLOAD_DOWN_SENSOR,
)
PAYLOAD_LINK = "payload"
# Generated worlds (horus_forest_p*/horus_urban_p*) carry horus_sensor_payload_procedural: one
# 8-beam ring fanned out over every RING_BEAMS direction, plus the downward rangefinder.
PAYLOAD_RING_SENSOR = "proximity_ring"
PAYLOAD_LAYOUTS = ("seven", "ring")


def payload_topic_root(world: str, model_chain: str) -> str:
    """Scoped LaserScan topic root of the ToF payload inside a Gazebo world.

    ``model_chain`` is the slash- or comma-separated chain of model names between the world
    and the payload, e.g. ``horus_iris/horus_sensor_payload`` (indoor) or
    ``horus_iris/iris_with_gimbal/horus_sensor_payload`` (outdoor airframe). gz-sim scopes
    sensor topics under every enclosing model, so the chain is what differs between worlds.
    """
    names = [name.strip() for name in model_chain.replace(",", "/").split("/") if name.strip()]
    if not world.strip() or not names:
        raise ValueError("--world and --model-chain must name the world and the payload's parent models")
    scope = "".join(f"/model/{name}" for name in names)
    return f"/world/{world.strip()}{scope}/link/{PAYLOAD_LINK}/sensor"


def payload_sensors(world: str, model_chain: str) -> list[SensorSpec]:
    """The seven standard payload sensors as SensorSpecs scoped to ``world``."""
    root = payload_topic_root(world, model_chain)
    return [SensorSpec(f"{root}/{name}/scan", orientation, sensor_id)
            for name, orientation, sensor_id in PAYLOAD_SENSORS]


def payload_ring_sensors(world: str, model_chain: str) -> list[SensorSpec | RingSensorSpec]:
    """The generated-world payload sensors: the ring topic plus the unchanged down sensor."""
    root = payload_topic_root(world, model_chain)
    name, orientation, sensor_id = PAYLOAD_DOWN_SENSOR
    return [
        RingSensorSpec(f"{root}/{PAYLOAD_RING_SENSOR}/scan", RING_BEAMS),
        SensorSpec(f"{root}/{name}/scan", orientation, sensor_id),
    ]


def payload_specs(world: str, model_chain: str, layout: str = "seven") -> list[SensorSpec | RingSensorSpec]:
    """Payload sensors for ``layout``: the classic seven, or the generated-world ring."""
    if layout == "seven":
        return payload_sensors(world, model_chain)
    if layout == "ring":
        return payload_ring_sensors(world, model_chain)
    raise ValueError(f"unknown payload layout {layout!r}; expected one of {', '.join(PAYLOAD_LAYOUTS)}")


class _PymavlinkSender:
    """Adapt a pymavlink connection or MAVLink object to the bridge sender API."""

    def __init__(self, link: Any) -> None:
        mav = getattr(link, "mav", link)
        self._send = getattr(mav, "distance_sensor_send", None)
        if not callable(self._send):
            raise RuntimeError("MAVLink link has no distance_sensor_send method")
        self._lock = threading.Lock()

    def __call__(self, reading: DistanceSensorReading) -> None:
        with self._lock:
            self._send(
                reading.time_boot_ms,
                reading.min_distance,
                reading.max_distance,
                reading.current_distance,
                reading.sensor_type,
                reading.sensor_id,
                reading.orientation,
                reading.covariance,
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m horus.tof_bridge",
        description="Forward multiple Gazebo LaserScan topics over one MAVLink link.",
        epilog=(
            "Example: python -m horus.tof_bridge --mavlink tcp:127.0.0.1:5763 "
            "--world horus_indoor --model-chain horus_iris/horus_sensor_payload"
        ),
    )
    parser.add_argument(
        "--world",
        required=True,
        help="Gazebo world name; bridges the payload's seven sensors (or its 8-beam ring "
             "with --layout ring)",
    )
    parser.add_argument(
        "--model-chain",
        required=True,
        help="model names between the world and the payload, slash- or comma-separated "
             "(e.g. horus_iris/iris_with_gimbal/horus_sensor_payload)",
    )
    parser.add_argument(
        "--layout",
        choices=PAYLOAD_LAYOUTS,
        default="seven",
        help="payload layout: seven single-beam ToF sensors "
             "(indoor, canonical outdoor; the default) or ring, one proximity_ring scan fanned "
             "out to one DISTANCE_SENSOR per beam (generated worlds)",
    )
    parser.add_argument(
        "--mavlink",
        default="tcp:127.0.0.1:5763",
        help="single pymavlink connection shared by all sensors (default: tcp:127.0.0.1:5763)",
    )
    parser.add_argument(
        "--rate-hz",
        type=float,
        default=20.0,
        help="maximum send rate per sensor, from 10 to 20 Hz (default: 20)",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=60.0,
        help="seconds before warning that no scan arrived yet (default: 60; never fatal: the "
             "lockstep world does not run until a flight client connects)",
    )
    return parser


class _StartupWatch:
    """Non-fatal startup progress for the ToF bridge.

    SITL's lockstep world only advances while a flight client holds its MAVLink port, so a
    bridge started before anyone connects legitimately sees no LaserScan for minutes. Warn
    periodically instead of exiting: a missing topic is a configuration error the log names,
    not a reason to stop the simulation.
    """

    def __init__(self, timeout: float, interval: float = 30.0, *,
                 started: float | None = None) -> None:
        self._timeout = timeout
        self._interval = interval
        self._started = time.monotonic() if started is None else started
        self._next_warning = self._started + timeout

    def update(self, missing: Sequence[str], *, now: float | None = None) -> str | None:
        """Return the periodic warning to log, or None while the timeout has not elapsed."""
        if not missing:
            return None
        now = time.monotonic() if now is None else now
        if now < self._started + self._timeout or now < self._next_warning:
            return None
        self._next_warning = now + self._interval
        return (f"waiting for first scan (no flight client holding SITL yet?): "
                f"{', '.join(missing)}")


def _open_mavlink(endpoint: str) -> Any:
    try:
        from pymavlink import mavutil
    except ImportError as exc:
        raise RuntimeError("pymavlink is unavailable; install it in the simulation container") from exc
    try:
        return mavutil.mavlink_connection(
            endpoint,
            source_system=BRIDGE_SOURCE_SYSTEM,
            source_component=BRIDGE_SOURCE_COMPONENT,
        )
    except Exception as exc:
        raise RuntimeError(f"could not open MAVLink endpoint {endpoint!r}: {exc}") from exc


def _serve(
    sensors: Sequence[SensorSpec | RingSensorSpec],
    link: Any,
    rate_hz: float,
    startup_timeout: float,
) -> None:
    bridge = MultiSensorBridge(_PymavlinkSender(link), sensors, rate_hz)
    received: set[str] = set()
    received_lock = threading.Lock()
    callback_errors: queue.Queue[Exception] = queue.Queue(maxsize=1)
    nodes = []

    def scan_callback(topic: str):
        def on_scan(scan: Any) -> None:
            try:
                if bridge.on_scan(topic, scan):
                    with received_lock:
                        received.add(topic)
            except Exception as exc:
                try:
                    callback_errors.put_nowait(exc)
                except queue.Full:
                    pass

        return on_scan

    for sensor in sensors:
        nodes.append(subscribe_laser_scan(sensor.topic, scan_callback(sensor.topic)))
        if isinstance(sensor, RingSensorSpec):
            ids = ", ".join(str(beam.sensor_id) for beam in sensor.beams)
            print(
                f"Subscribed to {sensor.topic} as a {len(sensor.beams)}-beam ring "
                f"(DISTANCE_SENSOR ids {ids})",
                flush=True,
            )
        else:
            print(
                f"Subscribed to {sensor.topic} as DISTANCE_SENSOR id={sensor.sensor_id} "
                f"orientation={sensor.orientation}",
                flush=True,
            )

    ready = False
    watch = _StartupWatch(startup_timeout)
    while True:
        try:
            callback_error = callback_errors.get_nowait()
        except queue.Empty:
            callback_error = None
        if callback_error is not None:
            raise RuntimeError(f"LaserScan/MAVLink callback failed: {callback_error}") from callback_error
        with received_lock:
            missing = [sensor.topic for sensor in sensors if sensor.topic not in received]
        if not missing and not ready:
            print(
                f"Ready: {len(sensors)} LaserScan topics streaming over one MAVLink link",
                flush=True,
            )
            ready = True
        if not ready:
            # Never fatal: the world may simply have no flight client yet (see _StartupWatch).
            warning = watch.update(missing)
            if warning is not None:
                print(warning, flush=True)
        time.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.startup_timeout) or args.startup_timeout < 0:
        parser.error("--startup-timeout must be finite and non-negative")
    try:
        validate_rate_hz(args.rate_hz)
        sensors = payload_specs(args.world, args.model_chain, args.layout)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        link = _open_mavlink(args.mavlink)
        print(
            f"Connected to {args.mavlink} as sysid={BRIDGE_SOURCE_SYSTEM} "
            f"compid={BRIDGE_SOURCE_COMPONENT}",
            flush=True,
        )
        try:
            _serve(sensors, link, args.rate_hz, args.startup_timeout)
        except KeyboardInterrupt:
            return 0
        finally:
            close = getattr(link, "close", None)
            if callable(close):
                close()
    except (RuntimeError, ValueError) as exc:
        print(f"horus-tof-bridge: {exc}", file=sys.stderr)
        return 2
    return 0
