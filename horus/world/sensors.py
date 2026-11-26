"""Sensors and gimbal bridge for the built-in 3D test world (#4e2c)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from horus.gimbal.device import GimbalDeviceEndpoint
from horus.tof_bridge.bridge import DistanceSensorReading
from horus.tof_bridge.mavlink_out import PymavlinkSender, open_mavlink
from horus.world.physics import MuJoCoQuadWorld

logger = logging.getLogger(__name__)

# (MuJoCo rangefinder name, MAVLink ROTATION_* orientation, sensor_id)
WORLD_RANGE_SENSORS: tuple[tuple[str, int, int], ...] = (
    ("range_front", 0, 0),
    ("range_front_right", 1, 1),
    ("range_back_right", 3, 7),
    ("range_back", 4, 3),
    ("range_back_left", 5, 8),
    ("range_front_left", 7, 5),
    ("range_down", 25, 6),
)

_MIN_DISTANCE_CM = 5
_MAX_DISTANCE_CM = 400
_MAV_DISTANCE_SENSOR_LASER = 0


def rangefinders_to_distance_readings(
    distances_m: dict[str, float],
    time_boot_ms: int,
) -> list[DistanceSensorReading]:
    """Convert MuJoCo rangefinder distances (m) into MAVLink DISTANCE_SENSOR readings (cm)."""
    readings: list[DistanceSensorReading] = []
    for name, orientation, sensor_id in WORLD_RANGE_SENSORS:
        dist_m = float(distances_m.get(name, 4.0))
        dist_cm = max(_MIN_DISTANCE_CM, min(_MAX_DISTANCE_CM, int(round(dist_m * 100.0))))
        readings.append(
            DistanceSensorReading(
                time_boot_ms=time_boot_ms & 0xFFFFFFFF,
                min_distance=_MIN_DISTANCE_CM,
                max_distance=_MAX_DISTANCE_CM,
                current_distance=dist_cm,
                sensor_type=_MAV_DISTANCE_SENSOR_LASER,
                sensor_id=sensor_id,
                orientation=orientation,
                covariance=0,
            )
        )
    return readings


class WorldSensors:
    """20 Hz 7-beam ToF DISTANCE_SENSOR publisher + 2-axis MAVLink gimbal endpoint."""

    def __init__(
        self,
        world: MuJoCoQuadWorld,
        *,
        range_endpoint: str | None = "tcp:127.0.0.1:5763",
        gimbal_connection: str | None = "udpout:127.0.0.1:14600",
        rate_hz: float = 20.0,
        mavlink_sender: Any | None = None,
        gimbal_device: GimbalDeviceEndpoint | None = None,
    ) -> None:
        self.world = world
        self.range_endpoint = range_endpoint
        self.gimbal_connection = gimbal_connection
        self.rate_hz = max(5.0, float(rate_hz))
        self._sender = mavlink_sender
        self._link: Any = None
        self._gimbal = gimbal_device
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._booted = time.monotonic()

    def start(self) -> None:
        if self._gimbal is None and self.gimbal_connection:
            self._gimbal = GimbalDeviceEndpoint(self.gimbal_connection)
        if self._gimbal is not None:
            self._gimbal.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="horus-world-sensors", daemon=True)
        self._thread.start()

    def _ensure_sender(self) -> Any | None:
        if self._sender is not None:
            return self._sender
        if not self.range_endpoint:
            return None
        try:
            self._link = open_mavlink(self.range_endpoint)
            self._sender = PymavlinkSender(self._link)
            return self._sender
        except Exception as exc:
            logger.debug("waiting for ToF MAVLink endpoint %s: %s", self.range_endpoint, exc)
            return None

    def step_once(self) -> None:
        """Apply latest gimbal setpoint and send one 7-beam ToF + gimbal status tick."""
        if self._gimbal is not None:
            target = self._gimbal.target()
            if target is not None:
                self.world.set_gimbal(target[0], target[1])
            pitch_deg, yaw_deg = self.world.gimbal_angles()
            try:
                self._gimbal.publish(pitch_deg, 0.0, yaw_deg)
            except OSError as exc:
                logger.debug("gimbal publish skipped: %s", exc)

        sender = self._ensure_sender()
        if sender is not None:
            boot_ms = int((time.monotonic() - self._booted) * 1000.0) & 0xFFFFFFFF
            readings = rangefinders_to_distance_readings(self.world.read_rangefinders_m(), boot_ms)
            for reading in readings:
                try:
                    sender(reading)
                except OSError as exc:
                    logger.debug("ToF send failed, resetting link: %s", exc)
                    self._reset_sender()
                    break

    def _reset_sender(self) -> None:
        self._sender = None
        link, self._link = self._link, None
        if link is not None:
            try:
                link.close()
            except Exception:
                pass

    def _run(self) -> None:
        period = 1.0 / self.rate_hz
        while not self._stop.is_set():
            started = time.monotonic()
            self.step_once()
            remaining = period - (time.monotonic() - started)
            if remaining > 0:
                self._stop.wait(remaining)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._gimbal is not None:
            self._gimbal.close()
        self._reset_sender()
