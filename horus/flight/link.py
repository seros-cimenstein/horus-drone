"""Threaded ArduPilot MAVLink client with a coherent, timestamped telemetry cache."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from horus.config import FlightSettings


# Ignore position, acceleration, yaw and yaw-rate fields; only NED velocity is active.
VELOCITY_ONLY_MASK = (1 | 2 | 4 | 64 | 128 | 256 | 1024 | 2048)
# Velocity plus absolute yaw: ArduCopter holds its current heading for velocity-only targets, which
# cancels a separate CONDITION_YAW, so target-facing skills send yaw in the same setpoint.
VELOCITY_YAW_MASK = (1 | 2 | 4 | 64 | 128 | 256 | 2048)
# The model pitch joint spans -135..45, but ArduPilot's mount command rejects below -90.
GIMBAL_PITCH_MIN_DEG = -90.0
GIMBAL_PITCH_MAX_DEG = 45.0
GIMBAL_YAW_LIMIT_DEG = 160.0


@dataclass(frozen=True, slots=True)
class Pose:
    """Local pose in meters: north/east and altitude above local origin."""

    north_m: float
    east_m: float
    up_m: float

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Velocity:
    north_m_s: float = 0.0
    east_m_s: float = 0.0
    down_m_s: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DistanceSensor:
    orientation: int
    distance_m: float
    min_distance_m: float
    max_distance_m: float
    updated_at: str


@dataclass(frozen=True, slots=True)
class Telemetry:
    observed_at: str
    received_monotonic: float
    pose: Pose | None = None
    velocity: Velocity = field(default_factory=Velocity)
    mode: str = "UNKNOWN"
    armed: bool = False
    battery_remaining_pct: int | None = None
    battery_voltage_v: float | None = None
    proximity: tuple[DistanceSensor, ...] = ()
    statustext: tuple[str, ...] = ()
    heartbeat_monotonic: float | None = None
    heading_deg: float | None = None
    pose_updated_monotonic: float | None = None
    time_boot_ms: int | None = None

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        monotonic = time.monotonic() if now is None else now
        return {
            "observed_at": self.observed_at,
            "time_boot_ms": self.time_boot_ms,
            "observation_age_s": max(0.0, monotonic - self.received_monotonic),
            "pose_age_s": (
                None if self.pose_updated_monotonic is None
                else max(0.0, monotonic - self.pose_updated_monotonic)
            ),
            "pose": None if self.pose is None else self.pose.as_dict(),
            "velocity": self.velocity.as_dict(),
            "mode": self.mode,
            "armed": self.armed,
            "battery_remaining_pct": self.battery_remaining_pct,
            "battery_voltage_v": self.battery_voltage_v,
            "heading_deg": self.heading_deg,
            "proximity": [asdict(sensor) for sensor in self.proximity],
            # Status text is deliberately an untrusted quoted value at API boundaries.
            "statustext": list(self.statustext),
            "heartbeat_age_s": (
                None if self.heartbeat_monotonic is None
                else max(0.0, monotonic - self.heartbeat_monotonic)
            ),
        }


class MavlinkFlightLink:
    """ArduPilot link. Motion commands are NED velocity setpoints, never position targets."""

    def __init__(self, settings: FlightSettings, connection: Any | None = None):
        self.settings = settings
        self._connection = connection
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._heartbeat_monotonic: float | None = None
        self._pose: Pose | None = None
        self._global_origin: tuple[float, float] | None = None
        self._heading_deg: float | None = None
        self._local_position_seen = False
        self._velocity = Velocity()
        self._pose_updated_monotonic: float | None = None
        self._time_boot_ms: int | None = None
        self._mode = "UNKNOWN"
        self._armed = False
        self._battery_remaining_pct: int | None = None
        self._battery_voltage_v: float | None = None
        self._proximity: dict[int, DistanceSensor] = {}
        self._statustext: list[str] = []
        self._last_received_monotonic = time.monotonic()
        self._last_observed_at = datetime.now(timezone.utc).isoformat()

    @property
    def connection(self) -> Any:
        if self._connection is None:
            raise RuntimeError("MAVLink is not connected")
        return self._connection

    def connect(self) -> None:
        if self._connection is not None:
            return
        global_origin = (
            self._gps_global_origin_fields() if self.settings.gps_free_mode else None
        )
        from pymavlink import mavutil

        self._connection = mavutil.mavlink_connection(
            self.settings.connection,
            baud=self.settings.baud,
            source_system=self.settings.source_system,
            source_component=self.settings.source_component,
            # TCP connects retry once per second; SITL may not be listening yet when HORUS starts.
            retries=max(3, int(self.settings.heartbeat_timeout_s)),
        )
        heartbeat = self._connection.wait_heartbeat(timeout=self.settings.heartbeat_timeout_s)
        if heartbeat is None:
            self._connection.close()
            self._connection = None
            raise TimeoutError(
                f"No MAVLink heartbeat from {self.settings.connection} within "
                f"{self.settings.heartbeat_timeout_s:g}s"
            )
        with self._lock:
            self._accept_heartbeat(heartbeat)
        if global_origin is not None:
            # This establishes only the autopilot's global/EKF origin; it is not a
            # GPS fix or a synthetic local-position update in this link.
            with self._send_lock:
                self.connection.mav.set_gps_global_origin_send(
                    self.connection.target_system, *global_origin
                )
        self._request_telemetry()
        self._reader = threading.Thread(target=self._read_loop, name="horus-mavlink", daemon=True)
        self._reader.start()

    def _gps_global_origin_fields(self) -> tuple[int, int, int]:
        """Validate the configured WGS-84 origin and convert it to MAVLink units."""
        latitude = float(self.settings.global_origin_latitude_deg)
        longitude = float(self.settings.global_origin_longitude_deg)
        altitude = float(self.settings.global_origin_altitude_m)
        if not all(math.isfinite(value) for value in (latitude, longitude, altitude)):
            raise ValueError("GPS-free global origin coordinates must be finite")
        if not -90.0 <= latitude <= 90.0:
            raise ValueError("global origin latitude must be between -90 and 90 degrees")
        if not -180.0 <= longitude <= 180.0:
            raise ValueError("global origin longitude must be between -180 and 180 degrees")
        altitude_mm = int(round(altitude * 1000.0))
        if not -(1 << 31) <= altitude_mm <= (1 << 31) - 1:
            raise ValueError("global origin altitude must fit a signed MAVLink millimeter field")
        return (
            int(round(latitude * 1e7)),
            int(round(longitude * 1e7)),
            altitude_mm,
        )

    def _request_telemetry(self) -> None:
        """Request useful messages at 5 Hz where the autopilot supports intervals."""
        mav = self.connection.mav
        command = getattr(mav, "command_long_send", None)
        if command is None:
            return
        try:
            from pymavlink import mavutil

            for message_id in (2, 30, 32, 33, 74, 132, 147, 1, 253):
                command(
                    self.connection.target_system,
                    self.connection.target_component,
                    mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                    0,
                    message_id,
                    200_000,
                    0,
                    0,
                    0,
                    0,
                    0,
                )
        except (AttributeError, OSError):
            # A link may not expose interval controls; incoming heartbeat/telemetry still work.
            return

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                message = self.connection.recv_match(blocking=True, timeout=0.5)
            except (OSError, AttributeError):
                if self._stop.wait(0.2):
                    break
                continue
            if message is not None:
                self._consume(message)

    def _consume(self, message: Any) -> None:
        kind = message.get_type()
        now = time.monotonic()
        with self._lock:
            self._last_received_monotonic = now
            self._last_observed_at = datetime.now(timezone.utc).isoformat()
            if kind == "SYSTEM_TIME":
                try:
                    time_boot_ms = int(message.time_boot_ms)
                except (AttributeError, TypeError, ValueError, OverflowError):
                    time_boot_ms = -1
                if 0 <= time_boot_ms <= 0xFFFFFFFF:
                    self._time_boot_ms = time_boot_ms
            elif kind == "HEARTBEAT":
                self._accept_heartbeat(message, now)
            elif kind == "LOCAL_POSITION_NED":
                self._pose = Pose(float(message.x), float(message.y), -float(message.z))
                self._velocity = Velocity(float(message.vx), float(message.vy), float(message.vz))
                self._local_position_seen = True
                self._pose_updated_monotonic = now
            elif kind == "GLOBAL_POSITION_INT":
                self._velocity = Velocity(
                    float(message.vx) / 100.0,
                    float(message.vy) / 100.0,
                    float(message.vz) / 100.0,
                )
                heading = int(message.hdg)
                if 0 <= heading < 36000:
                    self._heading_deg = heading / 100.0
                # GPS fallback is kept in the same relative-meter frame as local NED.
                if not self._local_position_seen:
                    latitude = float(message.lat) / 1e7
                    longitude = float(message.lon) / 1e7
                    if self._global_origin is None:
                        self._global_origin = (latitude, longitude)
                    origin_lat, origin_lon = self._global_origin
                    north = (latitude - origin_lat) * 111_320.0
                    east = (longitude - origin_lon) * 111_320.0 * math.cos(
                        math.radians(origin_lat)
                    )
                    self._pose = Pose(
                        north,
                        east,
                        float(message.relative_alt) / 1000.0,
                    )
                    self._pose_updated_monotonic = now
            elif kind == "ATTITUDE":
                self._heading_deg = (math.degrees(float(message.yaw)) + 360.0) % 360.0
            elif kind == "SYS_STATUS":
                remaining = int(message.battery_remaining)
                self._battery_remaining_pct = remaining if 0 <= remaining <= 100 else None
                voltage = int(message.voltage_battery)
                self._battery_voltage_v = voltage / 1000.0 if voltage > 0 else None
            elif kind == "BATTERY_STATUS":
                remaining = int(message.battery_remaining)
                self._battery_remaining_pct = remaining if 0 <= remaining <= 100 else None
                voltages = [int(v) for v in message.voltages if 0 < int(v) < 65535]
                self._battery_voltage_v = sum(voltages) / 1000.0 if voltages else None
            elif kind == "DISTANCE_SENSOR":
                current = float(message.current_distance) / 100.0
                minimum = float(message.min_distance) / 100.0
                maximum = float(message.max_distance) / 100.0
                orientation = int(message.orientation)
                if 0 <= orientation <= 7 and minimum <= current <= maximum:
                    self._proximity[orientation] = DistanceSensor(
                        orientation, current, minimum, maximum, self._last_observed_at
                    )
            elif kind == "STATUSTEXT":
                text = message.text
                if isinstance(text, bytes):
                    text = text.decode("utf-8", errors="replace")
                text = str(text).rstrip("\x00")
                if text:
                    self._statustext.append(text)
                    self._statustext = self._statustext[-20:]

    def _accept_heartbeat(self, message: Any, now: float | None = None) -> None:
        from pymavlink import mavutil

        self._heartbeat_monotonic = time.monotonic() if now is None else now
        self._armed = bool(int(message.base_mode) & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
        try:
            mode_name = self.connection.flightmode
            if mode_name:
                self._mode = str(mode_name)
        except AttributeError:
            self._mode = "UNKNOWN"

    def telemetry(self) -> Telemetry:
        with self._lock:
            return Telemetry(
                self._last_observed_at,
                self._last_received_monotonic,
                self._pose,
                self._velocity,
                self._mode,
                self._armed,
                self._battery_remaining_pct,
                self._battery_voltage_v,
                tuple(sorted(self._proximity.values(), key=lambda value: value.orientation)),
                tuple(self._statustext),
                self._heartbeat_monotonic,
                self._heading_deg,
                self._pose_updated_monotonic,
                self._time_boot_ms,
            )

    def heartbeat_ok(self, max_age_s: float = 3.0) -> bool:
        with self._lock:
            heartbeat = self._heartbeat_monotonic
        return heartbeat is not None and time.monotonic() - heartbeat <= max_age_s

    def _set_mode(self, name: str) -> None:
        mapping = self.connection.mode_mapping()
        if name not in mapping:
            raise RuntimeError(f"Autopilot does not advertise {name} mode")
        with self._send_lock:
            self.connection.mav.set_mode_send(
                self.connection.target_system,
                1,
                mapping[name],
            )

    def set_guided(self) -> None:
        self._set_mode("GUIDED")

    def arm(self) -> None:
        from pymavlink import mavutil

        with self._send_lock:
            self.connection.mav.command_long_send(
                self.connection.target_system,
                self.connection.target_component,
                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                0,
                1,
                0,
                0,
                0,
                0,
                0,
                0,
            )

    def takeoff(self, altitude_m: float) -> None:
        from pymavlink import mavutil

        with self._send_lock:
            self.connection.mav.command_long_send(
                self.connection.target_system,
                self.connection.target_component,
                mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                float(altitude_m),
            )

    def land(self) -> None:
        self._set_mode("LAND")

    def rth(self) -> None:
        self._set_mode("RTL")

    def set_velocity_ned(
        self, north_m_s: float, east_m_s: float, down_m_s: float, yaw_deg: float | None = None
    ) -> None:
        """Guided NED velocity setpoint; ``yaw_deg`` (heading, 0 = north) also sets the heading."""
        mav = self.connection.mav
        from pymavlink import mavutil

        if yaw_deg is None:
            mask, yaw_rad = VELOCITY_ONLY_MASK, 0.0
        else:
            yaw = float(yaw_deg)
            if not math.isfinite(yaw):
                raise ValueError("yaw must be finite")
            mask = VELOCITY_YAW_MASK
            yaw_rad = math.atan2(math.sin(math.radians(yaw)), math.cos(math.radians(yaw)))
        with self._send_lock:
            mav.set_position_target_local_ned_send(
                int(time.monotonic() * 1000) & 0xFFFFFFFF,
                self.connection.target_system,
                self.connection.target_component,
                mavutil.mavlink.MAV_FRAME_LOCAL_NED,
                mask,
                0.0,
                0.0,
                0.0,
                float(north_m_s),
                float(east_m_s),
                float(down_m_s),
                0.0,
                0.0,
                0.0,
                yaw_rad,
                0.0,
            )

    def set_yaw(self, heading_deg: float, speed_deg_s: float = 30.0) -> None:
        from pymavlink import mavutil

        with self._send_lock:
            self.connection.mav.command_long_send(
                self.connection.target_system,
                self.connection.target_component,
                mavutil.mavlink.MAV_CMD_CONDITION_YAW,
                0,
                float(heading_deg) % 360.0,
                float(speed_deg_s),
                1,
                0,
                0,
                0,
                0,
            )

    def set_gimbal_pitch_yaw(self, pitch_deg: float, yaw_deg: float = 0.0) -> None:
        """Point the mount in degrees (pitch earth-frame, yaw body-frame)."""
        pitch = float(pitch_deg)
        yaw = float(yaw_deg)
        if not math.isfinite(pitch) or not math.isfinite(yaw):
            raise ValueError("gimbal pitch and yaw must be finite")
        if not GIMBAL_PITCH_MIN_DEG <= pitch <= GIMBAL_PITCH_MAX_DEG:
            raise ValueError(
                f"gimbal pitch must be between {GIMBAL_PITCH_MIN_DEG:g} "
                f"and {GIMBAL_PITCH_MAX_DEG:g} degrees"
            )
        if not -GIMBAL_YAW_LIMIT_DEG <= yaw <= GIMBAL_YAW_LIMIT_DEG:
            raise ValueError(
                f"gimbal yaw must be between {-GIMBAL_YAW_LIMIT_DEG:g} "
                f"and {GIMBAL_YAW_LIMIT_DEG:g} degrees"
            )
        from pymavlink import mavutil

        with self._send_lock:
            self.connection.mav.command_long_send(
                self.connection.target_system,
                self.connection.target_component,
                mavutil.mavlink.MAV_CMD_DO_MOUNT_CONTROL,
                0,
                pitch,
                0.0,
                yaw,
                0.0,
                0.0,
                0.0,
                mavutil.mavlink.MAV_MOUNT_MODE_MAVLINK_TARGETING,
            )

    def hold(self) -> None:
        """Remain in GUIDED, actively commanding zero velocity (safe without an RC)."""
        self.set_guided()
        self.set_velocity_ned(0.0, 0.0, 0.0)

    def close(self) -> None:
        self._stop.set()
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=1.0)
        if self._connection is not None:
            self._connection.close()
            self._connection = None
