"""The device side of the MAVLink gimbal-device protocol, for simulated gimbals.

A simulator (the Gazebo gimbal, the GTA gimbal bridge) runs one endpoint: it announces itself with a
gimbal HEARTBEAT, takes HORUS's GIMBAL_DEVICE_SET_ATTITUDE setpoints and reports the attitude it
measures as GIMBAL_DEVICE_ATTITUDE_STATUS, horizon-locked with nose-relative yaw. Setpoints in
another frame are ignored.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

from horus.gimbal import frames
from horus.mavlink import load_mavutil

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 1.0


class GimbalDeviceEndpoint:
    """Gimbal-device MAVLink endpoint; ``target()`` is HORUS's latest setpoint, ``publish()``
    reports a measured attitude."""

    def __init__(
        self,
        connection: str,
        *,
        system: int = 1,
        component: int = 154,
        mavlink_connection: Any | None = None,
    ):
        self._address = connection
        self._system = system
        self._component = component
        self._connection = mavlink_connection
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._target: tuple[float, float] | None = None
        self._booted = time.monotonic()

    def start(self) -> None:
        if self._connection is None:
            mavutil = load_mavutil()
            self._connection = mavutil.mavlink_connection(
                self._address, source_system=self._system, source_component=self._component
            )
        self._thread = threading.Thread(target=self._run, name="gimbal-device", daemon=True)
        self._thread.start()

    def target(self) -> tuple[float, float] | None:
        """(pitch_deg, yaw_deg) of the latest accepted setpoint: earth-frame pitch, nose-relative yaw."""
        with self._lock:
            return self._target

    def publish(self, pitch_deg: float, roll_deg: float, yaw_deg: float) -> None:
        """Report the measured attitude: horizon-relative pitch/roll, nose-relative yaw (degrees)."""
        if self._connection is None:
            return
        q = frames.quaternion_from_euler(roll_deg, pitch_deg, yaw_deg)
        with self._send_lock:
            self._connection.mav.gimbal_device_attitude_status_send(
                0,
                0,
                self._time_boot_ms(),
                frames.HORIZON_LOCKED_VEHICLE_YAW,
                q,
                math.nan,
                math.nan,
                math.nan,
                0,
                math.nan,
                math.nan,
                0,
            )

    def _time_boot_ms(self) -> int:
        return int((time.monotonic() - self._booted) * 1000.0) & 0xFFFFFFFF

    def _heartbeat(self) -> None:
        mavutil = load_mavutil()
        mavlink = mavutil.mavlink
        with self._send_lock:
            self._connection.mav.heartbeat_send(
                mavlink.MAV_TYPE_GIMBAL, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, mavlink.MAV_STATE_ACTIVE
            )

    def _accept(self, message: Any) -> None:
        if message.get_type() != "GIMBAL_DEVICE_SET_ATTITUDE":
            return
        try:
            target_system = int(message.target_system)
            target_component = int(message.target_component)
        except (AttributeError, TypeError, ValueError):
            return
        if target_system not in (0, self._system) or target_component not in (0, self._component):
            return
        angles = frames.setpoint_angles(message)
        if angles is None:
            logger.warning("ignoring a gimbal setpoint outside horizon-pitch/nose-yaw frames (flags %s)",
                           getattr(message, "flags", None))
            return
        with self._lock:
            self._target = angles

    def _run(self) -> None:
        next_heartbeat = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_heartbeat:
                try:
                    self._heartbeat()
                except OSError as exc:
                    logger.debug("gimbal heartbeat not sent: %s", exc)
                next_heartbeat = now + HEARTBEAT_INTERVAL_S
            try:
                message = self._connection.recv_match(blocking=True, timeout=0.1)
            except (OSError, AttributeError):
                if self._stop.wait(0.2):
                    break
                continue
            if message is not None:
                self._accept(message)

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._connection is not None:
            self._connection.close()
            self._connection = None
