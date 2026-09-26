"""Command-line Gazebo LaserScan to MAVLink DISTANCE_SENSOR bridge."""

import argparse
import math
import queue
import sys
import threading
import time
from typing import Any

from .bridge import DistanceSensorReading, MultiSensorBridge, SensorSpec
from .transport import subscribe_laser_scan

BRIDGE_SOURCE_SYSTEM = 254
BRIDGE_SOURCE_COMPONENT = 196  # MAV_COMP_ID_OBSTACLE_AVOIDANCE


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
            "--sensor /world/indoor/model/iris/link/lidar/sensor/scan:1:0"
        ),
    )
    parser.add_argument(
        "--sensor",
        action="append",
        required=True,
        metavar="TOPIC:ORIENTATION[:ID]",
        help="LaserScan topic and MAVLink orientation; repeat once per sensor",
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
        default=15.0,
        help="seconds to wait for one valid scan from every topic; 0 disables the timeout",
    )
    return parser


def _parse_sensor(value: str, default_sensor_id: int) -> SensorSpec:
    parts = value.rsplit(":", 2)
    if len(parts) not in (2, 3):
        raise ValueError("--sensor must use TOPIC:ORIENTATION[:ID]")
    topic = parts[0].strip()
    try:
        orientation = int(parts[1])
        sensor_id = int(parts[2]) if len(parts) == 3 else default_sensor_id
    except ValueError as exc:
        raise ValueError("--sensor orientation and ID must be integers") from exc
    return SensorSpec(topic, orientation, sensor_id)


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
    sensors: list[SensorSpec],
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
                if bridge.on_scan(topic, scan) is not None:
                    with received_lock:
                        received.add(topic)
            except Exception as exc:
                try:
                    callback_errors.put_nowait(exc)
                except queue.Full:
                    pass

        return on_scan

    started = time.monotonic()
    for sensor in sensors:
        nodes.append(subscribe_laser_scan(sensor.topic, scan_callback(sensor.topic)))
        print(
            f"Subscribed to {sensor.topic} as DISTANCE_SENSOR id={sensor.sensor_id} "
            f"orientation={sensor.orientation}",
            flush=True,
        )

    ready = False
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
                f"Ready: {len(sensors)} LaserScan sensors streaming over one MAVLink link",
                flush=True,
            )
            ready = True
        if missing and startup_timeout and time.monotonic() - started >= startup_timeout:
            raise RuntimeError(
                f"no valid LaserScan received on {', '.join(missing)} within "
                f"{startup_timeout:g} seconds"
            )
        time.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.rate_hz) or not 10 <= args.rate_hz <= 20:
        parser.error("--rate-hz must be between 10 and 20")
    if not math.isfinite(args.startup_timeout) or args.startup_timeout < 0:
        parser.error("--startup-timeout must be finite and non-negative")
    try:
        sensors = [_parse_sensor(value, index) for index, value in enumerate(args.sensor)]
    except ValueError as exc:
        parser.error(str(exc))
    if len({sensor.topic for sensor in sensors}) != len(sensors):
        parser.error("--sensor topics must be unique")
    if len({sensor.sensor_id for sensor in sensors}) != len(sensors):
        parser.error("--sensor IDs must be unique")
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
