from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from horus.config import FlightSettings, GimbalSettings
from horus.flight.link import Pose, Telemetry
from horus.gta_bridge import (
    PACKET_SIZE,
    CameraGimbal,
    CameraPose,
    GtaTelemetryLink,
    compose_camera_pose,
    decode_packet,
    encode_packet,
    parse_target,
)
from horus.mavlink import load_mavutil

# Shared with the C# parser tests (gta/horus_mod/tests/Tests.cs).
VECTOR = json.loads(
    (Path(__file__).resolve().parents[1] / "gta/horus_mod/tests/pose_vector.json").read_text(encoding="utf-8")
)
VECTOR_POSE = CameraPose(
    VECTOR["north_m"], VECTOR["east_m"], VECTOR["down_m"],
    VECTOR["roll_deg"], VECTOR["pitch_deg"], VECTOR["yaw_deg"],
)
NOW = 100.0


def test_packet_is_56_bytes() -> None:
    assert PACKET_SIZE == 56
    assert len(encode_packet(0, VECTOR_POSE, 90.0)) == 56


def test_encoding_matches_the_shared_csharp_test_vector() -> None:
    assert encode_packet(VECTOR["seq"], VECTOR_POSE, VECTOR["hfov_deg"]).hex() == VECTOR["hex"]
    assert decode_packet(bytes.fromhex(VECTOR["hex"])) == (VECTOR["seq"], VECTOR_POSE, VECTOR["hfov_deg"])


def test_packet_layout_is_little_endian_in_contract_order() -> None:
    data = encode_packet(2**40 + 3, CameraPose(1.0, 2.0, 3.0, 4.0, 5.0, 6.0), 7.0)
    assert data[:4] == b"HPOS"
    assert struct.unpack_from("<I", data, 4) == (1,)
    assert struct.unpack_from("<Q", data, 8) == (2**40 + 3,)
    assert struct.unpack_from("<ddd", data, 16) == (1.0, 2.0, 3.0)
    assert struct.unpack_from("<ffff", data, 40) == (4.0, 5.0, 6.0, 7.0)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data[:-1],
        lambda data: data + b"\0",
        lambda data: b"XPOS" + data[4:],
        lambda data: data[:4] + struct.pack("<I", 2) + data[8:],
        lambda data: data[:16] + struct.pack("<d", math.nan) + data[24:],
        lambda data: data[:48] + struct.pack("<f", math.inf) + data[52:],
        lambda data: data[:52] + struct.pack("<f", 0.0),
        lambda data: data[:52] + struct.pack("<f", 180.0),
    ],
)
def test_decode_rejects_what_the_mod_rejects(mutate) -> None:
    with pytest.raises(ValueError):
        decode_packet(mutate(bytes.fromhex(VECTOR["hex"])))


def _telemetry(
    *,
    up_m: float = 12.0,
    heading_deg: float | None = 30.0,
    pose_age_s: float = 0.02,
    heading_age_s: float = 0.02,
) -> Telemetry:
    return Telemetry(
        observed_at="",
        received_monotonic=NOW,
        pose=Pose(4.0, -2.5, up_m),
        pose_updated_monotonic=NOW - pose_age_s,
        heading_deg=heading_deg,
        heading_updated_monotonic=NOW - heading_age_s,
    )


class _FakeEndpoint:
    """GimbalDeviceEndpoint without a socket: ``setpoint`` is HORUS's latest accepted setpoint."""

    def __init__(self) -> None:
        self.setpoint: tuple[float, float] | None = None
        self.published: list[tuple[float, float, float]] = []

    def target(self) -> tuple[float, float] | None:
        return self.setpoint

    def publish(self, pitch_deg: float, roll_deg: float, yaw_deg: float) -> None:
        self.published.append((pitch_deg, roll_deg, yaw_deg))


def _gimbal(default_pitch_deg: float = -45.0) -> tuple[CameraGimbal, _FakeEndpoint]:
    endpoint = _FakeEndpoint()
    return CameraGimbal(endpoint, default_pitch_deg, GimbalSettings()), endpoint  # type: ignore[arg-type]


def test_before_any_setpoint_the_camera_faces_the_nose_at_the_default_pitch_and_reports_it() -> None:
    gimbal, endpoint = _gimbal(default_pitch_deg=-45.0)
    camera = compose_camera_pose(_telemetry(), NOW, *gimbal.update())
    assert camera == CameraPose(4.0, -2.5, -12.0, 0.0, -45.0, 30.0)
    assert endpoint.published == [(-45.0, 0.0, 0.0)]


