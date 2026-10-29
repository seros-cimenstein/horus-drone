"""Stream the drone camera pose from ArduPilot to the GTA V renderer mod (gta/horus_mod).

``horus-gta-bridge`` reads LOCAL_POSITION_NED and ATTITUDE from a MAVLink endpoint (SITL serial1
by default, so HORUS keeps serial0) and sends a 56-byte UDP pose packet to HorusGta.dll at a fixed
rate. The mod places GTA's scripted camera at that pose, so GTA renders the drone camera view
while SITL keeps flying the vehicle.

The bridge is also the camera's gimbal device: an ideal self-levelling gimbal on the MAVLink
gimbal-device protocol (horus/gimbal/device.py). It holds HORUS's latest setpoint exactly and
reports that attitude back as measured; ArduPilot takes no part in the gimbal.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import math
import os
import socket
import struct
import time
from dataclasses import dataclass

from horus.config import FlightSettings, GimbalSettings, HorusConfig, load_config
from horus.flight.link import MavlinkFlightLink, Telemetry
from horus.gimbal.device import GimbalDeviceEndpoint
from horus.mavlink import load_mavutil

LOGGER = logging.getLogger(__name__)

# Little-endian: magic | u32 version | u64 seq | f64 north, east, down (m) |
# f32 roll, pitch, yaw (deg) | f32 hfov (deg). gta/horus_mod/src/Core/PoseProtocol.cs parses it.
PACKET = struct.Struct("<4sIQdddffff")
PACKET_MAGIC = b"HPOS"
PACKET_VERSION = 1
PACKET_SIZE = PACKET.size

DEFAULT_MAVLINK = "tcp:127.0.0.1:5762"
DEFAULT_TARGET = "127.0.0.1:5611"
# HORUS listens for the gimbal device on udpin:127.0.0.1:14600 (gimbal.connection).
DEFAULT_GIMBAL = "udpout:127.0.0.1:14600"
DEFAULT_RATE_HZ = 50.0
# Camera horizontal FOV when no HORUS config is available.
FALLBACK_HFOV_DEG = 90.0
# A position or heading older than this stops the stream (the mod then hands the screen back to
# the game after its timeout).
POSE_MAX_AGE_S = 1.0
# HORUS itself is component 190 (FlightSettings.source_component).
BRIDGE_SOURCE_COMPONENT = 191
STATUS_INTERVAL_S = 5.0


@dataclass(frozen=True, slots=True)
class CameraPose:
    """Camera pose in the NED frame of SITL home: metres, and degrees with yaw clockwise from
    north, pitch positive up and roll positive right side down."""

    north_m: float
    east_m: float
    down_m: float
    roll_deg: float
    pitch_deg: float
    yaw_deg: float


def encode_packet(seq: int, pose: CameraPose, hfov_deg: float) -> bytes:
    return PACKET.pack(
        PACKET_MAGIC,
        PACKET_VERSION,
        seq,
        pose.north_m,
        pose.east_m,
        pose.down_m,
        pose.roll_deg,
        pose.pitch_deg,
        pose.yaw_deg,
        hfov_deg,
    )


def decode_packet(data: bytes) -> tuple[int, CameraPose, float]:
    """``(seq, pose, hfov_deg)`` of a packet; ValueError for one the mod would reject."""
    if len(data) != PACKET_SIZE:
        raise ValueError(f"pose packet must be {PACKET_SIZE} bytes, got {len(data)}")
    magic, version, seq, *values = PACKET.unpack(data)
    if magic != PACKET_MAGIC:
        raise ValueError(f"bad pose packet magic {magic!r}")
    if version != PACKET_VERSION:
        raise ValueError(f"unsupported pose packet version {version}")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("pose packet has a non-finite field")
    hfov_deg = values[-1]
    if not 0.0 < hfov_deg < 180.0:
        raise ValueError(f"pose packet hfov {hfov_deg} is outside (0, 180)")
    return seq, CameraPose(*values[:6]), hfov_deg


def _fresh(updated_monotonic: float | None, now: float, max_age_s: float) -> bool:
    return updated_monotonic is not None and now - updated_monotonic <= max_age_s


def compose_camera_pose(
    telemetry: Telemetry, now: float, gimbal_pitch_deg: float, gimbal_yaw_deg: float
) -> CameraPose | None:
    """Camera pose from a telemetry snapshot and the gimbal attitude (horizon-relative pitch,
    nose-relative yaw, level roll), or None without a fresh position and heading.

    The camera sits at the vehicle position; its yaw is the vehicle heading plus the gimbal yaw.
    """
    pose = telemetry.pose
    heading = telemetry.heading_deg
    if pose is None or not _fresh(telemetry.pose_updated_monotonic, now, POSE_MAX_AGE_S):
        return None
    if heading is None or not _fresh(telemetry.heading_updated_monotonic, now, POSE_MAX_AGE_S):
        return None
    return CameraPose(
        pose.north_m,
        pose.east_m,
        -pose.up_m,
        0.0,
        gimbal_pitch_deg,
        (heading + gimbal_yaw_deg) % 360.0,
    )


class CameraGimbal:
    """The GTA camera's gimbal device: an ideal self-levelling gimbal that holds HORUS's latest
    setpoint, clamped to the gimbal limits as a mechanical stop would, with level roll until the
    next setpoint. Before any setpoint it faces the nose at ``default_pitch_deg``, the pitch
    HORUS's own geometry assumes then (``executor.default_gimbal_pitch_deg``)."""

    def __init__(self, endpoint: GimbalDeviceEndpoint, default_pitch_deg: float, limits: GimbalSettings):
        self._endpoint = endpoint
        self._default_pitch_deg = default_pitch_deg
        self._limits = limits

    def update(self) -> tuple[float, float]:
        """(pitch_deg, yaw_deg) the camera holds now, reported to HORUS as the measured attitude."""
        target = self._endpoint.target()
        if target is None:
            pitch, yaw = self._default_pitch_deg, 0.0
        else:
            limits = self._limits
            pitch = min(max(target[0], limits.pitch_min_deg), limits.pitch_max_deg)
            yaw = min(max(target[1], -limits.yaw_limit_deg), limits.yaw_limit_deg)
        self._endpoint.publish(pitch, 0.0, yaw)
        return pitch, yaw


