"""Forward HorusGta.dll's range-sensor packets to ArduPilot as MAVLink DISTANCE_SENSOR.

In GTA's JSON-physics mode the drone flies in the game, so the HORUS ToF payload is emulated
there: HorusGta.dll casts the payload's seven beams into the GTA world and sends one UDP
datagram per sweep (gta/horus_mod/README.md "Range sensors"). This bridge gives each beam the
payload sensor's MAVLink identity and range limits, and sends it to SITL's serial2 exactly as
the Gazebo ToF bridge does, so ArduPilot's proximity avoidance and HORUS see the same messages.

Datagram (little-endian, 44 bytes): ``b"HRNG"``, uint32 version 1, uint32 seq, uint32 time_ms
(the physics simulation time of the sweep), then seven float32 ranges in metres from the hull,
+inf when nothing is hit within range, in the order of :data:`PACKET_SENSORS`.
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .bridge import DistanceSensorReading, laser_scan_to_distance_sensor
from .mavlink_out import BRIDGE_SOURCE_COMPONENT, BRIDGE_SOURCE_SYSTEM, PAYLOAD_SENSORS, PymavlinkSender, open_mavlink

PACKET = struct.Struct("<4sIII7f")
MAGIC = b"HRNG"
VERSION = 1
DEFAULT_LISTEN = "127.0.0.1:5612"
# The beams in packet order, by the Gazebo payload sensor each one stands in for.
PACKET_SENSORS = (
    "proximity_front",
    "proximity_front_right",
    "proximity_right",
    "proximity_rear",
    "proximity_left",
    "proximity_front_left",
    "range_down",
)
# The payload's ToF limits (sim/models/horus_sensor_payload/model.sdf: 0.05-4.0 m).
RANGE_MIN_M = 0.05
RANGE_MAX_M = 4.0

_IDENTITIES = {name: (orientation, sensor_id) for name, orientation, sensor_id in PAYLOAD_SENSORS}


@dataclass(frozen=True, slots=True)
class RangePacket:
    seq: int
    time_ms: int
    ranges: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class _Beam:
    """One beam as the one-sample LaserScan the shared converter takes."""

    range_min: float
    range_max: float
    ranges: tuple[float, ...]


def decode_range_packet(data: bytes) -> RangePacket:
    """Parse one HorusGta range datagram; ValueError for anything that is not one."""
    if len(data) != PACKET.size:
        raise ValueError(f"range packet is {len(data)} bytes, expected {PACKET.size}")
    magic, version, seq, time_ms, *ranges = PACKET.unpack(data)
    if magic != MAGIC:
        raise ValueError(f"range packet magic is {magic!r}, expected {MAGIC!r}")
    if version != VERSION:
        raise ValueError(f"range packet version is {version}, expected {VERSION}")
    if any(math.isnan(value) or value < 0 for value in ranges):
        raise ValueError("range packet holds a NaN or negative range")
    return RangePacket(seq=seq, time_ms=time_ms, ranges=tuple(ranges))


def packet_readings(packet: RangePacket) -> list[DistanceSensorReading]:
    """One DISTANCE_SENSOR reading per beam, with the payload sensor's identity and limits.

    A hit closer than the 5 cm minimum is still an obstacle at the hull, so it is reported at
    the minimum rather than dropped (the shared converter ignores returns below the minimum).
    """
    readings = []
    for name, distance_m in zip(PACKET_SENSORS, packet.ranges, strict=True):
        orientation, sensor_id = _IDENTITIES[name]
        if math.isfinite(distance_m):
            distance_m = max(distance_m, RANGE_MIN_M)
        reading = laser_scan_to_distance_sensor(
            _Beam(RANGE_MIN_M, RANGE_MAX_M, (distance_m,)),
            time_boot_ms=packet.time_ms,
            orientation=orientation,
            sensor_id=sensor_id,
        )
        if reading is not None:
            readings.append(reading)
    return readings


def _parse_address(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"address must be HOST:PORT, got {text!r}")
    return host, int(port)


def serve(
    sock: socket.socket,
    send: Callable[[DistanceSensorReading], None],
    *,
    log: Callable[[str], None] = print,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Forward every valid datagram on ``sock``; report malformed ones once each kind."""
    reported: set[str] = set()
    forwarded = 0
    while not should_stop():
        try:
            data = sock.recv(256)
        except TimeoutError:
            continue
        try:
            packet = decode_range_packet(data)
        except ValueError as exc:
            if str(exc) not in reported:
                reported.add(str(exc))
                log(f"dropped: {exc}")
            continue
        for reading in packet_readings(packet):
            send(reading)
        forwarded += 1
        if forwarded == 1:
            log(f"first range packet forwarded (seq {packet.seq}, sim {packet.time_ms} ms)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m horus.tof_bridge.gta",
        description="Forward HorusGta.dll range packets to ArduPilot as DISTANCE_SENSOR.",
    )
    parser.add_argument("--listen", default=DEFAULT_LISTEN, help=f"UDP HOST:PORT (default {DEFAULT_LISTEN})")
    parser.add_argument("--mavlink", default="tcp:127.0.0.1:5763", help="SITL endpoint (default serial2)")
    return parser


def _open_when_listening(endpoint: str) -> Any:
    """Open ``endpoint`` once SITL listens there: serial2 opens only after SITL boots.

    A TCP endpoint is probed quietly first; pymavlink's own connect retries print every attempt.
    """
    scheme, _, address = endpoint.partition(":")
    if scheme == "tcp":
        host, port = _parse_address(address)
        while True:
            try:
                socket.create_connection((host, port), timeout=1.0).close()
                break
            except OSError:
                time.sleep(1.0)
    return open_mavlink(endpoint)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        listen = _parse_address(args.listen)
    except ValueError as exc:
        parser.error(str(exc))
    link = _open_when_listening(args.mavlink)
    print(
        f"Connected to {args.mavlink} as sysid={BRIDGE_SOURCE_SYSTEM} compid={BRIDGE_SOURCE_COMPONENT}",
        flush=True,
    )
    # Bound only now, so sweeps sent while SITL was still booting are not queued and replayed.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(listen)
    sock.settimeout(1.0)
    print(f"listening for HorusGta range packets on udp {args.listen}", flush=True)
    try:
        serve(sock, PymavlinkSender(link), log=lambda line: print(line, flush=True))
    except KeyboardInterrupt:
        return 0
    finally:
        sock.close()
        close = getattr(link, "close", None)
        if callable(close):
            close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