def test_setpoint_pitch_is_horizon_relative_and_yaw_is_added_to_the_heading() -> None:
    gimbal, endpoint = _gimbal()
    endpoint.setpoint = (-60.0, 25.0)
    camera = compose_camera_pose(_telemetry(heading_deg=350.0), NOW, *gimbal.update())
    assert camera is not None
    assert (camera.roll_deg, camera.pitch_deg) == (0.0, -60.0)
    assert camera.yaw_deg == pytest.approx(15.0)  # 350 + 25, wrapped
    assert endpoint.published == [(-60.0, 0.0, 25.0)]


def test_the_setpoint_is_held_without_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [NOW]
    monkeypatch.setattr("horus.gta_bridge.time", SimpleNamespace(monotonic=lambda: clock[0]))
    gimbal, endpoint = _gimbal()
    endpoint.setpoint = (-20.0, -90.0)
    for clock[0] in (NOW, NOW + 5.0, NOW + 600.0):
        assert gimbal.update() == (-20.0, -90.0)
    assert endpoint.published == [(-20.0, 0.0, -90.0)] * 3


@pytest.mark.parametrize(
    ("setpoint", "held"),
    [
        ((-120.0, 0.0), (-90.0, 0.0)),
        ((60.0, 0.0), (45.0, 0.0)),
        ((0.0, 175.0), (0.0, 160.0)),
        ((0.0, -179.0), (0.0, -160.0)),
    ],
)
def test_setpoints_beyond_horus_limits_stop_at_the_limit(setpoint, held) -> None:
    gimbal, endpoint = _gimbal()
    endpoint.setpoint = setpoint
    assert gimbal.update() == held
    assert endpoint.published == [(held[0], 0.0, held[1])]


@pytest.mark.parametrize(
    "telemetry",
    [
        Telemetry(observed_at="", received_monotonic=NOW, heading_deg=0.0, heading_updated_monotonic=NOW),
        _telemetry(pose_age_s=1.5),
        _telemetry(heading_deg=None),
        _telemetry(heading_age_s=1.5),
    ],
)
def test_no_packet_without_a_fresh_position_and_heading(telemetry: Telemetry) -> None:
    assert compose_camera_pose(telemetry, NOW, 0.0, 0.0) is None


def test_link_messages_compose_like_horus_reads_them() -> None:
    link = GtaTelemetryLink(FlightSettings(), interval_us=20_000)
    link._consume(SimpleNamespace(
        get_type=lambda: "LOCAL_POSITION_NED", x=3.0, y=-1.0, z=-25.0, vx=0.0, vy=0.0, vz=-1.0,
    ))
    link._consume(SimpleNamespace(
        get_type=lambda: "ATTITUDE", roll=math.radians(5.0), pitch=math.radians(-3.0), yaw=math.radians(-90.0),
    ))
    telemetry = link.telemetry()
    camera = compose_camera_pose(telemetry, telemetry.received_monotonic, -45.0, -20.0)
    assert camera is not None
    assert (camera.north_m, camera.east_m, camera.down_m) == (3.0, -1.0, -25.0)
    # The vehicle lean never reaches the camera: the gimbal is self-levelling.
    assert (camera.pitch_deg, camera.roll_deg) == (-45.0, 0.0)
    assert camera.yaw_deg == pytest.approx(250.0)  # heading 270 + gimbal -20


class _FakeMav:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def command_long_send(self, *fields: object) -> None:
        self.calls.append(fields)


def test_link_requests_only_the_pose_messages_at_the_bridge_rate() -> None:
    mavlink = load_mavutil().mavlink
    link = GtaTelemetryLink(FlightSettings(), interval_us=20_000)
    mav = _FakeMav()
    link._connection = SimpleNamespace(target_system=1, target_component=1, mav=mav)
    link._request_telemetry()
    assert {call[2] for call in mav.calls} == {mavlink.MAV_CMD_SET_MESSAGE_INTERVAL}
    assert {call[4]: call[5] for call in mav.calls} == {
        mavlink.MAVLINK_MSG_ID_ATTITUDE: 20_000,
        mavlink.MAVLINK_MSG_ID_LOCAL_POSITION_NED: 20_000,
    }


def test_parse_target() -> None:
    assert parse_target("127.0.0.1:5611") == ("127.0.0.1", 5611)
    assert parse_target("gta-host:6000") == ("gta-host", 6000)
    for bad in ("5611", ":5611", "host:", "host:0", "host:70000", "host:x"):
        with pytest.raises(ValueError):
            parse_target(bad)