def parse_target(text: str) -> tuple[str, int]:
    """``host:port`` to a UDP address."""
    host, separator, port_text = text.rpartition(":")
    if not separator or not host:
        raise ValueError(f"target must be host:port, got {text!r}")
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"target port must be an integer, got {port_text!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"target port must be 1..65535, got {port}")
    return host, port


class GtaTelemetryLink(MavlinkFlightLink):
    """HORUS's flight link, so position and heading are read exactly as HORUS reads them,
    requesting only the camera-pose messages and at the bridge rate. Message intervals apply per
    MAVLink channel, so HORUS's own streams on serial0 are unaffected."""

    def __init__(self, settings: FlightSettings, interval_us: int):
        super().__init__(settings)
        self._interval_us = interval_us

    def _request_telemetry(self) -> None:
        mavlink = load_mavutil().mavlink
        try:
            for message_id in (mavlink.MAVLINK_MSG_ID_ATTITUDE, mavlink.MAVLINK_MSG_ID_LOCAL_POSITION_NED):
                self.connection.mav.command_long_send(
                    self.connection.target_system,
                    self.connection.target_component,
                    mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                    0,
                    message_id,
                    self._interval_us,
                    0,
                    0,
                    0,
                    0,
                    0,
                )
        except OSError:
            # The autopilot keeps its default stream rates.
            return


