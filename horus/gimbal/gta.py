"""The GTA gimbal as a MAVLink gimbal device: a bridge between HORUS and HorusGta.dll's gimbal.

HorusGta.dll holds the drone camera on a self-levelling three-axis gimbal (gta/horus_mod/README.md
"Gimbal"). This device announces itself to HORUS like the Gazebo one (``GimbalDeviceEndpoint``),
forwards HORUS's setpoint (horizon pitch, nose-relative yaw) to the mod as a 20-byte ``HGIM``
datagram, and publishes every 36-byte ``HGAT`` status the mod sends back as
GIMBAL_DEVICE_ATTITUDE_STATUS. Nothing is published that the mod did not measure: when its status
stops (game closed, SITL stopped), HORUS's gimbal attitude stops updating, as with a dead Gazebo
device. ArduPilot takes no part.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import select
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from horus.gimbal.device import GimbalDeviceEndpoint

logger = logging.getLogger(__name__)

SETPOINT = struct.Struct("<4sIIff")
STATUS = struct.Struct("<4sIIIfffff")
SETPOINT_MAGIC = b"HGIM"
STATUS_MAGIC = b"HGAT"
VERSION = 1
DEFAULT_MOD = "127.0.0.1:5613"
DEFAULT_LISTEN = "127.0.0.1:5614"
# The latest setpoint is sent again this often, so a mod that restarted or lost a datagram gets it.
SETPOINT_RESEND_S = 0.5
# The mod reports at least at 20 Hz (its heartbeat); this long without a status counts as silent.
STATUS_SILENCE_S = 1.0
# The log reports the measured attitude once it is this close to a new setpoint (degrees).
ARRIVAL_TOLERANCE_DEG = 0.5


@dataclass(frozen=True, slots=True)
class GimbalStatus:
    """One HGAT datagram: angles in degrees, camera pitch/roll from the horizon, yaw from the nose."""

    seq: int
    time_ms: int
    camera_roll_deg: float
    camera_pitch_deg: float
    camera_yaw_deg: float
    vehicle_roll_deg: float
    vehicle_pitch_deg: float


def encode_setpoint(seq: int, pitch_deg: float, yaw_deg: float) -> bytes:
    """The HGIM datagram for HORUS's setpoint (horizon pitch, nose-relative yaw, degrees)."""
    return SETPOINT.pack(SETPOINT_MAGIC, VERSION, seq & 0xFFFFFFFF, pitch_deg, yaw_deg)


def decode_status(data: bytes) -> GimbalStatus:
    """Parse one HGAT datagram; ValueError for anything that is not one."""
    if len(data) != STATUS.size:
        raise ValueError(f"status datagram of {len(data)} bytes, expected {STATUS.size}")
    magic, version, seq, time_ms, *angles = STATUS.unpack(data)
    if magic != STATUS_MAGIC:
        raise ValueError(f"status magic {magic!r}, expected {STATUS_MAGIC!r}")
    if version != VERSION:
        raise ValueError(f"status version {version}, expected {VERSION}")
    if not all(math.isfinite(angle) for angle in angles):
        raise ValueError("status with a non-finite angle")
    return GimbalStatus(seq, time_ms, *angles)


def parse_address(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"address must be HOST:PORT, got {text!r}")
    return host, int(port)


