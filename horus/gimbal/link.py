"""HORUS as the gimbal manager: a direct MAVLink link to the camera gimbal device.

The autopilot takes no part: HORUS sends GIMBAL_DEVICE_SET_ATTITUDE to the device and reads its
GIMBAL_DEVICE_ATTITUDE_STATUS, on the gimbal's own endpoint (``GimbalSettings.connection``).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from horus.config import GimbalSettings
from horus.gimbal import frames
from horus.mavlink import load_mavutil

logger = logging.getLogger(__name__)

# The setpoint is re-sent this often so a lost datagram or a device restart cannot leave the
# camera at another angle.
SETPOINT_RESEND_S = 0.5
# A body-frame status sample is composed with the vehicle lean only when that lean is this fresh.
LEAN_MAX_AGE_S = 1.0
GIMBAL_DEVICE_ATTITUDE_STATUS_ID = 285


@dataclass(frozen=True, slots=True)
class GimbalAttitude:
    """Measured gimbal attitude in degrees: pitch and roll relative to the horizon (pitch negative
    down), yaw relative to the vehicle nose (positive right)."""

    pitch_deg: float
    roll_deg: float
    yaw_deg: float
    updated_at: str
    # Monotonic receive time of this sample, for freshness checks on the same clock as frames.
    updated_monotonic: float

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        monotonic = time.monotonic() if now is None else now
        return {**asdict(self), "age_s": max(0.0, monotonic - self.updated_monotonic)}


class Gimbal(Protocol):
    """The gimbal interface the executor and perception call; MavlinkGimbal implements it."""

    settings: GimbalSettings

    def attitude(self) -> GimbalAttitude | None: ...

    def point(self, pitch_deg: float, yaw_deg: float = 0.0) -> None: ...

    def close(self) -> None: ...


class VehicleAttitude(Protocol):
    """Vehicle heading and lean for frame conversion (the flight link's Telemetry fits)."""

    heading_deg: float | None
    vehicle_roll_deg: float | None
    vehicle_pitch_deg: float | None
    attitude_updated_monotonic: float | None


class MavlinkGimbal:
    """Gimbal-device link: horizon-locked pitch setpoints, nose-relative yaw, measured attitude.

    ``vehicle`` returns the current vehicle attitude, needed only to place earth-frame yaw reports
    and, with ``settings.status_body_frame``, to compose the lean into body-frame reports.
    """

    def __init__(
        self,
        settings: GimbalSettings,
        vehicle: Callable[[], VehicleAttitude],
        connection: Any | None = None,
    ):
        if settings.connection is None and connection is None:
            raise ValueError("MavlinkGimbal needs gimbal.connection")
        self.settings = settings
        self._vehicle = vehicle
        self._connection = connection
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._attitude: GimbalAttitude | None = None
        self._setpoint: tuple[float, float] | None = None
        self._setpoint_sent_monotonic: float | None = None
        self._device_heartbeat_monotonic: float | None = None
        self._rate_requested = False

    def connect(self) -> None:
        """Open the endpoint and start reading; returns at once (the device may come up later)."""
        if self._connection is None:
            mavutil = load_mavutil()
            self._connection = mavutil.mavlink_connection(
                self.settings.connection,
                baud=self.settings.baud,
                source_system=self.settings.source_system,
                source_component=self.settings.source_component,
            )
        self._reader = threading.Thread(target=self._run, name="horus-gimbal", daemon=True)
        self._reader.start()

    def attitude(self) -> GimbalAttitude | None:
        with self._lock:
            return self._attitude

    def device_heartbeat_age_s(self, now: float | None = None) -> float | None:
        with self._lock:
            seen = self._device_heartbeat_monotonic
        return None if seen is None else max(0.0, (time.monotonic() if now is None else now) - seen)

    def point(self, pitch_deg: float, yaw_deg: float = 0.0) -> None:
        """Hold the camera at earth-frame ``pitch_deg`` and nose-relative ``yaw_deg``."""
        pitch = float(pitch_deg)
        yaw = float(yaw_deg)
        if not math.isfinite(pitch) or not math.isfinite(yaw):
            raise ValueError("gimbal pitch and yaw must be finite")
        low, high = self.settings.pitch_min_deg, self.settings.pitch_max_deg
        if not low <= pitch <= high:
            raise ValueError(f"gimbal pitch must be between {low:g} and {high:g} degrees")
        limit = self.settings.yaw_limit_deg
        if not -limit <= yaw <= limit:
            raise ValueError(f"gimbal yaw must be between {-limit:g} and {limit:g} degrees")
        with self._lock:
            self._setpoint = (pitch, yaw)
        self._send_setpoint()

    def _send_setpoint(self) -> None:
        with self._lock:
            setpoint = self._setpoint
        if setpoint is None or self._connection is None:
            return
        q = frames.setpoint_quaternion(*setpoint)
        with self._send_lock:
            self._connection.mav.gimbal_device_set_attitude_send(
                self.settings.device_system,
                self.settings.device_component,
                frames.HORIZON_LOCKED_VEHICLE_YAW,
                q,
                math.nan,
                math.nan,
                math.nan,
            )
        with self._lock:
            self._setpoint_sent_monotonic = time.monotonic()

    def _request_status_rate(self) -> None:
        mavutil = load_mavutil()
        with self._send_lock:
            self._connection.mav.command_long_send(
                self.settings.device_system,
                self.settings.device_component,
                mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                0,
                GIMBAL_DEVICE_ATTITUDE_STATUS_ID,
                1e6 / self.settings.status_rate_hz,
                0,
                0,
                0,
                0,
                0,
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._connection.recv_match(blocking=True, timeout=0.1)
            except (OSError, AttributeError):
                if self._stop.wait(0.2):
                    break
                continue
            if message is not None:
                self._consume(message)
            with self._lock:
                due = (
                    self._setpoint is not None
                    and (
                        self._setpoint_sent_monotonic is None
                        or time.monotonic() - self._setpoint_sent_monotonic >= SETPOINT_RESEND_S
                    )
                )
            if due:
                try:
                    self._send_setpoint()
                except OSError as exc:
                    logger.warning("could not send gimbal setpoint: %s", exc)

    def _from_device(self, message: Any) -> bool:
        try:
            return (
                int(message.get_srcSystem()) == self.settings.device_system
                and int(message.get_srcComponent()) == self.settings.device_component
            )
        except (AttributeError, TypeError, ValueError):
            return False

    def _consume(self, message: Any) -> None:
        kind = message.get_type()
        if kind not in {"HEARTBEAT", "GIMBAL_DEVICE_ATTITUDE_STATUS"} or not self._from_device(message):
            return
        now = time.monotonic()
        if kind == "HEARTBEAT":
            with self._lock:
                self._device_heartbeat_monotonic = now
                first = not self._rate_requested
                self._rate_requested = True
            if first:
                try:
                    self._request_status_rate()
                except OSError as exc:
                    logger.warning("could not request the gimbal status rate: %s", exc)
            return
        vehicle = self._vehicle()
        lean = (0.0, 0.0) if not self.settings.status_body_frame else _fresh_lean_deg(vehicle, now)
        angles = None if lean is None else frames.status_angles(message, vehicle.heading_deg, lean)
        if angles is None:
            return
        with self._lock:
            self._attitude = GimbalAttitude(
                *angles, datetime.now(timezone.utc).isoformat(), now
            )

    def close(self) -> None:
        self._stop.set()
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=1.0)
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def _fresh_lean_deg(vehicle: VehicleAttitude, now: float) -> tuple[float, float] | None:
    """Vehicle roll/pitch when at most ``LEAN_MAX_AGE_S`` old, else None."""
    if (
        vehicle.vehicle_roll_deg is None
        or vehicle.vehicle_pitch_deg is None
        or vehicle.attitude_updated_monotonic is None
        or now - vehicle.attitude_updated_monotonic > LEAN_MAX_AGE_S
    ):
        return None
    return vehicle.vehicle_roll_deg, vehicle.vehicle_pitch_deg