def _load_horus_config(path: str | None) -> HorusConfig | None:
    """The HORUS config (explicit path, HORUS_CONFIG or config/horus.yaml), or None when neither
    a path nor HORUS_CONFIG is given and config/horus.yaml does not exist."""
    try:
        return load_config(path)
    except FileNotFoundError:
        if path is not None or os.environ.get("HORUS_CONFIG"):
            raise
        return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="horus-gta-bridge",
        description="Send the drone camera pose from ArduPilot to the GTA V renderer mod (HorusGta.dll) "
                    "and act as the camera's MAVLink gimbal device for HORUS.",
    )
    parser.add_argument(
        "--mavlink",
        default=DEFAULT_MAVLINK,
        help=f"pymavlink endpoint (default: {DEFAULT_MAVLINK}, SITL serial1; HORUS uses serial0)",
    )
    parser.add_argument(
        "--target", default=DEFAULT_TARGET, help=f"mod UDP host:port (default: {DEFAULT_TARGET})"
    )
    parser.add_argument(
        "--gimbal", default=DEFAULT_GIMBAL,
        help=f"pymavlink endpoint of the gimbal device towards HORUS's gimbal.connection "
             f"(default: {DEFAULT_GIMBAL})",
    )
    parser.add_argument(
        "--rate-hz", type=float, default=DEFAULT_RATE_HZ,
        help="pose packets and gimbal reports per second, also the requested MAVLink rate "
             f"(default: {DEFAULT_RATE_HZ:g})",
    )
    parser.add_argument(
        "--hfov-deg", type=float, default=None,
        help="camera horizontal FOV (default: the HORUS config's executor.camera_horizontal_fov_deg, "
             f"or {FALLBACK_HFOV_DEG:g} without a config)",
    )
    parser.add_argument(
        "--default-pitch-deg", type=float, default=None,
        help="camera pitch before HORUS's first gimbal setpoint (default: the HORUS config's "
             "executor.default_gimbal_pitch_deg, or 0 without a config)",
    )
    parser.add_argument(
        "--config", default=None,
        help="HORUS config for the defaults above, the gimbal limits and the gimbal device ids "
             "(default: HORUS_CONFIG or config/horus.yaml when present)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        target = parse_target(args.target)
        config = _load_horus_config(args.config)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if not (math.isfinite(args.rate_hz) and 0.0 < args.rate_hz <= 1000.0):
        parser.error("--rate-hz must be in (0, 1000]")
    hfov_deg = args.hfov_deg
    if hfov_deg is None:
        hfov_deg = FALLBACK_HFOV_DEG if config is None else config.executor.camera_horizontal_fov_deg
    if not (math.isfinite(hfov_deg) and 0.0 < hfov_deg < 180.0):
        parser.error("--hfov-deg must be in (0, 180)")
    default_pitch_deg = args.default_pitch_deg
    if default_pitch_deg is None:
        default_pitch_deg = 0.0 if config is None else config.executor.default_gimbal_pitch_deg
    gimbal_settings = GimbalSettings() if config is None else config.gimbal
    if not gimbal_settings.pitch_min_deg <= default_pitch_deg <= gimbal_settings.pitch_max_deg:
        parser.error(
            f"--default-pitch-deg must be between {gimbal_settings.pitch_min_deg:g} "
            f"and {gimbal_settings.pitch_max_deg:g}"
        )
    # The bridge only listens: HORUS, not the bridge, sets the GPS-free global origin.
    settings = dataclasses.replace(
        config.flight if config is not None else FlightSettings(),
        connection=args.mavlink,
        source_component=BRIDGE_SOURCE_COMPONENT,
        gps_free_mode=False,
    )
    try:
        run(settings, gimbal_settings, args.gimbal, target, args.rate_hz, hfov_deg, default_pitch_deg)
    except KeyboardInterrupt:
        pass


def run(
    settings: FlightSettings,
    gimbal_settings: GimbalSettings,
    gimbal_connection: str,
    target: tuple[str, int],
    rate_hz: float,
    hfov_deg: float,
    default_pitch_deg: float,
) -> None:
    """Send pose packets to ``target`` and serve HORUS's gimbal on ``gimbal_connection`` until
    interrupted."""
    period = 1.0 / rate_hz
    link = GtaTelemetryLink(settings, interval_us=max(1, round(period * 1e6)))
    LOGGER.info("connecting to MAVLink %s", settings.connection)
    link.connect()
    endpoint = GimbalDeviceEndpoint(
        gimbal_connection, system=gimbal_settings.device_system, component=gimbal_settings.device_component
    )
    endpoint.start()
    gimbal = CameraGimbal(endpoint, default_pitch_deg, gimbal_settings)
    LOGGER.info(
        "sending %g Hz pose packets to udp %s:%d (hfov %.1f deg, default pitch %.1f deg); "
        "gimbal device on %s",
        rate_hz, *target, hfov_deg, default_pitch_deg, gimbal_connection,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = 0
    sent_since_status = 0
    send_error: OSError | None = None
    camera: CameraPose | None = None
    next_status = time.monotonic() + STATUS_INTERVAL_S
    next_send = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            camera = compose_camera_pose(link.telemetry(), now, *gimbal.update())
            if camera is not None:
                try:
                    sock.sendto(encode_packet(seq, camera, hfov_deg), target)
                    seq += 1
                    sent_since_status += 1
                except OSError as exc:
                    send_error = exc
            if now >= next_status:
                if camera is None:
                    LOGGER.warning("no fresh position and heading from MAVLink; not sending")
                else:
                    LOGGER.info(
                        "%d packets in %.0f s, seq %d: ned=(%.2f, %.2f, %.2f) rpy=(%.1f, %.1f, %.1f)",
                        sent_since_status, STATUS_INTERVAL_S, seq - 1, camera.north_m, camera.east_m,
                        camera.down_m, camera.roll_deg, camera.pitch_deg, camera.yaw_deg,
                    )
                if send_error is not None:
                    LOGGER.warning("UDP send to %s:%d failed: %s", *target, send_error)
                    send_error = None
                sent_since_status = 0
                next_status = now + STATUS_INTERVAL_S
            next_send += period
            delay = next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_send = time.monotonic()  # fell behind: do not burst to catch up
    finally:
        sock.close()
        endpoint.close()
        link.close()


if __name__ == "__main__":
    main()
