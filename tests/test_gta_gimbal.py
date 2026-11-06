from __future__ import annotations

import socket
import struct
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from horus.config import GimbalSettings
from horus.gimbal import MavlinkGimbal
from horus.gimbal.device import GimbalDeviceEndpoint
from horus.gimbal.gta import GtaGimbalBridge, decode_status, encode_setpoint, STATUS_SILENCE_S


def test_setpoint_bytes_match_the_mod_layout() -> None:
    # HorusGta.dll's GimbalProtocol: "HGIM" | u32 version 1 | u32 seq | f32 pitch | f32 yaw, little-endian.
    assert encode_setpoint(7, -30.0, 45.0) == bytes.fromhex("4847494d" "01000000" "07000000" "0000f0c1" "00003442")


def _status_bytes(seq: int = 3, time_ms: int = 1234, angles: tuple[float, ...] = (0.5, -29.0, 44.0, 2.0, -3.0),
                  magic: bytes = b"HGAT", version: int = 1) -> bytes:
    return magic + struct.pack("<III5f", version, seq, time_ms, *angles)


def test_status_decodes_and_anything_else_is_rejected() -> None:
    status = decode_status(_status_bytes())
    assert (status.seq, status.time_ms) == (3, 1234)
    assert (status.camera_roll_deg, status.camera_pitch_deg, status.camera_yaw_deg) == (0.5, -29.0, 44.0)
    assert (status.vehicle_roll_deg, status.vehicle_pitch_deg) == (2.0, -3.0)
    for bad in (_status_bytes()[:-1], _status_bytes() + b"\0", _status_bytes(magic=b"HRNG"), _status_bytes(version=2),
                _status_bytes(angles=(0.5, float("nan"), 44.0, 2.0, -3.0))):
        with pytest.raises(ValueError):
            decode_status(bad)


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for(condition: Callable[[], Any], timeout_s: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def _vehicle() -> SimpleNamespace:
    return SimpleNamespace(heading_deg=90.0, vehicle_roll_deg=0.0, vehicle_pitch_deg=0.0,
                           attitude_updated_monotonic=time.monotonic())


def _setpoints(mod: socket.socket, timeout_s: float) -> list[tuple[int, float, float]]:
    """HGIM datagrams the fake mod receives within ``timeout_s``: (seq, pitch, yaw)."""
    got = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        mod.settimeout(max(0.01, deadline - time.monotonic()))
        try:
            data = mod.recv(64)
        except TimeoutError:
            break
        magic, version, seq, pitch, yaw = struct.unpack("<4sIIff", data)
        assert (magic, version) == (b"HGIM", 1)
        got.append((seq, pitch, yaw))
    return got


def test_horus_points_the_mod_and_reads_back_what_it_measures_over_udp() -> None:
    horus_port = _free_udp_port()
    gimbal = MavlinkGimbal(GimbalSettings(connection=f"udpin:127.0.0.1:{horus_port}"), _vehicle)
    endpoint = GimbalDeviceEndpoint(f"udpout:127.0.0.1:{horus_port}")
    mod = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mod.bind(("127.0.0.1", 0))
    listen = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listen.bind(("127.0.0.1", 0))
    listen.setblocking(False)
    lines: list[str] = []
    bridge = GtaGimbalBridge(endpoint, listen, mod.getsockname(), log=lines.append)
    stop = threading.Event()
    runner = threading.Thread(target=bridge.run, args=(stop.is_set,), daemon=True)
    gimbal.connect()
    endpoint.start()
    runner.start()
    try:
        _wait_for(lambda: gimbal.device_heartbeat_age_s() is not None)
        assert _setpoints(mod, 0.3) == [], "nothing reaches the mod before HORUS points the camera"

        gimbal.point(-30.0, 45.0)
        first = _setpoints(mod, 1.5)
        assert first, "HORUS's setpoint reached the mod"
        assert all((pitch, yaw) == (-30.0, 45.0) for _, pitch, yaw in first)
        # Sent again while unchanged, so a restarted mod picks it up.
        assert len(first) >= 2 and [seq for seq, _, _ in first] == sorted({seq for seq, _, _ in first})

        mod.sendto(_status_bytes(angles=(0.5, -29.0, 44.0, 2.0, -3.0)), listen.getsockname())
        attitude = _wait_for(gimbal.attitude)
        assert (attitude.pitch_deg, attitude.roll_deg, attitude.yaw_deg) == (
            pytest.approx(-29.0, abs=1e-3), pytest.approx(0.5, abs=1e-3), pytest.approx(44.0, abs=1e-3))
        mod.sendto(_status_bytes(seq=4, angles=(0.0, -30.0, 45.0, 1.0, -1.0)), listen.getsockname())
        latest = _wait_for(lambda: (a := gimbal.attitude()) and a.pitch_deg == pytest.approx(-30.0, abs=1e-3) and a)
        assert latest.yaw_deg == pytest.approx(45.0, abs=1e-3)

        mod.sendto(b"\x01\x02\x03", listen.getsockname())
        mod.sendto(b"\x01\x02\x03", listen.getsockname())
        _wait_for(lambda: any(line.startswith("dropped") for line in lines))

        # The mod goes quiet: nothing more reaches HORUS, and the device says so once.
        time.sleep(STATUS_SILENCE_S + 0.3)
        assert gimbal.attitude().updated_monotonic == latest.updated_monotonic
        assert sum("silent" in line for line in lines) == 1
        assert sum(line.startswith("dropped") for line in lines) == 1
        mod.sendto(_status_bytes(seq=5), listen.getsockname())
        _wait_for(lambda: gimbal.attitude().updated_monotonic > latest.updated_monotonic)
        assert any("status back" in line for line in lines)
        # The log names the measured attitude once, when it first came within 0.5° of the setpoint:
        # the -29/44 status was 1° short, the -30/45 one arrived.
        arrivals = [line for line in lines if line.startswith("measured camera")]
        assert len(arrivals) == 1
        assert "pitch -30.00 yaw 45.00" in arrivals[0] and "setpoint pitch -30.0 yaw 45.0 reached" in arrivals[0]
    finally:
        stop.set()
        runner.join(timeout=2)
        endpoint.close()
        gimbal.close()
        mod.close()
        listen.close()