class GtaGimbalBridge:
    """Setpoints from ``endpoint`` to the mod at ``mod_address``; mod status on ``sock`` to ``endpoint``."""

    def __init__(
        self,
        endpoint: GimbalDeviceEndpoint,
        sock: socket.socket,
        mod_address: tuple[str, int],
        *,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = logger.info,
    ) -> None:
        self._endpoint = endpoint
        self._sock = sock
        self._mod = mod_address
        self._clock = clock
        self._log = log
        self._sent: tuple[float, float] | None = None
        self._sent_at = -math.inf
        self._seq = 0
        self._last_status_at: float | None = None
        self._silent = True
        self._statuses = 0
        self._rejected: set[str] = set()
        # The setpoint whose arrival the log still waits for: (pitch, yaw, monotonic time sent).
        self._awaiting: tuple[float, float, float] | None = None

    def forward_setpoint(self) -> bool:
        """Send HORUS's setpoint to the mod when it changed or is due again; True when sent."""
        target = self._endpoint.target()
        if target is None:
            return False
        now = self._clock()
        if target == self._sent and now - self._sent_at < SETPOINT_RESEND_S:
            return False
        self._seq += 1
        try:
            self._sock.sendto(encode_setpoint(self._seq, *target), self._mod)
        except OSError as exc:
            if "send" not in self._rejected:
                self._rejected.add("send")
                self._log(f"setpoint not sent to udp {self._mod[0]}:{self._mod[1]}: {exc}")
            return False
        if target != self._sent:
            self._log(f"setpoint pitch {target[0]:.1f} yaw {target[1]:.1f} -> mod")
            self._awaiting = (target[0], target[1], now)
        self._sent, self._sent_at = target, now
        return True

    def handle_datagram(self, data: bytes) -> GimbalStatus | None:
        """Publish one mod status to HORUS; malformed datagrams are dropped and reported once per kind."""
        try:
            status = decode_status(data)
        except ValueError as exc:
            if str(exc) not in self._rejected:
                self._rejected.add(str(exc))
                self._log(f"dropped: {exc}")
            return None
        self._endpoint.publish(status.camera_pitch_deg, status.camera_roll_deg, status.camera_yaw_deg)
        self._last_status_at = self._clock()
        self._statuses += 1
        if self._silent:
            self._silent = False
            self._log(
                f"mod status {'arriving' if self._statuses == 1 else 'back'} (seq {status.seq}, sim {status.time_ms} ms): "
                f"camera pitch {status.camera_pitch_deg:.1f} yaw {status.camera_yaw_deg:.1f} roll {status.camera_roll_deg:.1f}"
            )
        self._log_arrival(status)
        return status

    def _log_arrival(self, status: GimbalStatus) -> None:
        """Log the measured attitude once it first comes within ``ARRIVAL_TOLERANCE_DEG`` of the setpoint."""
        if self._awaiting is None:
            return
        pitch, yaw, sent_at = self._awaiting
        yaw_error = (status.camera_yaw_deg - yaw + 180.0) % 360.0 - 180.0
        if abs(status.camera_pitch_deg - pitch) > ARRIVAL_TOLERANCE_DEG or abs(yaw_error) > ARRIVAL_TOLERANCE_DEG:
            return
        self._awaiting = None
        self._log(
            f"measured camera pitch {status.camera_pitch_deg:.2f} yaw {status.camera_yaw_deg:.2f} roll "
            f"{status.camera_roll_deg:.2f} (vehicle roll {status.vehicle_roll_deg:.1f} pitch {status.vehicle_pitch_deg:.1f}): "
            f"setpoint pitch {pitch:.1f} yaw {yaw:.1f} reached {self._clock() - sent_at:.2f} s after it was sent"
        )

    def check_silence(self) -> bool:
        """True while no status came for ``STATUS_SILENCE_S``; logs the change once."""
        now = self._clock()
        silent = self._last_status_at is None or now - self._last_status_at >= STATUS_SILENCE_S
        if silent and not self._silent:
            self._silent = True
            self._log(f"mod status silent for {STATUS_SILENCE_S:g} s; HORUS sees no gimbal attitude until it returns")
        return silent

    def run(self, should_stop: Callable[[], bool] = lambda: False, poll_s: float = 0.02) -> None:
        while not should_stop():
            readable, _, _ = select.select([self._sock], [], [], poll_s)
            if readable:
                while True:
                    try:
                        data = self._sock.recv(256)
                    except (BlockingIOError, InterruptedError):
                        break
                    self.handle_datagram(data)
            self.forward_setpoint()
            self.check_silence()


def main(argv: list[str] | None = None) -> int:
    """Run the GTA gimbal device as ``horus-gta-gimbal`` or ``python -m horus.gimbal.gta``."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mavlink", default="udpout:127.0.0.1:14600",
                        help="MAVLink connection to HORUS's gimbal.connection (default: %(default)s)")
    parser.add_argument("--mod", default=DEFAULT_MOD,
                        help="UDP HOST:PORT of HorusGta.dll's [gimbal] bind/port (default: %(default)s)")
    parser.add_argument("--listen", default=DEFAULT_LISTEN,
                        help="UDP HOST:PORT for the mod's status, its [gimbal] target (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        mod = parse_address(args.mod)
        listen = parse_address(args.listen)
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=os.environ.get("HORUS_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(name)s %(message)s")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(listen)
    sock.setblocking(False)
    endpoint = GimbalDeviceEndpoint(args.mavlink)
    endpoint.start()
    logger.info("gimbal device: MAVLink %s, setpoints -> udp %s, status <- udp %s", args.mavlink, args.mod, args.listen)
    try:
        GtaGimbalBridge(endpoint, sock, mod).run()
    except KeyboardInterrupt:
        return 0
    finally:
        endpoint.close()
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
